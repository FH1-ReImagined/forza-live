"""The screening worker's models, CPU only (onnxruntime): what they see of a photo and what they
say about it. The decision is screen_policy's.

- Nsfw: Freepik/nsfw_image_detector (EVA02-B/14, 448 px, MIT), the whole frame squashed to 448 x
  448 as its training did; P(neutral), P(low), P(medium), P(high).
- Siglip: google/siglip2-base-patch16-224 (Apache-2.0) image embeddings of the whole frame and of
  tiles, scored against prompts.json's text embeddings (made at build time): per category, the
  highest sigmoid probability of its prompts over the regions (SigLIP's own head). A trained
  probe (a logistic layer over the same embeddings) can be added later: Probe.
- Ocr: RapidOCR (Apache-2.0) with the PP-OCRv6 models in its wheel: the lines of text and their
  confidence.
"""

import json
import os

# onnxruntime's Linux wheels (1.31) send usage telemetry to mobile.events.data.microsoft.com
# unless this is set before the library loads (onnxruntime.disable_telemetry_events() does not
# stop it; measured). The worker talks to nothing but the server.
os.environ["ORT_DISABLE_TELEMETRY"] = "1"

import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402

NSFW_LABELS = ("neutral", "low", "medium", "high")
CLIP_MEAN = np.array([0.48145466, 0.4578275, 0.40821073], np.float32)
CLIP_STD = np.array([0.26862954, 0.26130258, 0.27577711], np.float32)


def session(path, threads):
    import onnxruntime as ort
    opt = ort.SessionOptions()
    opt.intra_op_num_threads = threads
    opt.inter_op_num_threads = 1
    opt.log_severity_level = 3
    # Memory over speed (measured on a Ryzen 7 7800X3D, 4 threads): without the arena and without
    # prepacked weight copies each vision model holds about 425 MB instead of 850 MB between
    # photos, for about 5-10% more time per run.
    opt.enable_cpu_mem_arena = False
    opt.add_session_config_entry("session.disable_prepacking", "1")
    return ort.InferenceSession(path, sess_options=opt, providers=["CPUExecutionProvider"])


def softmax(x, axis=-1):
    e = np.exp(x - x.max(axis=axis, keepdims=True))
    return e / e.sum(axis=axis, keepdims=True)


class Nsfw:
    size = 448

    def __init__(self, models_dir, threads):
        self.path = os.path.join(models_dir, "nsfw", "model.onnx")
        self.sess = session(self.path, threads)

    def pixels(self, img):
        x = np.asarray(img.resize((self.size, self.size), Image.BICUBIC), np.float32) / 255.0
        return ((x - CLIP_MEAN) / CLIP_STD).transpose(2, 0, 1)[None]

    def __call__(self, img):
        logits = self.sess.run(None, {"pixel_values": self.pixels(img)})[0][0]
        p = softmax(logits.astype(np.float64))
        return {k: float(v) for k, v in zip(NSFW_LABELS, p)}


