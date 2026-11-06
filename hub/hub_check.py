"""The checks of an uploaded mod or texture pack (website #2), for the isolated scanning worker
(hub_worker.py). Standard library only. Every upload is untrusted: nothing in it is trusted to say
how big it is, what it holds or where it goes.

check(path, kind, work, limits) reads the zip at `path` and returns a Checked: either refused,
with the reason in words for the uploader, or the files to publish (their paths in the zip that
will be made, and where each was decompressed in `work`, counting every byte), with the
manifest.

The rules, on top of the launcher's own (launcher/forza_launcher/mods.py and texture_packs.py,
Add mod... and Add pack..., which a downloaded file must pass):
- A zip of stored or deflated entries only: no encryption, no other compression, no links, no
  device or other special files, at most `max_entries` entries and `max_unpacked` bytes once
  unpacked, no entry over `max_file`, no entry that unpacks to more than `max_ratio` times its
  compressed size (zip bombs). The sizes the zip states are not believed: every byte is counted
  as it is decompressed, and the zip is refused at the first byte over a limit.
- Names: relative, "/"-separated, printable ASCII without :*?"<>| or "\\", no "." or ".."
  part, no empty part, at most 255 bytes, unique without regard to case.
- The layout the launcher finds: the files at the zip's top or in its one folder.
- An allow-list of what may be in it, by kind:
  - mod: mod.toml (optional), read-me and licence files (.txt, .md) beside it, and media/ with
    files of the game's own types (GAME_TYPES) at the game's paths; no archive under media/
    (the game does not replace whole archives);
  - texture pack: <16 hex digits>.dds or .png textures (in folders too), checked as the game
    reads them (texture_header), pack.toml (optional) and .txt, .md and .toml files.
- No nested archive, executable or script, by name (REFUSED_TYPES) and by what the file starts
  with (MAGIC): a renamed .exe is refused like an .exe.
- mod.toml and pack.toml: UTF-8 TOML of at most 64 KiB; name, version, author and description
  are kept as text (cut to their lengths), nothing else.

The worker then scans the files with ClamAV and writes a new zip of only these files (repack):
fixed times, no comments, no extra fields, no permissions, in one folder named for the item.
"""

import os
import re
import stat
import struct
import tomllib
import unicodedata
import zipfile
import zlib
from dataclasses import dataclass, field

MOD, PACK = "mod", "pack"
KINDS = (MOD, PACK)
MAX_NAME = 255
CHUNK = 1 << 16
HEAD = 148           # bytes kept of each file: what it starts with, a texture's header
MANIFEST_MAX = 64 << 10
DOC_MAX = 1 << 20

