"""kzo.moe / bookof.moe 源。

搜索、封面、卷目录可匿名。登录后 getdownurl 出 epub，解包成页图给阅读器。
"""
from __future__ import annotations

import http.cookiejar
import json
import re
import ssl
import urllib.parse
import urllib.request
from html import unescape
from pathlib import Path
import io
import os
import posixpath
import threading
import zipfile
from xml.etree import ElementTree as ET

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
ROOT = Path(__file__).resolve().parent
CACHE = ROOT / "data" / "kmoe"
SECRET = ROOT / ".secrets" / "kmoe.env"
_LOGIN_LOCK = threading.Lock()
_LOGGED = False


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
        "desc": "Kmoe / kzo.moe。登录后解 epub 在线看漫画页。",
        "status": meta.get("status") or "",
        "source": "kmoe",
        "external": f"{SITE}/c/{bid}.htm",
        "chapters": chapters,
    }



def _load_secret() -> tuple[str, str]:
    email = os.environ.get("KMOE_EMAIL") or ""
    passwd = os.environ.get("KMOE_PASS") or ""
    if SECRET.is_file():
        for line in SECRET.read_text("utf-8").splitlines():
            if line.startswith("KMOE_EMAIL=") and not email:
                email = line.split("=", 1)[1].strip()
            elif line.startswith("KMOE_PASS=") and not passwd:
                passwd = line.split("=", 1)[1].strip()
    return email, passwd


def _ensure_login() -> None:
    global _LOGGED
    if _LOGGED:
        return
    with _LOGIN_LOCK:
        if _LOGGED:
            return
        email, passwd = _load_secret()
        if not email or not passwd:
            raise RuntimeError("missing kmoe credentials")
        _get(SITE + "/login.php", {"Accept": "text/html"})
        boundary = "----WebKitFormBoundary7MA4YWxkTrZu0gW"
        fields = {"email": email, "passwd": passwd}
        parts = []
        for k, v in fields.items():
            parts.append(
                (
                    "--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n"
                    % (boundary, k, v)
                ).encode()
            )
        body = b"".join(parts) + ("--%s--\r\n" % boundary).encode()
        h = {
            "User-Agent": UA,
            "Accept": "application/json",
            "Referer": SITE + "/login.php",
            "Origin": SITE,
            "X-KM-FROM": "KMOE/3.0.0(WEB) POST /login.php",
            "Content-Type": "multipart/form-data; boundary=" + boundary,
        }
        req = urllib.request.Request(SITE + "/login_act.php", data=body, headers=h, method="POST")
        with _OPENER.open(req, timeout=25) as resp:
            raw = resp.read().decode("utf-8", "replace")
        if '"msgid":"m100"' not in raw and "m100" not in raw:
            raise RuntimeError("kmoe login failed: " + raw[:200])
        _LOGGED = True


def _vol_dir(bid: str, volid: str) -> Path:
    d = CACHE / numeric_id(bid) / str(volid)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _spine_images(data: bytes) -> list[bytes]:
    z = zipfile.ZipFile(io.BytesIO(data))
    opf_name = next((n for n in z.namelist() if n.endswith(".opf")), None)
    if not opf_name:
        raise RuntimeError("epub has no opf")
    root = ET.fromstring(z.read(opf_name))
    ns = "{http://www.idpf.org/2007/opf}"
    manifest = {}
    for item in root.findall(".//%sitem" % ns):
        manifest[item.get("id")] = item.get("href")
    spine = [it.get("idref") for it in root.findall(".//%sitemref" % ns)]
    base = str(Path(opf_name).parent)
    if base == ".":
        base = ""
    pages = []
    for idref in spine:
        href = manifest.get(idref) or ""
        if not href.lower().endswith((".xhtml", ".html", ".htm")):
            continue
        path = href if not base or href.startswith("/") else str(Path(base) / href)
        html = z.read(path.replace("\\", "/")).decode("utf-8", "replace")
        title = ""
        m = re.search(r"<title>(.*?)</title>", html, re.I | re.S)
        if m:
            title = m.group(1)
        if "THE END" in title.upper() or title.strip() == "封面":
            continue
        srcs = re.findall(r'src=["\']([^"\']+)', html)
        if not srcs:
            continue
        img_rel = srcs[0]
        img_path = posixpath.normpath(posixpath.join(posixpath.dirname(path), img_rel))
        if img_path.startswith("./"):
            img_path = img_path[2:]
        if "cover.jpg" in img_path.lower() or "theend" in img_path.lower():
            continue
        pages.append(z.read(img_path))
    return pages


