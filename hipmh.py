"""嬉皮漫画 m.hipmh.com 源。"""
from __future__ import annotations

import base64
import json
import re
import ssl
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

from decoder import decode_hip_images

CTX = ssl._create_unverified_context()
UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)
API = "https://hipapi1.s3file.top"
COVER = "https://cover.s3imgs.top"
IMG_HOSTS = {
    1: "https://hip-tx-1.s3imgs.top",
    2: "https://hip-cf-1.s3imgs.top",
    3: "https://hip-tx-1.s3imgs.top",
    9: "https://hip-tx-s1.s3imgs.top",
}
_META = {}
_OPENER = urllib.request.build_opener(urllib.request.HTTPSHandler(context=CTX))
IMG_FALLBACK = [
    "https://hip-tx-1.s3imgs.top",
    "https://hip-cf-1.s3imgs.top",
    "https://hip-tx-s1.s3imgs.top",
    "https://hip-cf-s1.s3imgs.top",
]


def hip_get(url: str, timeout: int = 25) -> bytes:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": UA,
            "Accept": "application/json,*/*",
            "Referer": "https://m.hipmh.com/",
            "Origin": "https://m.hipmh.com",
        },
    )
    with _OPENER.open(req, timeout=timeout) as resp:
        return resp.read()


def hip_json(url: str) -> dict:
    return json.loads(hip_get(url).decode("utf-8", "replace"))


def b64d(s: str) -> str:
    pad = s + "=" * ((4 - len(s) % 4) % 4)
    return base64.b64decode(pad).decode("utf-8", "replace")


def numeric_mid(token) -> str:
    token = str(token or "").strip()
    if token.isdigit():
        return token
    head = token.split("-", 1)[0]
    try:
        raw = b64d(head)
    except Exception:
        return token
    if raw.startswith("m:"):
        return raw[2:].split("-", 1)[0]
    return token


def work_slug(mid: str) -> str:
    return "hip-" + numeric_mid(mid)


def hid_to_api(hid: str) -> str:
    hid = (hid or "").strip()
    if not hid:
        return hid
    head, _sep, rest = hid.partition("-")
    try:
        raw = b64d(head)
    except Exception:
        return hid
    if raw.startswith("c:"):
        return hid
    if "-c:" in raw:
        cpart = raw.split("-c:", 1)[1]
        api_head = base64.b64encode(("c:" + cpart).encode()).decode().rstrip("=")
        return api_head + (("-" + rest) if rest else "")
    return hid


def popularity(limit: int = 24) -> list[dict]:
    req = urllib.request.Request(
        "https://m.hipmh.com/popularity",
        headers={
            "User-Agent": UA,
            "Accept": "text/html",
            "Referer": "https://m.hipmh.com/",
        },
    )
    with _OPENER.open(req, timeout=20) as resp:
        html = resp.read().decode("utf-8", "replace")
    items = []
    seen = set()
    for m in re.finditer(
        r'href="/works/([^"]+)"[^>]*aria-label="([^"]+)"[\s\S]{0,900}?src="(https://[^"]+)"',
        html,
    ):
        work_id, title, cover = m.group(1), m.group(2), m.group(3)
        mid = numeric_mid(work_id)
        if mid in seen:
            continue
        seen.add(mid)
        item = {
            "slug": work_slug(mid),
            "title": title,
            "cover": cover,
            "href": "/works/" + work_id,
            "source": "hip",
            "mid": mid,
            "work_id": work_id,
        }
        _META[mid] = {"title": title, "cover": cover, "desc": ""}
        items.append(item)
        if len(items) >= limit:
            break
    return items


def cover_url(path: str) -> str:
    path = path or ""
    if path.startswith("http"):
        return path
    if path.startswith("/"):
        return COVER + path
    return COVER + "/" + path if path else ""