# The file types of the game's media archives (their extensions, counted in the game's own
# archives, October 2026), which a mod may replace. Not .sh, .lua, .py or .html: scripts and
# pages are refused even where the game has a file of that type.
GAME_TYPES = frozenset((
    ".bin", ".bix", ".pgeo", ".fiz", ".xds", ".xml", ".carbin", ".soundscape", ".pvsz", ".str",
    ".tga", ".txt", ".bundle", ".bgf", ".bsg", ".fbf", ".fxobj", ".gr2", ".skinbin", ".fev",
    ".fsb", ".owt", ".dds", ".png", ".jpg", ".dt", ".xpr", ".ini", ".slt", ".sch", ".fmbin",
))
DOC_TYPES = frozenset((".txt", ".md"))
PACK_DOC_TYPES = frozenset((".txt", ".md", ".toml"))
FINGERPRINT = re.compile(r"[0-9a-fA-F]{16}\.(dds|png)", re.IGNORECASE)
# Never allowed, whatever the kind: archives, executables, scripts, installers, shortcuts.
REFUSED_TYPES = frozenset((
    ".zip", ".7z", ".rar", ".tar", ".gz", ".tgz", ".bz2", ".xz", ".zst", ".cab", ".iso", ".jar",
    ".apk", ".exe", ".dll", ".sys", ".scr", ".com", ".bat", ".cmd", ".ps1", ".psm1", ".vbs", ".vbe",
    ".js", ".jse", ".wsf", ".wsh", ".hta", ".msi", ".msp", ".lnk", ".url", ".reg", ".cpl", ".so",
    ".dylib", ".sh", ".bash", ".py", ".pyc", ".pl", ".rb", ".lua", ".php", ".html", ".htm", ".svg",
    ".xhtml", ".appimage", ".deb", ".rpm", ".desktop", ".command", ".app", ".pkg", ".dmg",
    ".docm", ".xlsm", ".pptm",
))
# What refused files start with: executables, archives, scripts.
MAGIC = (
    (b"MZ", "a Windows program"), (b"\x7fELF", "a Linux program"),
    (b"\xfe\xed\xfa\xce", "a macOS program"), (b"\xfe\xed\xfa\xcf", "a macOS program"),
    (b"\xce\xfa\xed\xfe", "a macOS program"), (b"\xcf\xfa\xed\xfe", "a macOS program"),
    (b"\xca\xfe\xba\xbe", "a macOS or Java program"), (b"#!", "a script"),
    (b"PK\x03\x04", "an archive"), (b"PK\x05\x06", "an archive"), (b"7z\xbc\xaf\x27\x1c", "an archive"),
    (b"Rar!\x1a\x07", "an archive"), (b"\x1f\x8b", "an archive"), (b"BZh", "an archive"),
    (b"\xfd7zXZ\x00", "an archive"), (b"\x28\xb5\x2f\xfd", "an archive"), (b"MSCF", "an archive"),
    (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", "an Office or installer file"), (b"%PDF", "a PDF"),
    (b"<!DOCTYPE html", "a web page"), (b"<html", "a web page"), (b"<script", "a script"),
    (b"<?php", "a script"),
)
BAD_CHARS = set(':*?"<>|\\')


@dataclass
class Limits:
    max_entries: int = 20_000
    max_unpacked: int = 4 << 30
    max_file: int = 1 << 30
    max_ratio: int = 100          # an entry that unpacks to more than this times its packed size
    ratio_floor: int = 1 << 20    # entries smaller than this unpacked are not judged by ratio


@dataclass
class Checked:
    ok: bool
    reason: str = ""                                  # why not, for the uploader
    files: list = field(default_factory=list)         # [(path inside the item's folder, bytes)]
    manifest: dict = field(default_factory=dict)      # name, version, author, description
    unpacked: int = 0
    textures: int = 0


class Refused(Exception):
    pass


def refuse(why):
    raise Refused(why)


def _shown(name):
    """A name from the zip, safe to put in a message (no control or format characters, cut)."""
    text = "".join(c if c.isprintable() else "?" for c in name)
    return text[:80] + ("..." if len(text) > 80 else "")


def check_name(name):
    """The entry's path, checked, without a trailing "/", or Refused."""
    if not name or len(name.encode("utf-8", "replace")) > MAX_NAME:
        refuse('a file name is empty or longer than 255 characters ("%s")' % _shown(name))
    if any(not (0x20 <= ord(c) <= 0x7E) or c in BAD_CHARS for c in name):
        refuse('"%s" has characters the game cannot use: plain letters, digits, spaces and . _ - '
               '( ) only, with / between folders' % _shown(name))
    if name.startswith("/") or (len(name) > 1 and name[1] == ":"):
        refuse('"%s" is an absolute path' % _shown(name))
    trimmed = name[:-1] if name.endswith("/") else name
    parts = trimmed.split("/")
    if any(p in ("", ".", "..") for p in parts):
        refuse('"%s" leaves its folder or has an empty part' % _shown(name))
    if any(p != p.strip() or p.endswith(".") for p in parts):
        refuse('"%s" has a part that ends in a space or a dot' % _shown(name))
    return trimmed


def suffix(name):
    base = name.rsplit("/", 1)[-1]
    return "." + base.rsplit(".", 1)[-1].lower() if "." in base else ""


def sniff(head):
    """What a file is when its first bytes are those of a refused type, else None."""
    text = head.lstrip(b"\xef\xbb\xbf \t\r\n").lower()
    for magic, what in MAGIC:
        if head.startswith(magic) or (magic[:1] == b"<" and text.startswith(magic.lower())):
            return what
    return None


def read_entry(z, info, limits, budget, out):
    """Decompresses an entry into the open file `out`, counting every byte: Refused at the first
    byte over the file's limit, the ratio or what is left of the whole budget ([bytes left]).
    The entry's first HEAD bytes, and its size."""
    if info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
        refuse('"%s" uses a compression the launcher cannot unpack: repack it as a normal zip'
               % _shown(info.filename))
    if info.flag_bits & 0x1:
        refuse('"%s" is encrypted' % _shown(info.filename))
    ratio_cap = max(limits.ratio_floor, info.compress_size * limits.max_ratio)
    head, n = b"", 0
    try:
        with z.open(info) as f:
            while True:
                chunk = f.read(CHUNK)
                if not chunk:
                    break
                n += len(chunk)
                if n > limits.max_file:
                    refuse('"%s" is bigger than %d MB' % (_shown(info.filename), limits.max_file >> 20))
                if n > budget[0]:
                    refuse("it unpacks to more than %d MB" % (limits.max_unpacked >> 20))
                if n > ratio_cap:
                    refuse('"%s" unpacks to far more than its packed size (a zip bomb?)' % _shown(info.filename))
                if len(head) < HEAD:
                    head += chunk[:HEAD - len(head)]
                out.write(chunk)
    except (zipfile.BadZipFile, zlib.error, EOFError, RuntimeError, NotImplementedError, ValueError, OSError,
            struct.error) as e:
        refuse('"%s" could not be read (%s)' % (_shown(info.filename), type(e).__name__))
    budget[0] -= n
    return head, n


CD_ENTRY_MAX = 46 + MAX_NAME + 64   # a central directory entry: its name and a zip64 extra field


def directory_size(path, limits):
    """Refused when the zip's end record (or its zip64 form) says it has more entries, or a
    bigger central directory, than the limits allow: zipfile reads the whole directory into
    memory before anything else can be checked."""
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        f.seek(max(0, size - (65535 + 22)))
        tail = f.read()
        i = tail.rfind(b"PK\x05\x06")
        if i < 0 or len(tail) - i < 22:
            refuse("it is not a zip file that can be read (no end record)")
        count, cd_size = struct.unpack_from("<H", tail, i + 10)[0], struct.unpack_from("<I", tail, i + 12)[0]
        j = tail.rfind(b"PK\x06\x07", 0, i)  # the zip64 end record's locator
        if j >= 0 and i - j == 20:
            offset = struct.unpack_from("<Q", tail, j + 8)[0]
            if offset > size - 56:
                refuse("it is not a zip file that can be read (bad zip64 record)")
            f.seek(offset)
            rec = f.read(56)
            if rec[:4] != b"PK\x06\x06" or len(rec) < 56:
                refuse("it is not a zip file that can be read (bad zip64 record)")
            count, cd_size = struct.unpack_from("<Q", rec, 32)[0], struct.unpack_from("<Q", rec, 40)[0]
    if count > limits.max_entries:
        refuse("it holds %d entries, more than %d" % (count, limits.max_entries))
    if cd_size > limits.max_entries * CD_ENTRY_MAX or cd_size > size:
        refuse("its list of files is too big")


def entries(path, limits):
    """The zip's file entries [(name, info)], checked: names, types of entry, count."""
    try:
        directory_size(path, limits)
        z = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError, ValueError, EOFError, struct.error) as e:
        refuse("it is not a zip file that can be read (%s)" % type(e).__name__)
    try:
        return z, _entries(z, limits)
    except Refused:
        z.close()
        raise


