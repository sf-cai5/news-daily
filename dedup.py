"""去重层：URL 规范化 + 标题相似度判定（spec 5.2）。"""

from __future__ import annotations

import re
from difflib import SequenceMatcher
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# 追踪参数，规范化时直接丢掉，避免同一文章不同分享链接被当成两条
TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "spm", "from", "from_source", "share_token", "fbclid", "gclid",
    "sessionid", "ref", "source", "src", "_f", "share_source", "dt_dapp",
}

# ASCII 标点区间 + 中日韩标点，全部视为噪声
_PUNCT = re.compile(
    r"[\s\u3000!-/:-@\[-`{-~"
    r"\u3001-\u303f\uff01-\uff0f\uff1a-\uff20\uff3b-\uff40\uff5b-\uff65"
    r"\u2014\u2018\u2019\u201c\u201d\u2026\u00b7]+"
)
_BRACKET_HEAD = re.compile(r"^(?:【[^】]*】|\[[^\]]*\]|\([^)]*\)|（[^）]*）)+")
_SOURCE_TAIL = re.compile(
    r"[-_|—｜/]\s*(?:新浪|腾讯|网易|搜狐|澎湃|央视|新华|人民|中国新闻网|中新网|界面|财新|"
    r"BBC|CNN|Reuters|AP|路透社?|联合早报|纽约时报|卫报)?\s*"
    r"(?:新闻|网|中文网|客户端|快讯|报道)?$"
)

# 标题相似度阈值（spec：>= 0.9 视为重复）
TITLE_DUP_THRESHOLD = 0.9


def normalize_url(url: str) -> str:
    """去掉 fragment、追踪参数、末尾斜杠，主机名小写。

    注意：不改写主机名（不动 www.）。存进库里的链接是要给人点开的，
    必须保持能解析的原始形态；主机名的差异交给 url_key() 处理。
    """
    if not url:
        return ""
    url = url.strip()
    if url.startswith("//"):
        url = "http:" + url
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        return ""
    netloc = parts.netloc.lower()
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if k.lower() not in TRACKING_PARAMS]
    path = re.sub(r"/+$", "", parts.path) or "/"
    return urlunsplit((parts.scheme, netloc, path, urlencode(query), ""))


def url_key(url: str) -> str:
    """去重比较键：在 normalize_url 基础上忽略 www. 前缀与 http/https 差异。

    同一条新闻常被不同源用 http 与 https 两种写法给出，比较时统一当作同一条；
    存库的链接仍然是 normalize_url 的原样（要能点开）。
    """
    normalized = normalize_url(url)
    if not normalized:
        return ""
    parts = urlsplit(normalized)
    netloc = parts.netloc[4:] if parts.netloc.startswith("www.") else parts.netloc
    return urlunsplit(("https", netloc, parts.path, parts.query, ""))


def normalize_title(title: str) -> str:
    """去括号前缀、媒体名后缀、标点、大小写。"""
    t = (title or "").strip()
    t = _BRACKET_HEAD.sub("", t).strip()
    t = _SOURCE_TAIL.sub("", t).strip()
    t = _PUNCT.sub("", t).lower()
    return t


def _bigrams(s: str) -> set:
    if len(s) < 2:
        return {s} if s else set()
    return {s[i:i + 2] for i in range(len(s) - 1)}


def similarity(a: str, b: str) -> float:
    """标题相似度：取序列比与字符二元组 Jaccard 的较大值。"""
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    ratio = SequenceMatcher(None, a, b).ratio()
    ga, gb = _bigrams(a), _bigrams(b)
    union = ga | gb
    jac = len(ga & gb) / len(union) if union else 0.0
    return max(ratio, jac)


def is_duplicate(title: str, seen_titles: list, threshold: float = TITLE_DUP_THRESHOLD) -> bool:
    """标题与已见标题（应为已 normalize 的字符串）相似度 >= threshold 即视为重复。"""
    n = normalize_title(title)
    if not n:
        return False
    for s in seen_titles:
        if similarity(n, s) >= threshold:
            return True
    return False


def dedup_url(items: list) -> list:
    """同一批次内按 url_key 去重，保留先出现的，链接存规范化后的可解析形态。"""
    seen = {}
    out = []
    for it in items:
        key = url_key(it.get("url", ""))
        if not key or key in seen:
            continue
        seen[key] = True
        it["url"] = normalize_url(it["url"])
        out.append(it)
    return out