def search(keyword: str, page: int = 1, page_size: int = 20) -> dict:
    qs = urllib.parse.urlencode({"q": keyword, "page": page, "page_size": page_size})
    j = hip_json(f"{API}/v1/search?{qs}")
    if j.get("code") != 200:
        raise RuntimeError(j.get("message") or "hipmh search failed")
    data = j.get("data") or {}
    items = []
    for s in data.get("data") or []:
        mid = numeric_mid(s.get("id") or "")
        items.append(
            {
                "slug": work_slug(mid),
                "title": s.get("title") or "",
                "cover": cover_url(s.get("vertical_image_url") or ""),
                "href": "/works/" + (s.get("id") or ""),
                "source": "hip",
                "mid": mid,
                "work_id": s.get("id") or "",
                "desc": s.get("description") or "",
            }
        )
        _META[mid] = {"title": items[-1]["title"], "cover": items[-1]["cover"], "desc": items[-1]["desc"]}
    return {
        "code": 0,
        "keyword": keyword,
        "page": int(data.get("page") or page),
        "total": int(data.get("total") or 0),
        "list": items,
        "source": "hip",
    }


def _chapter_page(mid: str, page: int, per_page: int = 50) -> dict:
    qs = urllib.parse.urlencode(
        {"mid": mid, "page": page, "per_page": per_page, "order": "desc"}
    )
    j = hip_json(f"{API}/v1/manga/chapters?{qs}")
    if j.get("code") != 200:
        raise RuntimeError(j.get("message") or "hipmh chapters failed")
    return j.get("data") or {}


def _items_to_chapters(items: list) -> list[dict]:
    out = []
    for ch in items or []:
        hid = ch.get("hid") or ""
        out.append(
            {
                "id": hid,
                "title": ch.get("title") or "",
                "slug": hid,
                "order": ch.get("chapter_number"),
                "updated": (ch.get("updated_at") or "")[:10],
            }
        )
    return out


def manga(mid: str) -> dict:
    mid = numeric_mid(mid)
    first = _chapter_page(mid, 1)
    total_pages = min(int(first.get("total_pages") or 1), 40)
    chapters = _items_to_chapters(first.get("items") or [])
    if total_pages > 1:
        pages: dict[int, list] = {}
        with ThreadPoolExecutor(max_workers=8) as pool:
            futs = {pool.submit(_chapter_page, mid, p): p for p in range(2, total_pages + 1)}
            for fut in as_completed(futs):
                pages[futs[fut]] = fut.result().get("items") or []
        for p in range(2, total_pages + 1):
            chapters.extend(_items_to_chapters(pages.get(p) or []))
    meta = _META.get(mid) or {}
    title = meta.get("title") or ""
    cover = meta.get("cover") or ""
    desc = meta.get("desc") or ""
    if not title:
        title = "嬉皮 " + mid
    payload = {
        "code": 0,
        "id": mid,
        "title": title,
        "slug": work_slug(mid),
        "cover": cover,
        "desc": desc,
        "status": "",
        "source": "hip",
        "chapters": chapters,
    }
    return payload


def chapter(mid: str, hid: str) -> dict:
    mid = numeric_mid(mid)
    api_hid = hid_to_api(hid)
    j = hip_json(f"{API}/v2/chapter?hid={urllib.parse.quote(api_hid)}")
    if j.get("code") != 200:
        raise RuntimeError(j.get("message") or "hipmh chapter failed")
    d = j.get("data") or {}
    blob = d.get("images") or ""
    paths = decode_hip_images(blob, d.get("order_id"), d.get("sid"))
    line = int(d.get("line") or 1)
    host = IMG_HOSTS.get(line, IMG_HOSTS[1])
    pages = []
    for i, path in enumerate(paths, 1):
        if not path.startswith("/"):
            path = "/" + path
        pages.append({"order": i, "path": path, "url": host + path})
    return {
        "code": 0,
        "mid": str(d.get("manga_id") or mid),
        "cid": hid,
        "title": d.get("chapter_title") or "",
        "manga": d.get("manga_title") or "",
        "slug": work_slug(d.get("manga_id") or mid),
        "line": line,
        "src": "hip",
        "source": "hip",
        "prevslug": d.get("prev_hid") or "",
        "nextslug": d.get("next_hid") or "",
        "prevtitle": "",
        "nexttitle": "",
        "pre": d.get("prev_hid") or "",
        "next": d.get("next_hid") or "",
        "pages": pages,
    }


def img_hosts(line: int | None = None) -> list[str]:
    primary = IMG_HOSTS.get(int(line or 1), IMG_HOSTS[1])
    out = [primary]
    for h in IMG_FALLBACK:
        if h not in out:
            out.append(h)
    return out
