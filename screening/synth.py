"""Synthetic test pictures for the screening tests, drawn with Pillow (nothing downloaded, nothing
real): a plain scene, a car with a race-number livery, text, the start-up check's red star."""

import io
import math
import os

from PIL import Image, ImageDraw, ImageFont

W, H = 1920, 1080
FONTS = ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
         "/usr/share/fonts/dejavu-sans-fonts/DejaVuSans-Bold.ttf",
         "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf")


def font(size):
    for path in FONTS:
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    return ImageFont.load_default(size)  # Pillow's own font (10.1 and later)


def scene(w=W, h=H):
    """A sky over a road through grass: what a photo with nothing in it looks like."""
    img = Image.new("RGB", (w, h))
    d = ImageDraw.Draw(img)
    for y in range(h // 2):
        t = y / (h / 2)
        d.line([(0, y), (w, y)], fill=(int(90 + 80 * t), int(150 + 60 * t), int(230 - 20 * t)))
    d.rectangle([0, h // 2, w, h], fill=(70, 120, 60))
    d.polygon([(w * 0.45, h // 2), (w * 0.55, h // 2), (w * 0.9, h), (w * 0.1, h)],
              fill=(60, 60, 64))
    for i in range(6):
        y0 = h // 2 + (i * i + 1) * h // 80
        d.rectangle([w // 2 - 4 - i * 2, y0, w // 2 + 4 + i * 2, y0 + 8 + i * 6],
                    fill=(230, 230, 230))
    return img


def car(img, number="88", colour=(200, 30, 30), x=None, y=None, scale=1.0):
    """A side-on car with a race-number roundel and a stripe, as liveries have."""
    d = ImageDraw.Draw(img)
    w, h = img.size
    s = scale
    x = w // 2 - int(400 * s) if x is None else x
    y = int(h * 0.62) if y is None else y
    d.rounded_rectangle([x, y, x + 800 * s, y + 170 * s], radius=int(50 * s), fill=colour)
    d.polygon([(x + 190 * s, y), (x + 290 * s, y - 110 * s), (x + 560 * s, y - 110 * s),
               (x + 660 * s, y)], fill=colour)
    d.polygon([(x + 220 * s, y - 5 * s), (x + 305 * s, y - 95 * s), (x + 545 * s, y - 95 * s),
               (x + 630 * s, y - 5 * s)], fill=(40, 50, 70))
    d.rectangle([x, y + 70 * s, x + 800 * s, y + 95 * s], fill=(250, 250, 250))
    for cx in (x + 170 * s, x + 640 * s):
        d.ellipse([cx - 75 * s, y + 100 * s, cx + 75 * s, y + 250 * s], fill=(20, 20, 20))
        d.ellipse([cx - 35 * s, y + 140 * s, cx + 35 * s, y + 210 * s], fill=(150, 150, 150))
    if number:
        cx, cy, r = x + 420 * s, y + 85 * s, 70 * s
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(255, 255, 255), outline=(0, 0, 0),
                  width=max(1, int(5 * s)))
        d.text((cx, cy), number, fill=(0, 0, 0), font=font(int(90 * s)), anchor="mm")
    return img


def text(img, words, size=110, xy=None, fill=(255, 255, 255), box=(20, 20, 20)):
    """Words on a plate, readable (OCR should get them)."""
    d = ImageDraw.Draw(img)
    f = font(size)
    w, h = img.size
    xy = xy or (w // 2, h // 4)
    l, t, r, b = d.textbbox(xy, words, font=f, anchor="mm")
    d.rectangle([l - 30, t - 30, r + 30, b + 30], fill=box)
    d.text(xy, words, fill=fill, font=f, anchor="mm")
    return img


def star(img, cx=None, cy=None, r=300, fill=(220, 20, 30)):
    """A five-pointed star: the start-up check's picture (prompts.json "selftest")."""
    w, h = img.size
    cx = w // 2 if cx is None else cx
    cy = h // 2 if cy is None else cy
    pts = []
    for i in range(10):
        a = -math.pi / 2 + i * math.pi / 5
        rr = r if i % 2 == 0 else r * 0.4
        pts.append((cx + rr * math.cos(a), cy + rr * math.sin(a)))
    ImageDraw.Draw(img).polygon(pts, fill=fill)
    return img


def selftest_star():
    return star(Image.new("RGB", (W, H), (235, 235, 235)))


def png(img):
    out = io.BytesIO()
    img.save(out, "PNG")
    return out.getvalue()