def regions(img, grids):
    """(name, box) of the whole frame and of each tile of each grid (cols x rows)."""
    w, h = img.size
    out = [("full", (0, 0, w, h))]
    for cols, rows in grids:
        for r in range(rows):
            for c in range(cols):
                box = (c * w // cols, r * h // rows, (c + 1) * w // cols, (r + 1) * h // rows)
                out.append(("tile%dx%d:%d,%d" % (cols, rows, c, r), box))
    return out


class Siglip:
    size = 224

    def __init__(self, models_dir, threads):
        self.path = os.path.join(models_dir, "siglip2", "vision_model.onnx")
        self.sess = session(self.path, threads)
        with open(os.path.join(models_dir, "siglip2", "preprocessor_config.json")) as f:
            pre = json.load(f)
        self.mean = np.array(pre["image_mean"], np.float32)
        self.std = np.array(pre["image_std"], np.float32)
        self.resample = int(pre.get("resample", 2))  # 2: PIL's bilinear, as the processor
        z = np.load(os.path.join(models_dir, "siglip2", "prompts.npz"))
        self.text = z["embeddings"].astype(np.float32)
        self.labels = [str(x) for x in z["labels"]]
        self.texts = [str(x) for x in z["texts"]]
        self.scale, self.bias = float(z["logit_scale"]), float(z["logit_bias"])
        self.categories = sorted(set(self.labels) - {"neutral", "selftest"})

    def embed(self, img, grids):
        names, crops = [], []
        for name, box in regions(img, grids):
            crop = img.crop(box).resize((self.size, self.size), self.resample)
            x = np.asarray(crop, np.float32) / 255.0
            crops.append(((x - self.mean) / self.std).transpose(2, 0, 1))
            names.append(name)
        outs = self.sess.run(["pooler_output"], {"pixel_values": np.stack(crops)})[0]
        emb = outs / np.linalg.norm(outs, axis=-1, keepdims=True)
        return names, emb.astype(np.float32)

    def score(self, names, emb, categories=None):
        """{category: {"sigmoid", "region", "prompt"}}: per category the highest sigmoid
        probability (SigLIP's own head: scale x cosine + bias) of its prompts over the regions,
        where and which prompt. (A softmax against the neutral prompts was tried: on plain tiles
        of sky or road no prompt fits and the share is noise, up to 0.7 on game screenshots.)"""
        cats = categories or self.categories
        logits = self.scale * emb.astype(np.float64) @ self.text.T.astype(np.float64) + self.bias
        sig = 1.0 / (1.0 + np.exp(-logits))  # [regions, prompts]
        labels = np.array(self.labels)
        out = {}
        for cat in cats:
            idx = np.where(labels == cat)[0]
            r, p = np.unravel_index(np.argmax(sig[:, idx]), (len(names), len(idx)))
            out[cat] = {"sigmoid": float(sig[r, idx[p]]), "region": names[r],
                        "prompt": self.texts[idx[p]]}
        return out


class Probe:
    """The hook for a trained classifier over SigLIP2 embeddings: an .npz with W [classes, 768],
    b [classes], labels [classes] (e.g. trained with scikit-learn's LogisticRegression on the
    embeddings the worker saves with [state] save_embeddings). Its score per label is the
    highest sigmoid over the regions."""

    def __init__(self, path):
        z = np.load(path)
        self.path = path
        self.w = z["W"].astype(np.float32)
        self.b = z["b"].astype(np.float32)
        self.labels = [str(x) for x in z["labels"]]

    def __call__(self, names, emb):
        p = 1.0 / (1.0 + np.exp(-(emb @ self.w.T + self.b)))
        return {lab: {"score": float(p[:, i].max()), "region": names[int(p[:, i].argmax())]}
                for i, lab in enumerate(self.labels)}


class Ocr:
    def __init__(self, threads, min_score=0.5):
        import rapidocr
        from rapidocr import RapidOCR
        models = os.path.join(os.path.dirname(rapidocr.__file__), "models")
        self.models = {
            "det": os.path.join(models, "PP-OCRv6_det_small.onnx"),
            "rec": os.path.join(models, "PP-OCRv6_rec_small.onnx"),
            "cls": os.path.join(models, "ch_ppocr_mobile_v2.0_cls_mobile.onnx"),
        }
        try:
            from importlib.metadata import version
            self.version = "rapidocr %s, PP-OCRv6 small" % version("rapidocr")
        except Exception:
            self.version = "rapidocr, PP-OCRv6 small"
        self.engine = RapidOCR(params={
            "Global.log_level": "error",
            "Global.text_score": min_score,
            "Det.model_path": self.models["det"],
            "Rec.model_path": self.models["rec"],
            "Cls.model_path": self.models["cls"],
            "EngineConfig.onnxruntime.intra_op_num_threads": threads,
            "EngineConfig.onnxruntime.inter_op_num_threads": 1,
        })

    def __call__(self, img):
        bgr = np.ascontiguousarray(np.asarray(img)[:, :, ::-1])  # never a path or URL
        res = self.engine(bgr)
        if not res or res.txts is None:
            return []
        return [(str(t), float(s)) for t, s in zip(res.txts, res.scores)]
