"""章节图路径解码。

包子 baozimh.org / 嬉皮 m.hipmh.com 都把 images 做成带标记的乱序 blob，
浏览器里由 `/assets/runtime/chapter-decoder.js` 的 `window.__cimg.r` 还原。
这里是同一套步骤的 Python 版，按 blob 前缀自动选配置。
"""
from __future__ import annotations

import base64
import json

PROFILES = {
    "bz": {
        "prefix": "J7r",
        "suffix": "nQ",
        "marker_a": "kD",
        "marker_b": "W4s",
        "chunk": 7,
        "src": "_-9876543210abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ",
        "dst": "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_",
    },
    "hip": {
        "prefix": "qM9",
        "suffix": "Z7",
        "marker_a": "Vx",
        "marker_b": "pL0",
        "chunk": 7,
        "src": "_-9876543210abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ",
        "dst": "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_",
    },
}

_HIP_D = (40503 << 16 | 31153) & 0xFFFFFFFF
_HIP_R = (34283 << 16 | 51819) & 0xFFFFFFFF


def _unshuffle(payload: str, chunk: int) -> str:
    parts = []
    for i, off in enumerate(range(0, len(payload), chunk)):
        block = payload[off : off + chunk]
        if i % 2:
            block = block[::-1]
        parts.append(block)
    return "".join(parts)


def _b64url_decode(text: str) -> bytes:
    pad = "=" * ((4 - len(text) % 4) % 4)
    return base64.b64decode(text.replace("-", "+").replace("_", "/") + pad)


def _detect(blob: str) -> str:
    for name, p in PROFILES.items():
        if blob.startswith(p["prefix"]) and blob.endswith(p["suffix"]):
            return name
    raise ValueError("bad image blob")


def decode_blob(blob: str, profile: str | None = None) -> list:
    if not isinstance(blob, str) or len(blob) < 8:
        raise ValueError("bad image blob")
    name = profile or _detect(blob)
    p = PROFILES[name]
    prefix, suffix = p["prefix"], p["suffix"]
    marker_a, marker_b = p["marker_a"], p["marker_b"]
    chunk = p["chunk"]
    table = str.maketrans(p["src"], p["dst"])
    body = blob[len(prefix) : -len(suffix)]
    remain = len(body) - len(marker_a) - len(marker_b)
    if remain <= 0:
        raise ValueError("short image blob")
    tail = remain // 3
    head = (remain - tail) // 2
    mid = remain - tail - head
    p0 = body[:head]
    m1 = body[head : head + len(marker_a)]
    p1 = body[head + len(marker_a) : head + len(marker_a) + mid]
    m2_off = head + len(marker_a) + mid
    m2 = body[m2_off : m2_off + len(marker_b)]
    p2 = body[m2_off + len(marker_b) :]
    if m1 != marker_a or m2 != marker_b or len(p2) != tail:
        raise ValueError("image blob markers mismatch")
    raw = _b64url_decode(_unshuffle(p2 + p0 + p1, chunk).translate(table))
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, list):
        raise ValueError("decoded images is not a list")
    return data


def drop_dummy(images: list, order_id, sid) -> list:
    d = len(images)
    if d <= 0:
        return images
    try:
        m = int(order_id) & 0xFFFFFFFF
        g = int(sid) & 0xFFFFFFFF
    except (TypeError, ValueError):
        return images
    b = ((g * _HIP_D) ^ (d * _HIP_R)) % d
    h = m ^ b
    if 0 <= h < d:
        return images[:h] + images[h + 1 :]
    return images


def decode_images(blob: str) -> list:
    """包子格式：返回 [{order,url}, ...]。"""
    data = decode_blob(blob, "bz")
    out = []
    for item in data:
        if isinstance(item, dict):
            out.append(item)
        elif isinstance(item, str):
            out.append({"url": item})
    return out


def decode_hip_images(blob: str, order_id=None, sid=None) -> list:
    data = decode_blob(blob, "hip")
    paths = []
    for item in data:
        if isinstance(item, str):
            paths.append(item)
        elif isinstance(item, dict):
            paths.append(str(item.get("url") or ""))
    paths = [p for p in paths if p]
    if order_id is not None and sid is not None:
        paths = drop_dummy(paths, order_id, sid)
    return paths
