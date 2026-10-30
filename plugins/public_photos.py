"""Public JSON and image endpoints for completed player photo uploads."""

import json
import mimetypes
import os
import urllib.parse

import forza_clubs as clubs
import forza_photos as photos


MAX_LIMIT = 50


def json_response(status, value):
    return status, "application/json", json.dumps(value, separators=(",", ":")).encode()


def limit_from(url):
    value = urllib.parse.parse_qs(url.query).get("limit", ["24"])[0]
    try:
        return max(1, min(int(value), MAX_LIMIT))
    except ValueError:
        return 24


def public_photos():
    return [(fid, record) for fid, record in photos.shared_files() if record.get("done")]


def photo_item(fid, record):
    meta = record["meta"]
    owner = clubs.user_data(record["owner"])
    return {
        "id": str(fid),
        "title": meta.get("title") or record.get("name") or "Untitled photo",
        "createdAt": int(record["created"]),
        "rating": (meta["ratingSum"] / meta["ratings"]) if meta.get("ratings") else 0,
        "downloadCount": meta.get("downloads", 0),
        "owner": {"xuid": "%016X" % record["owner"], "gamertag": owner.get("wzGamerTag", "")},
        "previewUrl": "/api/v1/photos/content?id=%d&variant=preview" % fid,
        "imageUrl": "/api/v1/photos/content?id=%d&variant=image" % fid,
    }


def list_photos(server, url, headers, body, peer):
    query = urllib.parse.parse_qs(url.query)
    cursor = query.get("cursor", [None])[0]
    files = public_photos()
    if cursor:
        try:
            after = int(cursor)
        except ValueError:
            return json_response(400, {"error": "invalid cursor"})
        files = [(fid, record) for fid, record in files if fid < after]
    limit = limit_from(url)
    page = files[:limit]
    return json_response(200, {
        "items": [photo_item(fid, record) for fid, record in page],
        "nextCursor": str(page[-1][0]) if len(files) > limit else None,
    })


def content_type(path):
    with open(path, "rb") as source:
        magic = source.read(16)
    if magic.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if magic.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if magic.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    return mimetypes.guess_type(path)[0] or "application/octet-stream"


def photo_content(server, url, headers, body, peer):
    query = urllib.parse.parse_qs(url.query)
    try:
        fid = int(query.get("id", [""])[0])
    except ValueError:
        return 400, "text/plain", b"invalid photo id"
    record = photos.load()["files"].get(str(fid))
    if not record or "meta" not in record or not record.get("done"):
        return 404, "text/plain", b"photo not found"
    variant = query.get("variant", ["image"])[0]
    if variant not in ("preview", "image"):
        return 400, "text/plain", b"invalid photo variant"
    path = photos.file_path(fid, ".prev" if variant == "preview" else ".bin")
    try:
        with open(path, "rb") as source:
            data = source.read()
    except OSError:
        return 404, "text/plain", b"photo file not found"
    if not data:
        return 404, "text/plain", b"photo is empty"
    return 200, content_type(path), data


def register(server):
    server.plugins.route("GET", "/api/v1/photos", list_photos)
    server.plugins.route("GET", "/api/v1/photos/content", photo_content)
