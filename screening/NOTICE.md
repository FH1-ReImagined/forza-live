# Notice: the screening worker's third-party components

The worker's own code (this folder) is part of Forza Live, under the GNU Affero General Public
License, version 3 or later (the repository's `LICENSE` and `NOTICE.md`).

Its image adds these, downloaded when it is built (none of them is in this repository), each
pinned by revision and SHA-256 in `models.lock.json` or by version and hash in
`requirements*.txt`:

| Component | Use | Licence |
|-----------|-----|---------|
| [Freepik/nsfw_image_detector](https://huggingface.co/Freepik/nsfw_image_detector) (Freepik; built on timm's EVA02-B/14) | NSFW classifier, converted to ONNX at build time | MIT (its model card, kept as `/models/nsfw/README.md`) |
| [google/siglip2-base-patch16-224](https://huggingface.co/google/siglip2-base-patch16-224), as converted to ONNX by [onnx-community](https://huggingface.co/onnx-community/siglip2-base-patch16-224-ONNX) | zero-shot image scores (the vision model; the text model only embeds the prompts at build time) | Apache-2.0 |
| [RapidOCR](https://github.com/RapidAI/RapidOCR) 3.10.0 and the PP-OCRv6 models in its wheel (Baidu / PaddleOCR) | text in the picture | Apache-2.0 |
| [List of Dirty, Naughty, Obscene, and Otherwise Bad Words](https://github.com/LDNOOBW/List-of-Dirty-Naughty-Obscene-and-Otherwise-Bad-Words) (Shutterstock and contributors) | word lists, 27 languages, unchanged (`/models/wordlists/ldnoobw`, with its `LICENSE`); the worker ignores the entries in `wordlists/ldnoobw_skip.txt` | CC-BY-4.0 |
| onnxruntime, NumPy, Pillow, OpenCV (headless) and RapidOCR's other dependencies | runtime | MIT, BSD, HPND, Apache-2.0 (each package's own licence) |
| PyTorch, timm, torchvision, onnx, tokenizers | build stage only, not in the image | BSD-3-Clause, Apache-2.0 |

`wordlists/terms.txt` is this project's own list (the slogans and numeric codes follow the ADL's
hate symbols database).