def _entries(z, limits):
    infos = z.infolist()
    if len(infos) > limits.max_entries:
        refuse("it holds %d entries, more than %d" % (len(infos), limits.max_entries))
    files, seen, folders = [], {}, set()
    for info in infos:
        name = check_name(info.filename)
        mode = info.external_attr >> 16
        kind = stat.S_IFMT(mode)
        if kind == stat.S_IFLNK:
            refuse('"%s" is a link' % _shown(name))
        if kind not in (0, stat.S_IFREG, stat.S_IFDIR):
            refuse('"%s" is not a plain file or folder' % _shown(name))
        is_dir = info.filename.endswith("/") or kind == stat.S_IFDIR or (info.external_attr & 0x10)
        if is_dir:
            if info.file_size:
                refuse('"%s" is a folder with data in it' % _shown(name))
            folders.add(name.lower())
            continue
        low = name.lower()
        if low in seen:
            refuse('"%s" and "%s" differ only in case' % (_shown(name), _shown(seen[low])))
        seen[low] = name
        files.append((name, info))
    for name, _ in files:
        if name.lower() in folders:
            refuse('"%s" is both a file and a folder' % _shown(name))
        prefix = ""
        for part in name.lower().split("/")[:-1]:
            prefix = prefix + part
            if prefix in seen:
                refuse('"%s" is both a file and a folder' % _shown(seen[prefix]))
            prefix += "/"
    return files


