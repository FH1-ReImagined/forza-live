"""A photo's bytes to the picture the models see, treated as hostile input: PNG or JPEG only, within
the sizes the server publishes (forza_community MIN_SIZE to MAX_SIZE), decompression bombs
refused, at most MAX_WIDTH wide for the models. And its perceptual hash (pHash: a 64-bit DCT
hash), to find a re-upload of a hidden photo."""

import io
import warnings

import numpy as np
from PIL import Image

MIN_SIZE = (640, 360)
MAX_SIZE = (7680, 4320)
MAX_WIDTH = 1920


class BadPicture(ValueError):
    pass


def decode(data):
    """An RGB PIL image of a PNG or JPEG, at most MAX_WIDTH wide; BadPicture for anything else."""
    if not isinstance(data, (bytes, bytearray)) or len(data) < 16:
        raise BadPicture("no picture")
    Image.MAX_IMAGE_PIXELS = MAX_SIZE[0] * MAX_SIZE[1]
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            img = Image.open(io.BytesIO(bytes(data)), formats=["PNG", "JPEG"])
            w, h = img.size
            if not (MIN_SIZE[0] <= w <= MAX_SIZE[0] and MIN_SIZE[1] <= h <= MAX_SIZE[1]):
                raise BadPicture("size %dx%d" % (w, h))
            if img.mode not in ("RGB", "RGBA", "L", "P", "LA"):
                raise BadPicture("mode %s" % img.mode)
            img.draft("RGB", (MAX_WIDTH, MAX_WIDTH))  # JPEG: decode at a reduced scale when larger
            img = img.convert("RGB")
            img.load()
    except BadPicture:
        raise
    except (OSError, ValueError, SyntaxError, Image.DecompressionBombError,
            Image.DecompressionBombWarning, MemoryError, EOFError) as e:
        raise BadPicture("%s: %s" % (type(e).__name__, str(e)[:120])) from None
    if img.width > MAX_WIDTH:
        img = img.resize((MAX_WIDTH, max(1, round(img.height * MAX_WIDTH / img.width))),
                         Image.BILINEAR)
    return img


_N = 32
_DCT = np.cos(np.pi * (2 * np.arange(_N)[None, :] + 1) * np.arange(_N)[:, None] / (2 * _N))


def phash(img):
    """64-bit pHash (hex): the 8 x 8 lowest frequencies (DC included) of the DCT of the 32 x 32
    greyscale picture, each against their median."""
    g = np.asarray(img.convert("L").resize((_N, _N), Image.LANCZOS), np.float64)
    low = (_DCT @ g @ _DCT.T)[:8, :8].ravel()
    bits = low > np.median(low)
    return "%016x" % int("".join("1" if b else "0" for b in bits), 2)


def distance(a, b):
    return bin(int(a, 16) ^ int(b, 16)).count("1")
