"""kzo.moe / bookof.moe 源。

搜索、封面、卷目录可匿名（先打开详情页拿 cookie+hash）。
mobi/epub/zip 直链 /getdownurl.php 未登录 403，阅读页也没有逐页图。
"""
from __future__ import annotations

import http.cookiejar
import json
import re
import ssl
import urllib.parse
import urllib.request
from html import unescape

CTX = ssl._create_unverified_context()
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)
SITE = "https://kzo.moe"
BOOKOF = "https://bookof.moe"

_CJ = http.cookiejar.CookieJar()
_OPENER = urllib.request.build_opener(
    urllib.request.HTTPSHandler(context=CTX),
    urllib.request.HTTPCookieProcessor(_CJ),
)
_META: dict = {}


def _get(url: str, headers: dict | None = None, timeout: int = 25) -> bytes:
    h = {
        "User-Agent": UA,
        "Accept": "*/*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Referer": SITE + "/",
    }
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h)
    with _OPENER.open(req, timeout=timeout) as resp:
        return resp.read()


def _json(url: str, headers: dict | None = None) -> dict:
    raw = _get(url, headers)
    if not raw:
        return {}
    return json.loads(raw.decode("utf-8", "replace"))


def strip_tags(s: str) -> str:
    return unescape(re.sub(r"<[^>]+>", "", s or "")).strip()


def numeric_id(token) -> str:
    token = str(token or "").strip()
    if token.startswith("kmoe-"):
        token = token[5:]
    m = re.search(r"(\d+)", token)
    return m.group(1) if m else token


def work_slug(bid: str) -> str:
    return "kmoe-" + numeric_id(bid)


def search(keyword: str, page: int = 1) -> dict:
    qs = urllib.parse.urlencode({"s": keyword, "p": page})
    j = _json(
        f"{SITE}/data_list.php?{qs}",
        {"X-KM-FROM": "KMOE/3.0.0(WEB) GET /list.php", "Accept": "application/json"},
    )
    items = []
    for d in j.get("data") or []:
        book = d.get("url_book") or ""
        bid = numeric_id(book)
        title = strip_tags(d.get("name") or "")
        cover = d.get("url_cover") or ""
        item = {
            "slug": work_slug(bid),
            "title": title,
            "cover": cover,
            "href": book,
            "source": "kmoe",
            "mid": bid,
            "author": strip_tags(d.get("author") or ""),
            "status": d.get("status") or "",
            "newvol": d.get("newvol") or "",
            "desc": "",
        }
        items.append(item)
        if bid:
            _META[bid] = {
                "title": title,
                "cover": cover,
                "author": item["author"],
                "status": item["status"],
            }
    return {
        "code": 0,
        "keyword": keyword,
        "page": int(j.get("nowpage") or page),
        "total": int(j.get("total") or 0),
        "list": items,
        "source": "kmoe",
    }


def _page_hash(bid: str) -> str:
    html = _get(f"{SITE}/c/{bid}.htm", {"Accept": "text/html"}).decode("utf-8", "replace")
    m = re.search(r'data_book\(\s*"([^"]+)"', html)
    if not m:
        raise RuntimeError("no data_book hash")
    return m.group(1)


def _bookof_info(bid: str) -> str:
    html = _get(f"{BOOKOF}/b/{bid}.htm", {"Accept": "text/html", "Referer": BOOKOF + "/"}).decode(
        "utf-8", "replace"
    )
    m = re.search(r"setInfo\(\s*'([^']+)'", html)
    return m.group(1) if m else ""


def _bookof_covers(bid: str) -> dict[str, str]:
    info = _bookof_info(bid)
    if not info:
        return {}
    j = _json(
        f"{BOOKOF}/v1/bookvol/",
        {
            "Referer": f"{BOOKOF}/b/{bid}.htm",
            "Origin": BOOKOF,
            "X-KAPI-FROM": f"ZJS/1.0.0(WEB) GET /b/{bid}.htm",
            "X-KAPI-INFO": info,
            "Accept": "application/json",
        },
    )
    out = {}
    for v in j.get("volinfo") or []:
        name = (v.get("volname") or "").strip()
        cover = v.get("url_cover") or v.get("url_cover_small") or ""
        if name and cover:
            out[name] = cover
    return out


def manga(bid: str) -> dict:
    bid = numeric_id(bid)
    khash = _page_hash(bid)
    j = _json(
        f"{SITE}/data_book.php?h={urllib.parse.quote(khash)}",
        {
            "X-KM-FROM": f"KMOE/3.0.0(WEB) GET /c/{bid}.htm",
            "Accept": "application/json",
            "Referer": f"{SITE}/c/{bid}.htm",
        },
    )
    covers = {}
    try:
        covers = _bookof_covers(bid)
    except Exception:
        covers = {}
    chapters = []
    for row in j.get("voldata") or []:
        if not row:
            continue
        volid = str(row[0])
        vtype = row[3] if len(row) > 3 else ""
        title = row[5] if len(row) > 5 else volid
        pages = row[6] if len(row) > 6 else ""
        updated = (row[13] if len(row) > 13 else "")[:10]
        cover = covers.get(title) or covers.get(f"{vtype} {row[4] if len(row)>4 else ''}") or ""
        chapters.append(
            {
                "id": volid,
                "title": title,
                "slug": volid,
                "order": row[4] if len(row) > 4 else None,
                "updated": updated,
                "pages": pages,
                "kind": vtype,
            }
        )
        if cover:
            chapters[-1]["cover"] = cover
    # 最新在前：话/卷按接口顺序，原站是单行本在前、连载话在后。翻成最新话在最上。
    chapters.reverse()
    meta = _META.get(bid) or {}
    title = j.get("bookname") or meta.get("title") or ("Kmoe " + bid)
    cover = meta.get("cover") or ""
    if not cover and chapters:
        cover = chapters[0].get("cover") or ""
    return {
        "code": 0,
        "id": bid,
        "title": title,
        "slug": work_slug(bid),
        "cover": cover,
        "desc": "Kmoe / kzo.moe 卷目录。mobi·epub 下载需原站登录。",
        "status": meta.get("status") or "",
        "source": "kmoe",
        "external": f"{SITE}/c/{bid}.htm",
        "chapters": chapters,
    }
