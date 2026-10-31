#!/usr/bin/env python3
"""包子漫画本地站。

原站壳是 Astro，真资源在：
  搜索/首页  baozimh.org HTML
  目录      v2.apikk.top/api/manga/get
  章节图    v2.apikk.top/api/v2/chapter/getinfo  （加密 blob，本地 decoder 解开）
  图源      c-nd2/c-nd3.6wm.top

目录和解码结果进内存/磁盘缓存，二次打开不再打上游。
图片走代理流，不落盘。点下载才打 zip。

  python3 server.py
  http://127.0.0.1:8788
"""
from __future__ import annotations

import gzip
import hashlib
import html as html_lib
import io
import json
import os
import re
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from decoder import decode_images
import hipmh
import kmoe

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
DATA = ROOT / "data"
DATA.mkdir(exist_ok=True)
CTX = ssl._create_unverified_context()
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)
PORT = int(os.environ.get("PORT", "8788"))
HOST = os.environ.get("HOST") or ("0.0.0.0" if "PORT" in os.environ else "127.0.0.1")
SITE = "https://baozimh.org"
API = "https://v2.apikk.top"
CDN = {
    2: "https://c-nd2-1.6wm.top",
    "default": "https://c-nd2-1.6wm.top",
}
TTL_HOME = 1800
TTL_SEARCH = 1800
TTL_MANGA = 900
TTL_CHAPTER = 86400 * 7
STALE = 86400 * 3

_LOCK = threading.Lock()
_MEM: dict[str, tuple[float, object]] = {}
_OPENER = urllib.request.build_opener(urllib.request.HTTPSHandler(context=CTX))
_INDEX_HTML: bytes | None = None
_INDEX_MTIME = 0.0
HOME_MARK = b"window.__HOME__=null;"

CARD_RE = re.compile(
    r'<a href="(/manga/([^"/\s]+))"[^>]*>'
    r"[\s\S]{0,1500}?<img[^>]+src=\"(https://[^\"]+)\"[\s\S]{0,500}?"
    r"<h3[^>]*>([^<]+)</h3>",
    re.I,
)


def http_get(url: str, headers: dict | None = None, timeout: int = 25) -> bytes:
    h = {
        "User-Agent": UA,
        "Accept": "*/*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Referer": SITE + "/",
        "Origin": SITE,
    }
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h)
    with _OPENER.open(req, timeout=timeout) as resp:
        return resp.read()


def json_get(url: str) -> dict:
    raw = http_get(url, headers={"Accept": "application/json"})
    return json.loads(raw.decode("utf-8", "replace"))


def _disk_path(kind: str, key: str) -> Path:
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
    ascii_part = re.sub(r"[^a-zA-Z0-9._-]+", "_", key).strip("_")[:40]
    name = (ascii_part + "_" if ascii_part else "") + digest
    folder = DATA / kind
    folder.mkdir(exist_ok=True)
    return folder / (name + ".json")


def cache_get(kind: str, key: str, ttl: int, allow_stale: bool = False):
    now = time.time()
    ck = f"{kind}:{key}"
    with _LOCK:
        hit = _MEM.get(ck)
        if hit and now - hit[0] < ttl:
            return hit[1]
        if allow_stale and hit and now - hit[0] < STALE:
            return hit[1]
    path = _disk_path(kind, key)
    if path.is_file():
        try:
            obj = json.loads(path.read_text("utf-8"))
            ts = float(obj.get("_ts") or 0)
            data = obj.get("data")
            with _LOCK:
                _MEM[ck] = (ts, data)
            if now - ts < ttl or (allow_stale and now - ts < STALE):
                return data
        except Exception:
            pass
    return None