def root_of(names, kind):
    """The folder of the zip the item is in: "" (its top) or its one folder ("name/")."""
    tops = {n.split("/", 1)[0] for n in names}

    def is_root(prefix):
        if kind == MOD:
            return any(n.lower().startswith(prefix.lower() + "media/") or n.lower() == prefix.lower() + "mod.toml"
                       for n in names)
        return any(n.startswith(prefix) and FINGERPRINT.fullmatch(n.rsplit("/", 1)[-1]) for n in names)

    if kind == MOD and is_root(""):
        return ""
    if len(tops) == 1 and all("/" in n for n in names):
        only = next(iter(tops)) + "/"
        if is_root(only):
            return only
    if kind == PACK and is_root(""):
        return ""
    refuse("there is no mod in it: a mod holds a media folder (and usually mod.toml), at the zip's top "
           "or in its one folder" if kind == MOD else
           "there is no texture pack in it: no .dds or .png files named by a texture's fingerprint "
           "(16 hexadecimal digits)")


def allowed(rel, kind):
    """Why the file at `rel` (inside the item's folder) may not be in it, or None."""
    low, ext = rel.lower(), suffix(rel)
    if ext in REFUSED_TYPES:
        return '"%s": %s files are not allowed (no archives, programs or scripts)' % (_shown(rel), ext)
    if kind == MOD:
        if low == "mod.toml":
            return None
        if "/" not in low:
            return None if ext in DOC_TYPES else ('"%s": only mod.toml, .txt and .md files may be beside the '
                                                  'media folder' % _shown(rel))
        if not low.startswith("media/"):
            return '"%s": a mod\'s files go under media/, at the game\'s paths' % _shown(rel)
        if ext not in GAME_TYPES:
            return '"%s": %s is not one of the game\'s file types' % (_shown(rel), ext or "a file without a type")
        return None
    base = low.rsplit("/", 1)[-1]
    if FINGERPRINT.fullmatch(base):
        return None
    if ext in PACK_DOC_TYPES:
        return None
    return ('"%s": a texture pack holds <fingerprint>.dds or .png textures, pack.toml and .txt or .md '
            'files only' % _shown(rel))


# ---- The game's texture reader (the launcher's texture_packs.read_texture_header) ----

_DDS_FOURCC = {b"DXT1": "BC1", b"DXT2": "BC2", b"DXT3": "BC2", b"DXT4": "BC3", b"DXT5": "BC3",
               b"ATI1": "BC4", b"BC4U": "BC4", b"ATI2": "BC5", b"BC5U": "BC5"}
_DXGI = {71: "BC1", 72: "BC1", 74: "BC2", 75: "BC2", 77: "BC3", 78: "BC3", 80: "BC4", 83: "BC5",
         98: "BC7", 99: "BC7", 28: "RGBA8", 29: "RGBA8", 87: "BGRA8", 91: "BGRA8", 61: "R8", 49: "RG8"}
