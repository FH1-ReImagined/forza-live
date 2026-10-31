#!/usr/bin/env python3
"""Builds the screening worker's models (run when the image is built, never at run time).

  python3 build_models.py --cache DIR --out DIR [fetch|export|embed|install|all]

- fetch: downloads every file of models.lock.json into --cache, each checked against its pinned
  SHA-256 (and size); a file already there with the right hash is kept.
- export: Freepik/nsfw_image_detector (an EVA02 checkpoint for timm) to ONNX, fp32, and checks
  onnxruntime's output against PyTorch's on a fixed input.
- embed: the SigLIP2 text embeddings of prompts.json (the text model is used only here).
- install: what the worker needs into --out (the NSFW ONNX, the SigLIP2 vision model and its
  preprocessor config, the prompt embeddings, the word lists) and --out/manifest.json, with the
  source revisions and the SHA-256 of every installed file.

export and embed need requirements-build.txt (PyTorch, timm, tokenizers); the worker's image
does not carry them.
"""

import argparse
import hashlib
import json
import os
import shutil
import sys
import time
import urllib.request

os.environ["ORT_DISABLE_TELEMETRY"] = "1"  # onnxruntime's telemetry upload (screen_models.py)
HERE = os.path.dirname(os.path.abspath(__file__))
LOCK = os.path.join(HERE, "models.lock.json")
PROMPTS = os.path.join(HERE, "prompts.json")
NSFW_ARCH = "eva02_base_patch14_448"
NSFW_LABELS = ["neutral", "low", "medium", "high"]
TEXT_LEN = 64  # SigLIP2's text length (padding="max_length", max_length=64)
EOS, PAD = 1, 0


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def lock():
    with open(LOCK) as f:
        return json.load(f)


def fetch(cache):
    for e in lock()["files"]:
        dest = os.path.join(cache, e["name"])
        if os.path.exists(dest) and sha256_of(dest) == e["sha256"]:
            continue
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        part = dest + ".part"
        h, size = hashlib.sha256(), 0
        for attempt in range(3):
            try:
                h, size = hashlib.sha256(), 0
                with urllib.request.urlopen(e["url"], timeout=120) as r, open(part, "wb") as out:
                    for block in iter(lambda: r.read(1 << 20), b""):
                        h.update(block)
                        size += len(block)
                        out.write(block)
                break
            except OSError as err:
                print("fetch: %s: %s (attempt %d)" % (e["name"], err, attempt + 1), flush=True)
                time.sleep(5)
        else:
            sys.exit("fetch: %s failed" % e["name"])
        if h.hexdigest() != e["sha256"] or ("size" in e and size != e["size"]):
            os.remove(part)
            sys.exit("fetch: %s: SHA-256 %s (%d bytes), the lock says %s" % (
                e["name"], h.hexdigest(), size, e["sha256"]))
        os.replace(part, dest)
        print("fetch: %s (%d bytes) ok" % (e["name"], size), flush=True)


def export_nsfw(cache):
    """The Freepik checkpoint (timm's eva02_base_patch14_448 with a 4-class head, bf16 weights,
    keys under timm_model.) as an fp32 ONNX graph: input pixel_values [N,3,448,448] (CLIP mean
    and std), output logits [N,4] (neutral, low, medium, high)."""
    import numpy as np
    import onnxruntime as ort
    import timm
    import torch
    from safetensors.torch import load_file
    from timm.layers import set_fused_attn

    set_fused_attn(False)  # plain attention ops: the TorchScript exporter cannot trace SDPA here

    with open(os.path.join(cache, "nsfw", "config.json")) as f:
        cfg = json.load(f)
    assert cfg["architecture"] == NSFW_ARCH and cfg["label_names"] == NSFW_LABELS, cfg
    model = timm.create_model(NSFW_ARCH, pretrained=False, num_classes=4,
                              global_pool=cfg.get("global_pool", "avg"))
    state = load_file(os.path.join(cache, "nsfw", "model.safetensors"))
    state = {k.removeprefix("timm_model."): v.float() for k, v in state.items()}
    model.load_state_dict(state, strict=True)
    model.eval()
    out = os.path.join(cache, "nsfw", "model.onnx")
    x = torch.from_numpy(np.random.default_rng(1).standard_normal((1, 3, 448, 448), np.float32))
    with torch.inference_mode():
        torch.onnx.export(model, (x,), out + ".part", input_names=["pixel_values"],
                          output_names=["logits"], opset_version=17, dynamo=False,
                          dynamic_axes={"pixel_values": {0: "n"}, "logits": {0: "n"}})
        want = torch.softmax(model(x), -1).numpy()
    os.replace(out + ".part", out)
    sess = ort.InferenceSession(out, providers=["CPUExecutionProvider"])
    logits = sess.run(None, {"pixel_values": x.numpy()})[0]
    got = np.exp(logits - logits.max(-1, keepdims=True))
    got /= got.sum(-1, keepdims=True)
    diff = float(np.abs(got - want).max())
    print("export: nsfw/model.onnx, |onnxruntime - torch| = %.2g over the 4 probabilities" % diff)
    if diff > 1e-3:
        sys.exit("export: the ONNX graph does not match PyTorch")