def cache_set(kind: str, key: str, data):
    now = time.time()
    ck = f"{kind}:{key}"
    with _LOCK:
        _MEM[ck] = (now, data)
        if len(_MEM) > 800:
            oldest = sorted(_MEM.items(), key=lambda kv: kv[1][0])[:200]
            for k, _ in oldest:
                _MEM.pop(k, None)
    try:
        _disk_path(kind, key).write_text(
            json.dumps({"_ts": now, "data": data}, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception:
        pass


def parse_cards(html: str, limit: int = 60) -> list[dict]:
    items = []
    seen = set()
    for m in CARD_RE.finditer(html):
        slug = m.group(2)
        if slug in seen:
            continue
        seen.add(slug)
        items.append(
            {
                "slug": slug,
                "title": html_lib.unescape(m.group(4)).strip(),
                "cover": m.group(3),
                "href": m.group(1),
            }
        )
        if len(items) >= limit:
            break
    return items


def parse_home_sections(html: str) -> dict[str, list[dict]]:
    marks = [
        ("recent", "近期更新"),
        ("hot_update", "熱門更新"),
        ("rank", "人氣排行"),
        ("new", "最新上架"),
    ]
    found = []
    for key, label in marks:
        i = html.find(label)
        if i >= 0:
            found.append((i, key, label))
    found.sort()
    out: dict[str, list[dict]] = {k: [] for k, _ in marks}
    for idx, (pos, key, _label) in enumerate(found):
        end = found[idx + 1][0] if idx + 1 < len(found) else min(len(html), pos + 12000)
        out[key] = parse_cards(html[pos:end], 24)
    return out


def fetch_list_page(path: str) -> list[dict]:
    html = http_get(SITE + path, headers={"Accept": "text/html"}).decode("utf-8", "replace")
    return parse_cards(html, 36)


def home_payload() -> dict:
    cached = cache_get("home", "index", TTL_HOME, allow_stale=True)
    if cached:
        return cached
    extras = {}
    hip_rank: list = []
    with ThreadPoolExecutor(max_workers=4) as pool:
        f_html = pool.submit(lambda: http_get(SITE + "/", headers={"Accept": "text/html"}).decode("utf-8", "replace"))
        f_hip = pool.submit(hipmh.popularity)
        html = f_html.result()
        sections = parse_home_sections(html)
        need = []
        if not sections.get("rank"):
            need.append(("hots", "/hots", "rank"))
        if not sections.get("hot_update"):
            need.append(("dayup", "/dayup", "hot_update"))
        if not sections.get("new"):
            need.append(("newss", "/newss", "new"))
        futs = {pool.submit(fetch_list_page, path): (list_key, sec_key) for list_key, path, sec_key in need}
        for fut in as_completed(futs):
            list_key, sec_key = futs[fut]
            try:
                extras[list_key] = fut.result()
                sections[sec_key] = extras[list_key]
            except Exception:
                extras[list_key] = []
        try:
            hip_rank = f_hip.result()
        except Exception:
            hip_rank = []
    extras["hip_rank"] = hip_rank
    if hip_rank:
        seen = {((x.get("title") or "").strip()) for x in hip_rank}
        rest = [x for x in (sections.get("rank") or []) if (x.get("title") or "").strip() not in seen]
        sections["rank"] = hip_rank + rest
    payload = {"code": 0, "sections": sections, "lists": extras}
    cache_set("home", "index", payload)
    return payload


def work_slug_safe(mid: str) -> str:
    return "hip-" + str(mid)


def is_hip_request(slug: str = "", mid: str = "", cid: str = "", src: str = "") -> bool:
    if (src or "") == "hip":
        return True
    if str(slug or "").startswith("hip-") or str(mid or "").startswith("hip-"):
        return True
    c = str(cid or "")
    return bool(c) and not c.isdigit() and not str(slug or "").startswith("kmoe-")


def is_kmoe_request(slug: str = "", src: str = "") -> bool:
    if (src or "") == "kmoe":
        return True
    return str(slug or "").startswith("kmoe-")


def search_merged(keyword: str, page: int = 1) -> dict:
    key = f"mix_{keyword}_{page}"
    cached = cache_get("search", key, TTL_SEARCH, allow_stale=True)
    if cached:
        return cached
    errs: list[str] = []
    hip: dict = {"list": [], "total": 0}
    bz: dict = {"list": [], "total": 0}
    km: dict = {"list": [], "total": 0}

    def _hip():
        return hipmh.search(keyword, page)

    def _bz():
        return search_site(keyword, page)

    def _km():
        return kmoe.search(keyword, page)

    with ThreadPoolExecutor(max_workers=3) as pool:
        f_hip = pool.submit(_hip)
        f_bz = pool.submit(_bz)
        f_km = pool.submit(_km)
        try:
            hip = f_hip.result()
        except Exception as e:
            errs.append("嬉皮: " + str(e))
        try:
            bz = f_bz.result()
        except Exception as e:
            errs.append("包子: " + str(e))
        try:
            km = f_km.result()
        except Exception as e:
            errs.append("Kmoe: " + str(e))
    for it in bz.get("list") or []:
        it.setdefault("source", "bz")
    merged = []
    seen = set()
    for it in (hip.get("list") or []) + (km.get("list") or []) + (bz.get("list") or []):
        slug = it.get("slug") or ""
        if slug in seen:
            continue
        seen.add(slug)
        merged.append(it)
    payload = {
        "code": 0,
        "keyword": keyword,
        "page": page,
        "total": int(hip.get("total") or 0) + int(bz.get("total") or 0) + int(km.get("total") or 0),
        "list": merged,
        "sources": {
            "hip": int(hip.get("total") or 0),
            "bz": int(bz.get("total") or 0),
            "kmoe": int(km.get("total") or 0),
        },
    }
    if merged:
        for it in (hip.get("list") or []) + (km.get("list") or []):
            if it.get("slug"):
                cache_set(
                    "slug",
                    it["slug"],
                    {
                        "mid": it.get("mid") or "",
                        "slug": it["slug"],
                        "title": it.get("title") or "",
                        "cover": it.get("cover") or "",
                        "source": it.get("source") or "",
                    },
                )
        cache_set("search", key, payload)
        return payload
    if errs:
        raise RuntimeError("；".join(errs))
    return payload


def _fill_hip_meta(full: dict) -> dict:
    slug = full.get("slug") or work_slug_safe(full.get("id") or "")
    meta = cache_get("slug", slug, TTL_CHAPTER, allow_stale=True) or {}
    if not full.get("title") or str(full.get("title") or "").startswith("嬉皮 "):
        full["title"] = meta.get("title") or full.get("title") or ""
    if not full.get("cover"):
        full["cover"] = meta.get("cover") or ""
    return full


def hip_manga(slug: str) -> dict:
    mid = hipmh.numeric_mid(slug[4:] if slug.startswith("hip-") else slug)
    cached = cache_get("manga", "hip_" + mid, TTL_MANGA, allow_stale=True)
    if cached:
        return _fill_hip_meta(dict(cached))
    full = _fill_hip_meta(hipmh.manga(mid))
    cache_set("manga", "hip_" + mid, full)
    if full.get("slug"):
        cache_set(
            "slug",
            full["slug"],
            {
                "mid": full["id"],
                "slug": full["slug"],
                "title": full.get("title") or "",
                "cover": full.get("cover") or "",
                "source": "hip",
            },
        )
    return full


def kmoe_manga(slug: str) -> dict:
    bid = kmoe.numeric_id(slug[5:] if slug.startswith("kmoe-") else slug)
    cached = cache_get("manga", "kmoe_" + bid, TTL_MANGA, allow_stale=True)
    if cached:
        return dict(cached)
    full = kmoe.manga(bid)
    cache_set("manga", "kmoe_" + bid, full)
    if full.get("slug"):
        cache_set(
            "slug",
            full["slug"],
            {
                "mid": full["id"],
                "slug": full["slug"],
                "title": full.get("title") or "",
                "cover": full.get("cover") or "",
                "source": "kmoe",
            },
        )
    return full


def kmoe_chapter(mid: str, cid: str) -> dict:
    key = "kmoe_%s_%s" % (mid, cid)
    cached = cache_get("chapter", key, TTL_CHAPTER)
    if cached:
        return cached
    payload = kmoe.chapter(mid, cid)
    cache_set("chapter", key, payload)
    return payload


def hip_chapter(mid: str, cid: str) -> dict:
    key = "hip_%s_%s" % (mid, cid)
    cached = cache_get("chapter", key, TTL_CHAPTER)
    if cached:
        return cached
    payload = hipmh.chapter(mid, cid)
    cache_set("chapter", key, payload)
    return payload


def search_site(keyword: str, page: int = 1) -> dict:
    key = f"{keyword}_{page}"
    cached = cache_get("search", key, TTL_SEARCH, allow_stale=True)
    if cached:
        return cached
    qs = urllib.parse.urlencode({"q": keyword, "page": page})
    html = http_get(f"{SITE}/s?{qs}", headers={"Accept": "text/html"}).decode("utf-8", "replace")
    total = 0
    m = re.search(r"總數:\s*<span[^>]*>(\d+)</span>", html)
    if m:
        total = int(m.group(1))
    payload = {
        "code": 0,
        "keyword": keyword,
        "page": page,
        "total": total,
        "list": parse_cards(html, 60),
    }
    cache_set("search", key, payload)
    return payload


def manga_mid(slug: str) -> dict:
    cached = cache_get("slug", slug, TTL_CHAPTER)
    if cached and cached.get("mid"):
        return cached
    html = http_get(f"{SITE}/manga/{slug}", headers={"Accept": "text/html"}).decode(
        "utf-8", "replace"
    )
    mid = ""
    m = re.search(r'data-mid="(\d+)"', html)
    if m:
        mid = m.group(1)
    title = ""
    tm = re.search(r"<h1[^>]*>(.*?)</h1>", html, re.S)
    if tm:
        title = re.sub(r"<[^>]+>", "", tm.group(1)).strip()
    cover = ""
    cm = re.search(
        r"https://c-nc-1\.6wm\.top/manga/" + re.escape(slug) + r"/[^\"\s>]+\.webp", html
    )
    if cm:
        cover = cm.group(0)
    first_cs = ""
    fm = re.search(r'data-firstid="(\d+)"', html)
    if fm:
        first_cs = fm.group(1)
    if not mid:
        raise RuntimeError("no mid on manga page")
    meta = {"mid": mid, "slug": slug, "title": title, "cover": cover, "first_cs": first_cs}
    cache_set("slug", slug, meta)
    return meta


def manga_full(mid: str) -> dict:
    cached = cache_get("manga", mid, TTL_MANGA)
    if cached:
        return cached
    j = json_get(f"{API}/api/manga/get?mid={urllib.parse.quote(mid)}&mode=all")
    if not j.get("status"):
        raise RuntimeError(j.get("msg") or "manga get failed")
    d = j.get("data") or {}
    chapters = []
    for ch in d.get("chapters") or []:
        attr = ch.get("attributes") or {}
        chapters.append(
            {
                "id": str(ch.get("id") or ""),
                "title": attr.get("title") or "",
                "slug": attr.get("slug") or "",
                "order": attr.get("order"),
                "updated": attr.get("updatedAt") or "",
            }
        )
    chapters.reverse()
    payload = {
        "code": 0,
        "id": str(d.get("id") or mid),
        "title": d.get("title") or "",
        "slug": d.get("slug") or "",
        "cover": d.get("cover") or "",
        "desc": d.get("desc") or "",
        "status": d.get("status"),
        "chapters": chapters,
    }
    if payload["slug"]:
        cache_set(
            "slug",
            payload["slug"],
            {
                "mid": payload["id"],
                "slug": payload["slug"],
                "title": payload["title"],
                "cover": payload["cover"],
                "first_cs": chapters[-1]["id"] if chapters else "",
            },
        )
    cache_set("manga", mid, payload)
    return payload


def cdn_host(line: int | None) -> str:
    if line == 2:
        return CDN[2]
    return str(CDN["default"])


def chapter_info(mid: str, cid: str) -> dict:
    key = f"{mid}_{cid}"
    cached = cache_get("chapter", key, TTL_CHAPTER)
    if cached:
        return cached
    j = json_get(
        f"{API}/api/v2/chapter/getinfo?m={urllib.parse.quote(mid)}&c={urllib.parse.quote(cid)}"
    )
    if not j.get("status"):
        raise RuntimeError("chapter getinfo failed")
    info = (j.get("data") or {}).get("info") or {}
    blob = info.get("images") or {}
    line = int(blob.get("line") or 1)
    images = decode_images(blob.get("images") or "")
    host = cdn_host(line)
    pages = []
    for item in images:
        path = item.get("url") or ""
        if not path.startswith("/"):
            path = "/" + path
        pages.append(
            {
                "order": item.get("order"),
                "path": path,
                "url": host + path,
                "cf": host + path,
            }
        )
    payload = {
        "code": 0,
        "mid": str(info.get("mid") or mid),
        "cid": str(info.get("id") or cid),
        "title": info.get("title") or "",
        "manga": info.get("mangatitle") or "",
        "slug": info.get("slug") or "",
        "line": line,
        "prevslug": info.get("prevslug") or "",
        "nextslug": info.get("nextslug") or "",
        "prevtitle": info.get("prevtitle") or "",
        "nexttitle": info.get("nexttitle") or "",
        "pre": str(info.get("pre") or ""),
        "next": str(info.get("next") or ""),
        "source": "bz",
        "pages": pages,
    }
    cache_set("chapter", key, payload)
    return payload


def safe_name(text: str) -> str:
    base = re.sub(r'[<>:"/\\|?*]', "_", (text or "").strip()) or "file"
    return " ".join(base.split())[:80]


def content_disposition(filename: str) -> str:
    ascii_name = filename.encode("ascii", "replace").decode("ascii").replace("?", "_")
    starred = urllib.parse.quote(filename)
    return "attachment; filename=\"%s\"; filename*=UTF-8''%s" % (ascii_name, starred)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _gzip_body(self, body: bytes, ctype: str) -> tuple[bytes, dict]:
        if len(body) < 512:
            return body, {}
        kind = (ctype or "").split(";", 1)[0].strip().lower()
        if kind not in (
            "text/html",
            "text/plain",
            "text/css",
            "text/javascript",
            "application/javascript",
            "application/json",
            "application/xml",
        ):
            return body, {}
        ae = (self.headers.get("Accept-Encoding") or "").lower()
        if "gzip" not in ae:
            return body, {}
        buf = io.BytesIO()
        with gzip.GzipFile(fileobj=buf, mode="wb", compresslevel=5, mtime=0) as gz:
            gz.write(body)
        zipped = buf.getvalue()
        if len(zipped) >= len(body) - 80:
            return body, {}
        return zipped, {"Content-Encoding": "gzip", "Vary": "Accept-Encoding"}

    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None):
        extra = dict(extra or {})
        body, gz = self._gzip_body(body, ctype)
        extra.update(gz)
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", extra.get("Cache-Control") or "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Connection", extra.get("Connection") or "keep-alive")
        for k, v in extra.items():
            if k in ("Cache-Control", "Connection"):
                continue
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        raw = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        extra = {"Cache-Control": "no-store"}
        if obj.get("code") == 0:
            if "pages" in obj:
                extra["Cache-Control"] = "public, max-age=300, s-maxage=3600, stale-while-revalidate=86400"
            elif "chapters" in obj:
                extra["Cache-Control"] = "public, max-age=120, s-maxage=600, stale-while-revalidate=3600"
            elif any(k in obj for k in ("sections", "list")):
                extra["Cache-Control"] = "public, max-age=60, s-maxage=300, stale-while-revalidate=1800"
            extra["CDN-Cache-Control"] = extra["Cache-Control"]
        self._send(code, raw, "application/json; charset=utf-8", extra)

    def _index_bytes(self) -> bytes:
        global _INDEX_HTML, _INDEX_MTIME
        path = STATIC / "index.html"
        mtime = path.stat().st_mtime
        if _INDEX_HTML is None or mtime != _INDEX_MTIME:
            _INDEX_HTML = path.read_bytes()
            _INDEX_MTIME = mtime
        html = _INDEX_HTML
        if HOME_MARK not in html:
            return html
        try:
            payload = home_payload()
            blob = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            blob = blob.replace("<", "\\u003c").encode("utf-8")
            injected = b"window.__HOME__=" + blob + b";"
        except Exception:
            injected = HOME_MARK
        return html.replace(HOME_MARK, injected, 1)

    def do_OPTIONS(self):
        self._send(204, b"", "text/plain")

    def do_HEAD(self):
        self._route(head_only=True)

    def do_GET(self):
        self._route(head_only=False)

    def _img(self, path: str, line: int, head_only: bool, src: str = ""):
        if (
            ".." in path
            or not path.startswith("/")
            or not re.match(r"^/[A-Za-z0-9][A-Za-z0-9_./-]*\.(webp|jpg|jpeg|png)$", path, re.I)
        ):
            return self._send(400, b"bad path", "text/plain")
        hip = src == "hip" or path.startswith("/i/")
        if hip:
            urls = [h + path for h in hipmh.img_hosts(line)]
            referer = "https://reader.hipmh.top/"
        else:
            urls = [cdn_host(line) + path]
            referer = SITE + "/"
        rng = None if head_only else self.headers.get("Range")
        hdrs = {
            "User-Agent": UA,
            "Accept": "image/webp,image/*,*/*;q=0.8",
            "Referer": referer,
        }
        if rng:
            hdrs["Range"] = rng
        resp = None
        last_err = None
        for url in urls:
            req = urllib.request.Request(url, headers=hdrs)
            try:
                resp = urllib.request.urlopen(req, timeout=40, context=CTX)
                last_err = None
                break
            except urllib.error.HTTPError as e:
                last_err = e
                if e.code in (404, 403, 410):
                    continue
                body = e.read()
                return self._send(e.code, body, e.headers.get("Content-Type") or "text/plain")
            except Exception as e:
                last_err = e
                continue
        if resp is None:
            if isinstance(last_err, urllib.error.HTTPError):
                body = last_err.read()
                return self._send(last_err.code, body, last_err.headers.get("Content-Type") or "text/plain")
            return self._send(502, str(last_err or "cdn fail").encode(), "text/plain")
        ctype = resp.headers.get("Content-Type") or "image/webp"
        cl = resp.headers.get("Content-Length")
        cr = resp.headers.get("Content-Range") or ""
        extra = {
            "Accept-Ranges": "bytes",
            "Access-Control-Allow-Origin": "*",
            "Cache-Control": "public, max-age=86400, s-maxage=604800",
            "Connection": "keep-alive",
        }
        code = 206 if rng and cr else 200
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        if cl:
            self.send_header("Content-Length", cl)
        if cr:
            self.send_header("Content-Range", cr)
        for k, v in extra.items():
            self.send_header(k, v)
        self.end_headers()
        if head_only:
            resp.close()
            return
        try:
            while True:
                chunk = resp.read(64 * 1024)
                if not chunk:
                    break
                self.wfile.write(chunk)
        finally:
            resp.close()

    def _zip(self, mid: str, cid: str, src: str = ""):
        if is_kmoe_request(src=src) or src == "kmoe":
            info = kmoe_chapter(mid, cid)
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_STORED) as zf:
                for i, page in enumerate(info["pages"], 1):
                    name = page["path"].rsplit("/", 1)[-1]
                    raw = kmoe.page_file(mid, cid, name).read_bytes()
                    zf.writestr("%03d.jpg" % i, raw)
            data = buf.getvalue()
            fname = safe_name(f"{info['manga']} - {info['title']}") + ".zip"
            extra = {"Content-Disposition": content_disposition(fname)}
            return self._send(200, data, "application/zip", extra)
        if is_hip_request(mid=mid, cid=cid, src=src):
            info = hip_chapter(mid, cid)
        else:
            info = chapter_info(mid, cid)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_STORED) as zf:
            for i, page in enumerate(info["pages"], 1):
                raw = http_get(
                    page["url"],
                    headers={"Accept": "image/webp,image/*,*/*;q=0.8"},
                    timeout=40,
                )
                zf.writestr(f"{i:03d}.webp", raw)
        data = buf.getvalue()
        fname = safe_name(f"{info['manga']} - {info['title']}") + ".zip"
        extra = {"Content-Disposition": content_disposition(fname)}
        self._send(200, data, "application/zip", extra)

    def _route(self, head_only: bool = False):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)

        if path == "/health":
            return self._send(200, b"ok", "text/plain", {"Cache-Control": "no-store"})

        if path in ("/", "/index.html"):
            html = self._index_bytes()
            extra = {
                "Cache-Control": "public, max-age=30, s-maxage=60, stale-while-revalidate=300",
            }
            if head_only:
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(html)))
                self.send_header("Cache-Control", extra["Cache-Control"])
                self.send_header("Connection", "close")
                self.end_headers()
                return
            return self._send(200, html, "text/html; charset=utf-8", extra)

        if path == "/api/home":
            try:
                return self._json(home_payload())
            except Exception as e:
                return self._json({"code": -1, "msg": str(e)}, 502)

        if path == "/api/list":
            kind = (qs.get("kind") or ["hots"])[0]
            table = {"hots": "/hots", "dayup": "/dayup", "newss": "/newss", "manga": "/manga"}
            if kind not in table:
                return self._json({"code": -1, "msg": "bad kind"}, 400)
            cached = cache_get("list", kind, TTL_HOME)
            if cached:
                return self._json(cached)
            try:
                payload = {"code": 0, "kind": kind, "list": fetch_list_page(table[kind])}
                cache_set("list", kind, payload)
                return self._json(payload)
            except Exception as e:
                return self._json({"code": -1, "msg": str(e)}, 502)

        if path == "/api/search":
            kw = (qs.get("q") or qs.get("keyword") or [""])[0].strip()
            page = int((qs.get("page") or ["1"])[0] or 1)
            if not kw:
                return self._json({"code": -1, "msg": "keyword required"}, 400)
            try:
                return self._json(search_merged(kw, page))
            except Exception as e:
                return self._json({"code": -1, "msg": str(e)}, 502)

        if path == "/api/manga":
            slug = (qs.get("slug") or [""])[0].strip()
            src = (qs.get("src") or [""])[0].strip()
            if not slug:
                return self._json({"code": -1, "msg": "slug required"}, 400)
            try:
                if is_kmoe_request(slug=slug, src=src):
                    return self._json(kmoe_manga(slug))
                if is_hip_request(slug=slug, src=src):
                    return self._json(hip_manga(slug))
                meta = manga_mid(slug)
                full = manga_full(meta["mid"])
                if not full.get("cover"):
                    full["cover"] = meta.get("cover") or ""
                if not full.get("title"):
                    full["title"] = meta.get("title") or slug
                full.setdefault("source", "bz")
                return self._json(full)
            except Exception as e:
                return self._json({"code": -1, "msg": str(e)}, 502)

        if path == "/api/chapter":
            mid = (qs.get("m") or qs.get("mid") or [""])[0].strip()
            cid = (qs.get("c") or qs.get("cid") or [""])[0].strip()
            src = (qs.get("src") or [""])[0].strip()
            slug = (qs.get("slug") or [""])[0].strip()
            if not mid or not cid:
                return self._json({"code": -1, "msg": "m and c required"}, 400)
            try:
                if is_kmoe_request(slug=slug, src=src) or src == "kmoe" or str(mid).isdigit() and src == "kmoe":
                    return self._json(kmoe_chapter(mid, cid))
                if is_hip_request(mid=mid, cid=cid, src=src):
                    return self._json(hip_chapter(mid, cid))
                return self._json(chapter_info(mid, cid))
            except Exception as e:
                return self._json({"code": -1, "msg": str(e)}, 502)

        if path == "/img":
            img_path = (qs.get("path") or [""])[0]
            line = int((qs.get("line") or ["2"])[0] or 2)
            src = (qs.get("src") or [""])[0]
            if not img_path:
                return self._send(400, b"path required", "text/plain")
            try:
                return self._img(img_path, line, head_only, src=src)
            except Exception as e:
                return self._send(502, str(e).encode(), "text/plain")

        if path.startswith("/kmoe-img/"):
            parts = [p for p in path.split("/") if p]
            # kmoe-img / bid / vol / name
            if len(parts) != 4:
                return self._send(400, b"bad path", "text/plain")
            _, bid, vol, name = parts
            try:
                fpath = kmoe.page_file(bid, vol, name)
            except Exception as e:
                return self._send(404, str(e).encode(), "text/plain")
            data = fpath.read_bytes()
            ctype = "image/jpeg"
            if name.lower().endswith(".png"):
                ctype = "image/png"
            elif name.lower().endswith(".webp"):
                ctype = "image/webp"
            extra = {
                "Cache-Control": "public, max-age=86400, s-maxage=604800, immutable",
            }
            if head_only:
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", extra["Cache-Control"])
                self.send_header("Connection", "close")
                self.end_headers()
                return
            return self._send(200, data, ctype, extra)

        if path == "/download":
            if head_only:
                self.send_response(200)
                self.send_header("Content-Type", "application/zip")
                self.send_header("Connection", "close")
                self.end_headers()
                return
            mid = (qs.get("m") or [""])[0].strip()
            cid = (qs.get("c") or [""])[0].strip()
            src = (qs.get("src") or [""])[0].strip()
            if not mid or not cid:
                return self._send(400, b"m and c required", "text/plain")
            try:
                return self._zip(mid, cid, src=src)
            except Exception as e:
                return self._send(502, str(e).encode(), "text/plain")

        if head_only:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.send_header("Connection", "close")
            self.end_headers()
            return
        return self._send(404, b"not found", "text/plain")


def _warm() -> None:
    try:
        home_payload()
    except Exception as e:
        sys.stderr.write("warm home failed: %s\n" % e)


def main() -> int:
    threading.Thread(target=_warm, name="warm-home", daemon=True).start()
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"ikun manga  http://{HOST}:{PORT}", flush=True)
    print("cache:", DATA, flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