_BLOCK_BYTES = {"BC1": 8, "BC4": 8, "BC2": 16, "BC3": 16, "BC5": 16, "BC7": 16}
_TEXEL_BYTES = {"RGBA8": 4, "BGRA8": 4, "R8": 1, "RG8": 2}
MAX_TEXTURE = 16384


def texture_header(head, size):
    """Why the game would not use this .dds or .png file (its first 148 bytes and its size), or
    None."""
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        if len(head) < 33 or head[12:16] != b"IHDR":
            return "a damaged PNG file"
        w, h, depth, colour = struct.unpack(">IIBB", head[16:26])
        if depth not in (1, 2, 4, 8, 16) or colour not in (0, 2, 3, 4, 6):
            return "a damaged PNG file"
        if not w or not h or w > MAX_TEXTURE or h > MAX_TEXTURE:
            return "its size %dx%d is over %d" % (w, h, MAX_TEXTURE)
        return None
    if head[:4] != b"DDS ":
        return "neither a DDS nor a PNG file"
    if len(head) < 128 or struct.unpack_from("<I", head, 4)[0] != 124:
        return "a damaged DDS file"
    flags, h, w = struct.unpack_from("<III", head, 8)
    depth = struct.unpack_from("<I", head, 24)[0]
    pixel_flags, four_cc, bits = struct.unpack_from("<I4sI", head, 80)
    masks = struct.unpack_from("<III", head, 92)
    caps2 = struct.unpack_from("<I", head, 112)[0]
    offset, fmt = 128, None
    if caps2 & 0x200:
        return "a cube map (not supported yet)"
    if caps2 & 0x200000 or (flags & 0x800000 and depth > 1):
        return "a volume texture (not supported yet)"
    if pixel_flags & 0x4:
        if four_cc in _DDS_FOURCC:
            fmt = _DDS_FOURCC[four_cc]
        elif four_cc == b"DX10":
            offset = 148
            if len(head) < 148:
                return "a damaged DDS file"
            dxgi, dimension, misc, array = struct.unpack_from("<IIII", head, 128)
            if dimension != 3 or array > 1 or misc & 0x4:
                return "not a single 2D texture"
            if dxgi not in _DXGI:
                return "DXGI format %d, which the game does not read" % dxgi
            fmt = _DXGI[dxgi]
        else:
            return "a DDS format the game does not read"
    elif pixel_flags & 0x40 and bits == 32:
        if masks == (0xFF, 0xFF00, 0xFF0000):
            fmt = "RGBA8"
        elif masks == (0xFF0000, 0xFF00, 0xFF):
            fmt = "BGRA8"
        else:
            return "a 32-bit channel order the game does not read"
    elif pixel_flags & 0x20000 and bits == 8:
        fmt = "R8"
    elif pixel_flags & 0x20000 and pixel_flags & 0x1 and bits == 16:
        fmt = "RG8"
    else:
        return "a pixel format the game does not read"
    if not w or not h or w > MAX_TEXTURE or h > MAX_TEXTURE:
        return "its size %dx%d is over %d" % (w, h, MAX_TEXTURE)
    blocks = _BLOCK_BYTES.get(fmt)
    base = ((w + 3) // 4) * ((h + 3) // 4) * blocks if blocks else w * h * _TEXEL_BYTES[fmt]
    if size < offset + base:
        return "shorter than its header says"
    return None


# ---- Manifests ----

FIELDS = {"name": 60, "version": 20, "author": 40, "description": 1000}


def clean_text(value, limit):
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return ""
    text = "".join(c for c in str(value) if unicodedata.category(c) not in ("Cf", "Cs", "Co"))
    text = "".join(c if c.isprintable() or c == "\n" else " " for c in text)
    lines = [" ".join(line.split()) for line in text.splitlines()]
    return "\n".join(line for line in lines if line).strip()[:limit]


def manifest(data, name):
    if len(data) > MANIFEST_MAX:
        refuse("%s is bigger than 64 KB" % name)
    try:
        info = tomllib.loads(data.decode("utf-8-sig"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
        refuse("%s could not be read (%s)" % (name, _shown(str(e))))
    return {k: clean_text(info.get(k), n) for k, n in FIELDS.items() if clean_text(info.get(k), n)}


# ---- The check ----

def check(path, kind, work, limits=None):
    """Checks the zip at `path` as a `kind`; the files it would publish are decompressed into the
    empty folder `work`, as 0, 1, 2... (never under a name from the zip)."""
    limits = limits or Limits()
    if kind not in KINDS:
        return Checked(False, "unknown kind")
    try:
        z, found = entries(path, limits)
        with z:
            root = root_of([n for n, _ in found], kind)
            budget = [limits.max_unpacked]
            files, meta, textures = [], {}, 0
            for name, info in sorted(found, key=lambda e: e[0]):
                if not name.startswith(root):
                    refuse('"%s" is outside the %s\'s folder' % (_shown(name), "mod" if kind == MOD else "pack"))
                rel = name[len(root):]
                why = allowed(rel, kind)
                if why:
                    refuse(why)
                disk = os.path.join(work, str(len(files)))
                with open(disk, "xb") as out:
                    head, size = read_entry(z, info, limits, budget, out)
                what = sniff(head[:64])
                if what:
                    refuse('"%s" is %s, which is not allowed' % (_shown(rel), what))
                low = rel.lower()
                if low in ("mod.toml", "pack.toml"):
                    with open(disk, "rb") as f:
                        meta = manifest(f.read(MANIFEST_MAX + 1), rel)
                elif suffix(rel) in (".txt", ".md") and size > DOC_MAX and "/" not in rel:
                    refuse('"%s" is bigger than 1 MB' % _shown(rel))
                if kind == PACK and FINGERPRINT.fullmatch(low.rsplit("/", 1)[-1]):
                    problem = texture_header(head, size)
                    if problem:
                        refuse('"%s" would not load: %s' % (_shown(rel), problem))
                    textures += 1
                elif kind == MOD and low.startswith("media/") and suffix(rel) in (".dds", ".png"):
                    if not (head.startswith(b"DDS ") or head.startswith(b"\x89PNG\r\n\x1a\n")):
                        refuse('"%s" is not the image its name says' % _shown(rel))
                files.append((rel, disk, size))
            if kind == MOD and not any(r.lower().startswith("media/") for r, _, _ in files):
                refuse("there are no files under media/")
            if kind == PACK and not textures:
                refuse("there are no textures in it")
            return Checked(True, files=files, manifest=meta, unpacked=limits.max_unpacked - budget[0],
                           textures=textures)
    except Refused as e:
        return Checked(False, str(e))
    except (zipfile.BadZipFile, zlib.error, EOFError, ValueError, RuntimeError, struct.error, OSError,
            MemoryError, OverflowError) as e:
        return Checked(False, "it could not be read (%s)" % type(e).__name__)


FIXED_TIME = (1980, 1, 1, 0, 0, 0)


def repack(checked, folder, out):
    """Writes the checked files as a new zip at `out` (a path or a file), all in `folder`/: fixed
    times, no comment, no extra fields, plain file permissions."""
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6, allowZip64=True) as z:
        for rel, disk, _ in checked.files:
            info = zipfile.ZipInfo(folder + "/" + rel, FIXED_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            info.create_system = 3
            with open(disk, "rb") as src, z.open(info, "w", force_zip64=True) as dst:
                while True:
                    chunk = src.read(CHUNK)
                    if not chunk:
                        break
                    dst.write(chunk)


def folder_name(title, fallback):
    """The download's folder (and file) name: the launcher takes it as the mod's or pack's
    folder. Letters, digits, - and _ only, at most 40, never starting with a dot."""
    text = unicodedata.normalize("NFKD", title or "").encode("ascii", "ignore").decode()
    text = re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-")[:40].strip("-")
    return text or fallback