def tokenize(tok, texts):
    """SigLIP2's text input: lowercased, the Gemma tokenizer's ids without BOS, EOS, padded with
    PAD to TEXT_LEN (what its processor does with padding="max_length", max_length=64)."""
    import numpy as np
    tok.no_padding()  # tokenizer.json pads to 64 itself: padded here, after the EOS
    tok.no_truncation()
    ids = np.full((len(texts), TEXT_LEN), PAD, np.int64)
    for i, t in enumerate(texts):
        enc = tok.encode(t.lower(), add_special_tokens=False).ids[:TEXT_LEN - 1] + [EOS]
        ids[i, :len(enc)] = enc
    return ids


def embed(cache):
    import numpy as np
    import onnxruntime as ort
    from tokenizers import Tokenizer

    with open(PROMPTS) as f:
        spec = json.load(f)
    labels, texts = [], []
    for cat, prompts in spec["categories"].items():
        for p in prompts:
            labels.append(cat)
            texts.append(spec["template"].format(p))
    tok = Tokenizer.from_file(os.path.join(cache, "siglip2", "tokenizer.json"))
    sess = ort.InferenceSession(os.path.join(cache, "siglip2", "text_model.onnx"),
                                providers=["CPUExecutionProvider"])
    ids = tokenize(tok, texts)
    names = [o.name for o in sess.get_outputs()]
    outs = dict(zip(names, sess.run(None, {"input_ids": ids})))
    emb = outs["pooler_output"].astype(np.float32)
    emb /= np.linalg.norm(emb, axis=-1, keepdims=True)
    head = lock()["siglip2_head"]
    np.savez(os.path.join(cache, "siglip2", "prompts.npz"), embeddings=emb,
             labels=np.array(labels), texts=np.array(texts),
             logit_scale=np.float32(np.exp(head["logit_scale_log"])),
             logit_bias=np.float32(head["logit_bias"]))
    print("embed: %d prompts in %d categories" % (len(texts), len(spec["categories"])))


INSTALL = [  # (built file in the cache, installed name)
    ("nsfw/model.onnx", "nsfw/model.onnx"),
    ("nsfw/README.md", "nsfw/README.md"),  # its model card: the MIT licence and the authors
    ("siglip2/vision_model.onnx", "siglip2/vision_model.onnx"),
    ("siglip2/preprocessor_config.json", "siglip2/preprocessor_config.json"),
    ("siglip2/prompts.npz", "siglip2/prompts.npz"),
]


def install(cache, out):
    files = list(INSTALL) + [(e["name"], e["name"]) for e in lock()["files"]
                             if e["name"].startswith("wordlists/")]
    manifest = {"built": int(time.time()), "sources": [], "files": {}}
    for e in lock()["files"]:
        manifest["sources"].append({k: e[k] for k in ("name", "source", "revision", "sha256",
                                                      "license")})
    expected = lock().get("built", {})
    for src, name in files:
        dest = os.path.join(out, name)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.copyfile(os.path.join(cache, src), dest)
        manifest["files"][name] = sha256_of(dest)
        if name in expected:
            print("install: %s %s the lock's SHA-256" % (
                name, "matches" if expected[name] == manifest["files"][name] else "DIFFERS FROM"))
    versions = {}
    for mod in ("torch", "timm", "onnx", "onnxruntime", "tokenizers", "numpy"):
        try:
            versions[mod] = __import__(mod).__version__
        except ImportError:
            pass
    manifest["built_with"] = versions
    with open(os.path.join(HERE, "prompts.json")) as f:
        manifest["prompts_sha256"] = hashlib.sha256(f.read().encode()).hexdigest()
    with open(os.path.join(out, "manifest.json"), "w") as f:
        json.dump(manifest, f, indent=1)
    print("install: %d files into %s" % (len(files), out))


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--cache", required=True, help="downloads and intermediate files")
    ap.add_argument("--out", help="the installed models (install, all)")
    ap.add_argument("step", nargs="?", default="all",
                    choices=["fetch", "export", "embed", "install", "all"])
    a = ap.parse_args()
    if a.step in ("fetch", "all"):
        fetch(a.cache)
    if a.step in ("export", "all"):
        export_nsfw(a.cache)
    if a.step in ("embed", "all"):
        embed(a.cache)
    if a.step in ("install", "all"):
        if not a.out:
            sys.exit("--out is needed")
        install(a.cache, a.out)


if __name__ == "__main__":
    main()