def unpack_volume(bid: str, volid: str, fmt: int = 2) -> Path:
    bid = numeric_id(bid)
    volid = str(volid)
    dest = _vol_dir(bid, volid)
    marker = dest / "pages.json"
    if marker.is_file():
        return dest
    _ensure_login()
    raw = _get(
        f"{SITE}/getdownurl.php?b={bid}&v={volid}&mobi={fmt}&vip=0&json=1",
        {
            "X-KM-FROM": f"KMOE/3.0.0(WEB) GET /c/{bid}.htm",
            "Accept": "application/json",
            "Referer": f"{SITE}/c/{bid}.htm",
        },
        timeout=40,
    )
    info = json.loads(raw.decode("utf-8", "replace"))
    if info.get("code") != 200 or not info.get("url"):
        raise RuntimeError(info.get("msg") or "no download url")
    href = info["url"]
    req = urllib.request.Request(
        href,
        headers={"User-Agent": UA, "Referer": SITE + "/", "Accept": "*/*"},
    )
    with _OPENER.open(req, timeout=180) as resp:
        data = resp.read()
    images = _spine_images(data)
    if not images:
        raise RuntimeError("no comic pages in epub")
    names = []
    for i, blob in enumerate(images, 1):
        name = "%03d.jpg" % i
        (dest / name).write_bytes(blob)
        names.append(name)
    marker.write_text(json.dumps({"n": len(names), "files": names}, ensure_ascii=False), encoding="utf-8")
    return dest


def chapter(bid: str, volid: str) -> dict:
    bid = numeric_id(bid)
    volid = str(volid)
    dest = unpack_volume(bid, volid)
    meta = json.loads((dest / "pages.json").read_text("utf-8"))
    names = meta.get("files") or []
    title = ""
    manga_title = _META.get(bid, {}).get("title") or ""
    try:
        cat = manga(bid)
        manga_title = cat.get("title") or manga_title
        for ch in cat.get("chapters") or []:
            if str(ch.get("id")) == volid:
                title = ch.get("title") or ""
                break
    except Exception:
        pass
    pages = []
    for i, name in enumerate(names, 1):
        href = "/kmoe-img/%s/%s/%s" % (bid, volid, name)
        pages.append({"order": i, "path": href, "url": href, "cf": href})
    pre = nxt = ""
    pre_title = nxt_title = ""
    try:
        cat = manga(bid)
        chs = cat.get("chapters") or []
        ids = [str(c.get("id")) for c in chs]
        if volid in ids:
            i = ids.index(volid)
            if i + 1 < len(ids):
                pre = ids[i + 1]
                pre_title = chs[i + 1].get("title") or ""
            if i - 1 >= 0:
                nxt = ids[i - 1]
                nxt_title = chs[i - 1].get("title") or ""
    except Exception:
        pass
    return {
        "code": 0,
        "mid": bid,
        "cid": volid,
        "title": title or volid,
        "manga": manga_title,
        "slug": work_slug(bid),
        "line": 0,
        "source": "kmoe",
        "pre": pre,
        "next": nxt,
        "prevtitle": pre_title,
        "nexttitle": nxt_title,
        "pages": pages,
    }


def page_file(bid: str, volid: str, name: str) -> Path:
    if not re.match(r"^\d{3}\.(jpg|jpeg|png|webp)$", name, re.I):
        raise RuntimeError("bad page")
    dest = unpack_volume(bid, volid)
    path = dest / name
    if not path.is_file():
        raise RuntimeError("missing page")
    return path
