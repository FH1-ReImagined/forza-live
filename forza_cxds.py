"""Design previews: the game's cxds thumbnails as PNG pictures, standard library only.

A design's preview (previewData of StorefrontLivery.Create and StorefrontLayerGroup.Create,
forza_designs.py) is the texture the game keeps as the design's thumb.xdc: a paint job's is
288x160, a vinyl group's 256x128 (TU4, October 2026). Its layout:

- "cxds", then four big-endian words: version 2, the texture's size, the size of what follows
  (the file's length minus 20) and the texture's size again;
- the texture, raw-deflated: 52 bytes of header, whose words at 0x20 are the format (its low
  6 bits 6, k_8_8_8_8, and bit 0x80, tiled: 0x86 in the game's previews) and the size (width - 1
  and height - 1, 13 bits each); then the pixels, 4 bytes each (A, R, G, B), in the Xbox 360's
  tiled order, rows padded to a multiple of 32 pixels.

The pixels are dark as stored: the game shows them with a gamma of about 1/2.2 (seen in the
game against the same bytes, October 2026), so the PNG has that gamma applied (GAMMA) and is
meant to be shown as sRGB. Alpha is dropped. Anything that is not exactly such a texture is
refused (BadPreview): a wrong magic or version, sizes that do not match, damaged or trailing
compressed data, another format, a size over MAX_SIDE, pixels that are not all there.

The decoder is a port of tools/xdc_decode.py in the game's repository
(FH1-ReImagined/ForzaHorizon1-ReImagined, GPL-3.0-or-later; by Dyotson and Sean O'Connor),
with the same checks; this server is AGPL-3.0-or-later, which section 13 of both licences
allows. Added here: the gamma, the RGB PNG, a smaller size limit, and the tiled order worked
out once per texture size (a preview takes a few milliseconds).
"""

import functools
import operator
import struct
import zlib

import forza_community as community

MAX_PACKED = 1 << 20         # bytes of a cxds file (the storefront takes at most 153,600)
MAX_TEXTURE = 4 << 20        # bytes of the inflated texture
MAX_SIDE = 1024              # pixels: a preview is 288 or 256 wide
HEADER = 52                  # bytes before the pixels
FORMAT_8888, TILED = 6, 0x80
GAMMA = 1 / 2.2
LUT = bytes(round(255 * (v / 255) ** GAMMA) for v in range(256))
BPB_LOG2 = 2                 # log2 of the bytes per pixel (8_8_8_8)


class BadPreview(ValueError):
    """Not a preview this server converts."""


def tiled_offset(x, y, pitch):
    """The byte offset of pixel (x, y) in an Xbox 360 tiled 32-bit texture `pitch` pixels wide
    (xdc_decode.tiled_offset)."""
    macro = ((y >> 5) * (pitch >> 5)) << (BPB_LOG2 + 7)
    micro = ((y & 6) << 2) << BPB_LOG2
    base = (macro + ((micro & ~0xF) << 1) + (micro & 0xF)
            + ((y & 8) << (3 + BPB_LOG2)) + ((y & 1) << 4))
    macro = (x >> 5) << (BPB_LOG2 + 7)
    micro = (x & 7) << BPB_LOG2
    off = base + macro + ((micro & ~0xF) << 1) + (micro & 0xF)
    return (((off & ~0x1FF) << 3) + ((off & 0x1C0) << 2) + (off & 0x3F)
            + ((y & 16) << 7) + (((((y & 8) >> 2) + (x >> 3)) & 3) << 6))


@functools.lru_cache(maxsize=8)
def layout(width, height):
    """(red, green, blue getters, bytes needed) of a texture's size: itemgetters that take
    the pixels' red, green and blue bytes in row order from the tiled data."""
    pitch = (width + 31) & ~31
    offsets = [tiled_offset(x, y, pitch) for y in range(height) for x in range(width)]

    def getter(channel):
        if len(offsets) == 1:  # an itemgetter of one item gives the item, not a tuple
            return lambda pixels, at=offsets[0] + channel: (pixels[at],)
        return operator.itemgetter(*[o + channel for o in offsets])
    return getter(1), getter(2), getter(3), max(offsets) + 4


def texture(data):
    """(width, height, tiled pixels) of a cxds file; BadPreview for anything else."""
    if not isinstance(data, (bytes, bytearray)) or not 20 <= len(data) <= MAX_PACKED:
        raise BadPreview("not a preview of a size taken here")
    magic, version, size, packed, again = struct.unpack_from(">4s4I", data)
    if magic != b"cxds" or version != 2:
        raise BadPreview("not a version 2 cxds texture")
    if packed != len(data) - 20:
        raise BadPreview("the packed size does not match the file")
    if not HEADER < size <= MAX_TEXTURE or again != size:
        raise BadPreview("bad texture size")
    inflater = zlib.decompressobj(-15)
    try:
        body = inflater.decompress(bytes(data[20:]), size + 1)
    except zlib.error:
        raise BadPreview("damaged compressed data") from None
    if (len(body) != size or not inflater.eof or inflater.unused_data
            or inflater.unconsumed_tail):
        raise BadPreview("the texture is not the size it says")
    fmt, dims = struct.unpack_from(">II", body, 0x20)
    if fmt & 0x3F != FORMAT_8888 or not fmt & TILED:
        raise BadPreview("format 0x%X (only tiled 8_8_8_8)" % fmt)
    width, height = (dims & 0x1FFF) + 1, ((dims >> 13) & 0x1FFF) + 1
    if width > MAX_SIDE or height > MAX_SIDE:
        raise BadPreview("size %dx%d" % (width, height))
    return width, height, body[HEADER:]


def decode(data):
    """(width, height, RGB bytes in row order, gamma applied) of a cxds file; BadPreview for
    anything else."""
    width, height, pixels = texture(data)
    red, green, blue, needed = layout(width, height)
    if needed > len(pixels):
        raise BadPreview("the pixels are not all there")
    rgb = bytearray(width * height * 3)
    rgb[0::3], rgb[1::3], rgb[2::3] = bytes(red(pixels)), bytes(green(pixels)), bytes(blue(pixels))
    return width, height, bytes(rgb).translate(LUT)


def to_png(data):
    """(PNG, width, height) of a cxds file: an 8-bit RGB PNG with IHDR, IDAT and IEND only
    (forza_community.png). BadPreview for anything else."""
    width, height, rgb = decode(data)
    stride = width * 3
    rows = [rgb[i:i + stride] for i in range(0, len(rgb), stride)]
    return community.png(rows, width, height), width, height
