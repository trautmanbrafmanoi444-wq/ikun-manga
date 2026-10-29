"""包子漫画章节图路径解码。

站点把 images 做成带标记的乱序 blob，浏览器里由
`/assets/runtime/chapter-decoder.js` 的 `window.__cimg.r` 还原。
这里是同一套步骤的 Python 版。
"""
from __future__ import annotations

import base64
import json

PREFIX = "J7r"
MARKER_A = "kD"
MARKER_B = "W4s"
SUFFIX = "nQ"
CHUNK = 7
SRC = "_-9876543210abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
DST = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
_TABLE = str.maketrans(SRC, DST)


def _unshuffle(payload: str) -> str:
    parts = []
    for i, off in enumerate(range(0, len(payload), CHUNK)):
        block = payload[off : off + CHUNK]
        if i % 2:
            block = block[::-1]
        parts.append(block)
    return "".join(parts)


def _b64url_decode(text: str) -> bytes:
    pad = "=" * ((4 - len(text) % 4) % 4)
    return base64.b64decode(text.replace("-", "+").replace("_", "/") + pad)


def decode_images(blob: str) -> list[dict]:
    if not isinstance(blob, str) or not blob.startswith(PREFIX) or not blob.endswith(SUFFIX):
        raise ValueError("bad image blob")
    body = blob[len(PREFIX) : -len(SUFFIX)]
    remain = len(body) - len(MARKER_A) - len(MARKER_B)
    if remain <= 0:
        raise ValueError("short image blob")
    tail = remain // 3
    head = (remain - tail) // 2
    mid = remain - tail - head
    p0 = body[:head]
    m1 = body[head : head + len(MARKER_A)]
    p1 = body[head + len(MARKER_A) : head + len(MARKER_A) + mid]
    m2_off = head + len(MARKER_A) + mid
    m2 = body[m2_off : m2_off + len(MARKER_B)]
    p2 = body[m2_off + len(MARKER_B) :]
    if m1 != MARKER_A or m2 != MARKER_B or len(p2) != tail:
        raise ValueError("image blob markers mismatch")
    raw = _b64url_decode(_unshuffle(p2 + p0 + p1).translate(_TABLE))
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, list):
        raise ValueError("decoded images is not a list")
    return data
