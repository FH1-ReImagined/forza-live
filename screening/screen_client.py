"""The community photos' operator API (forza_community.py) as the screening worker uses it.

The token goes only in the X-Admin-Token header and never into a log line, an exception or a URL.
Requests go straight to the server: no proxy from the environment (a proxy could add
X-Forwarded-For, which the server refuses, and would see the token), no redirects (urllib would
send the token to wherever a redirect points), no Origin header.
"""

import json
import re
import urllib.error
import urllib.request

ID_RE = re.compile(r"[A-Z2-7]{12}")
IMAGE_RE = re.compile(r"/admin/photos/img/[A-Z2-7]{12}\.(png|jpg)")
SHA_RE = re.compile(r"[0-9a-f]{64}")


class AdminError(Exception):
    def __init__(self, status, message):
        super().__init__("HTTP %s: %s" % (status, message))
        self.status = status


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise AdminError(code, "the server answered with a redirect; not followed")


def photo_record(p):
    """The fields the worker uses from a queue entry, checked; None for a malformed one. The
    owner's XUID is not kept."""
    if not isinstance(p, dict) or not ID_RE.fullmatch(str(p.get("id", ""))):
        return None
    image = str(p.get("image", ""))
    if not IMAGE_RE.fullmatch(image) or image.split("/")[-1].split(".")[0] != p["id"]:
        return None
    sha = str(p.get("sha256", ""))
    return {"id": p["id"], "image": image, "sha256": sha if SHA_RE.fullmatch(sha) else "",
            "title": str(p.get("title") or "")[:200], "gamertag": str(p.get("gamertag") or "")[:64],
            "status": str(p.get("status", "")), "w": p.get("w"), "h": p.get("h")}


class AdminClient:
    def __init__(self, base_url, token, timeout=30, max_image_bytes=16 << 20):
        if not re.fullmatch(r"https?://[^/?#@\s]+", base_url.rstrip("/")):
            raise ValueError("server: a base URL like http://172.17.0.1:21201")
        self.base = base_url.rstrip("/")
        self._token = token
        self.timeout = timeout
        self.max_image_bytes = max_image_bytes
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)

    def __repr__(self):
        return "AdminClient(%r)" % self.base  # never the token

    def _request(self, method, path, body=None, limit=1 << 20):
        headers = {"Accept": "application/json", "User-Agent": "forza-screening"}
        if self._token:
            headers["X-Admin-Token"] = self._token
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)
        try:
            with self._opener.open(req, timeout=self.timeout) as r:
                out = r.read(limit + 1)
                ctype = r.headers.get("Content-Type", "")
        except urllib.error.HTTPError as e:
            try:
                msg = json.loads(e.read(4096) or b"{}").get("error", "")
            except (ValueError, AttributeError, OSError):
                msg = ""
            raise AdminError(e.code, str(msg)[:200] or e.reason) from None
        except AdminError:
            raise
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise AdminError(None, "%s %s: %s" % (method, path, getattr(e, "reason", e))) from None
        if len(out) > limit:
            raise AdminError(None, "%s %s: more than %d bytes" % (method, path, limit))
        return out, ctype

    def _json(self, method, path, body=None):
        out, _ = self._request(method, path, body)
        try:
            return json.loads(out)
        except ValueError:
            raise AdminError(None, "%s %s: not JSON" % (method, path)) from None

    def queue(self):
        """The held photos, oldest first, as photo_record()s (malformed entries dropped)."""
        doc = self._json("GET", "/admin/photos/queue")
        photos = doc.get("photos") if isinstance(doc, dict) else None
        if not isinstance(photos, list):
            raise AdminError(None, "the queue is not {\"photos\": [...]}")
        return [r for r in map(photo_record, photos) if r is not None]

    def photos(self, status):
        doc = self._json("GET", "/admin/photos?status=" + status)
        photos = doc.get("photos") if isinstance(doc, dict) else None
        return [r for r in map(photo_record, photos or []) if r is not None]

    def image(self, photo):
        data, ctype = self._request("GET", photo["image"], limit=self.max_image_bytes)
        if ctype.split(";")[0].strip() not in ("image/png", "image/jpeg"):
            raise AdminError(None, "image %s: content type %r" % (photo["id"], ctype[:40]))
        return data

    def act(self, pid, action, note=None):
        """release, hide (or hold): the photo's record after it. AdminError 409 when its status no
        longer allows it (an operator decided first), 404 when it is gone."""
        if not ID_RE.fullmatch(pid) or action not in ("release", "hide", "hold"):
            raise ValueError("bad photo id or action")
        return self._json("POST", "/admin/photos/%s/%s" % (pid, action),
                          {"note": note} if note is not None else None)
