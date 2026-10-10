"""抓取层：RSS 优先，无 RSS 或 RSS 失败时爬网页（spec 5.1）。

对外的唯一入口是 fetch_all(sources, settings) -> (items, errors)。
item 结构：
    {title, url, summary, source, domain, tier, published}
"""

from __future__ import annotations

import concurrent.futures as futures
import html
import re
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin, urlsplit

import requests
from bs4 import BeautifulSoup

try:
    import feedparser
except ImportError:  # feedparser 缺失时只走网页抓取
    feedparser = None

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
DEFAULT_TIMEOUT = 12
DEFAULT_MAX_PER_SOURCE = 12
# spec v2：时窗放宽到 24h → 48h → 72h，所以抓取窗口直接取最宽的一档
DEFAULT_LOOKBACK_HOURS = 72
DEFAULT_WORKERS = 6

_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")
_ARTICLE_HINT = re.compile(r"(\d{4}[-/]\d{1,2}|/\d{5,}|\.html?$|/article|/news|/detail|/a/|/p/|/post|/story)")

# 网页条目没有 pubDate 时，从链接里抠日期（/2026/10/10/、t20261008_、/2026-10-08/）
_URL_DATE_PATTERNS = (
    re.compile(r"/(20\d{2})/(\d{1,2})/(\d{1,2})/"),
    re.compile(r"t(20\d{2})(\d{2})(\d{2})"),
    re.compile(r"/(20\d{2})-(\d{1,2})-(\d{1,2})"),
    re.compile(r"/(20\d{2})(\d{2})(\d{2})/"),
)


def date_from_url(url: str) -> str | None:
    """能从链接里看出的发布日期，用于过滤历史页面上的陈旧条目。"""
    path = urlsplit(url or "").path
    for pattern in _URL_DATE_PATTERNS:
        m = pattern.search(path)
        if not m:
            continue
        try:
            y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
            if 2000 <= y <= 2100 and 1 <= mo <= 12 and 1 <= d <= 31:
                return datetime(y, mo, d).isoformat(timespec="seconds")
        except (TypeError, ValueError, IndexError):
            continue
    return None


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    })
    return s


def clean_text(raw: str, limit: int = 400) -> str:
    """去标签、解实体、压空白、截断。spec 第 9 节：只存必要字段，不存全文。"""
    if not raw:
        return ""
    text = html.unescape(str(raw))
    text = _TAG.sub(" ", text)
    text = _WS.sub(" ", text).strip()
    if len(text) > limit:
        text = text[:limit].rstrip() + "…"
    return text


def _parse_time(value) -> str | None:
    """把各种时间结构转成 ISO8601 字符串；无法识别返回 None。"""
    if value is None:
        return None
    if isinstance(value, time.struct_time):
        try:
            return datetime(*value[:6]).isoformat(timespec="seconds")
        except (TypeError, ValueError):
            return None
    if isinstance(value, datetime):
        return value.isoformat(timespec="seconds")
    text = str(value).strip()
    if not text:
        return None
    text = text.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text).isoformat(timespec="seconds")
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d",
                "%a, %d %b %Y %H:%M:%S %z", "%a, %d %b %Y %H:%M:%S %Z"):
        try:
            return datetime.strptime(text, fmt).isoformat(timespec="seconds")
        except ValueError:
            continue
    return None


def _fresh_enough(published: str | None, lookback_hours: int) -> bool:
    """有时间才过滤；没时间的网页条目一律保留。"""
    if not published or lookback_hours <= 0:
        return True
    try:
        dt = datetime.fromisoformat(published)
    except ValueError:
        return True
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt >= datetime.now() - timedelta(hours=lookback_hours)


def source_tier(source: dict) -> int:
    """源的 Tier 1–4。没写或写错的按 Tier 4（未知/自媒体）处理。"""
    try:
        tier = int(source.get("tier") or 4)
    except (TypeError, ValueError):
        tier = 4
    return tier if 1 <= tier <= 4 else 4


def _item(source: dict, title: str, url: str, summary: str, published: str | None,
          published_source: str = "unknown") -> dict | None:
    title = clean_text(title, 160)
    summary = clean_text(summary, 400)
    if not title or len(title) < 6 or not url:
        return None
    return {
        "title": title,
        "url": url,
        "summary": summary,
        "source": source.get("name", ""),
        "domain": source.get("domain", ""),
        "tier": source_tier(source),
        "type": source.get("type", "other"),
        "published": published,
        "published_at": published,
        "published_at_source": published_source if published else "unknown",
        "source_lang": detect_lang(f"{title} {summary}"),
    }


def detect_lang(text: str) -> str:
    """原文语言：汉字与拉丁字母比，汉字占比 ≥ 30% 且至少 5 个汉字判为 zh。"""
    text = text or ""
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    latin = sum(1 for ch in text if ch.isalpha() and not ("\u4e00" <= ch <= "\u9fff"))
    if cjk >= 5 and cjk / max(1, cjk + latin) >= 0.3:
        return "zh"
    return "en"


# ---------------------------------------------------------------- RSS

def fetch_rss(source: dict, session: requests.Session, settings: dict) -> list:
    url = source.get("rss")
    if not url or feedparser is None:
        return []
    resp = session.get(url, timeout=settings["timeout"])
    resp.raise_for_status()
    feed = feedparser.parse(resp.content)
    if getattr(feed, "bozo", 0) and not feed.entries:
        raise ValueError("RSS 解析失败")
    out, dated = [], []
    for entry in feed.entries[: settings["max_per_source"] * 2]:
        link = (entry.get("link") or "").strip()
        title = entry.get("title") or ""
        summary = entry.get("summary") or entry.get("description") or ""
        published = _parse_time(
            entry.get("published_parsed") or entry.get("updated_parsed")
            or entry.get("published") or entry.get("updated")
        )
        if published:
            dated.append(published)
        if not _fresh_enough(published, settings["lookback_hours"]):
            continue
        it = _item(source, title, link, summary, published, "rss")
        if it:
            out.append(it)
    # 整份 feed 的最新一条都过期 → 该源已停更，交给网页抓取，别喂陈旧新闻
    if dated and not _fresh_enough(sorted(dated)[-1], settings["lookback_hours"]):
        return []
    return out


# ---------------------------------------------------------------- 网页

def _align_host(url: str, page: str) -> str:
    """有些站点用 //example.com/ 写链接，而站点其实只认 www.example.com。

    这里只在“页面主机 = www. + 链接主机”时把主机名对齐，别的域名一律不动。
    """
    link, base = urlsplit(url), urlsplit(page)
    if base.netloc.startswith("www.") and link.netloc == base.netloc[4:]:
        return urlunsplit(("http", base.netloc, link.path, link.query, ""))
    return url


def fetch_web(source: dict, session: requests.Session, settings: dict) -> list:
    web = source.get("web") or {}
    page = web.get("page") or source.get("page")
    if not page:
        return []
    resp = session.get(page, timeout=settings["timeout"])
    resp.raise_for_status()
    if not resp.encoding or resp.encoding.lower() == "iso-8859-1":
        resp.encoding = resp.apparent_encoding or "utf-8"
    soup = BeautifulSoup(resp.text, "html.parser")

    containers = soup.select(web["list"]) if web.get("list") else [soup]
    include = re.compile(web["include"]) if web.get("include") else None
    link_sel = web.get("link", "a")
    title_sel = web.get("title")
    summary_sel = web.get("summary")

    out, seen = [], set()
    for box in containers:
        # list 选择器可能直接命中 <a> 元素，此时容器本身就是链接
        anchors = list(box.select(link_sel)) if hasattr(box, "select") else []
        if getattr(box, "name", "") == "a":
            anchors.insert(0, box)
        for a in anchors:
            href = (a.get("href") or "").strip()
            if not href or href.startswith(("#", "javascript:", "mailto:")):
                continue
            full = urljoin(page, href)
            full = _align_host(full, page)
            if include and not include.search(full):
                continue
            if not include and not _ARTICLE_HINT.search(urlsplit(full).path):
                continue
            if full in seen:
                continue
            title_el = a.select_one(title_sel) if title_sel else a
            title = (title_el.get("title") if title_el else "") or (title_el.get_text() if title_el else "")
            summary = ""
            if summary_sel:
                s_el = box.select_one(summary_sel)
                summary = s_el.get_text(" ", strip=True) if s_el else ""
            published = date_from_url(full)
            if not _fresh_enough(published, settings["lookback_hours"]):
                continue
            it = _item(source, title, full, summary, published, "webpage")
            if not it:
                continue
            seen.add(full)
            out.append(it)
            if len(out) >= settings["max_per_source"]:
                return out
    return out


def fetch_source(source: dict, session: requests.Session, settings: dict) -> tuple:
    """单个源：RSS 优先，失败或为空则爬网页。返回 (items, error)。"""
    errors = []
    for mode, fn in (("rss", fetch_rss), ("web", fetch_web)):
        try:
            items = fn(source, session, settings)
            if items:
                return items, None
            errors.append(f"{mode}: 无条目")
        except Exception as exc:  # 单源失败不影响整批
            errors.append(f"{mode}: {type(exc).__name__} {exc}")
    return [], "; ".join(errors) if errors else "无可用配置"


_SUMMARY_MIN = 20

# 文章页里能表示发布时间的 meta / 标签
_TIME_META = (
    ("article:published_time", "property"),
    ("og:published_time", "property"),
    ("published_time", "property"),
    ("pubdate", "name"),
    ("publishdate", "name"),
    ("date", "name"),
    ("og:release_date", "property"),
    ("weibo:article:create_at", "property"),
)


def page_published(soup: BeautifulSoup) -> str | None:
    """从文章页提取发布时间：meta → <time datetime> → 正文里的日期。"""
    for name, attr in _TIME_META:
        tag = soup.find("meta", attrs={attr: name})
        if tag and tag.get("content"):
            parsed = _parse_time(tag["content"])
            if parsed:
                return parsed
    tag = soup.find("time")
    if tag:
        parsed = _parse_time(tag.get("datetime") or tag.get_text(" ", strip=True))
        if parsed:
            return parsed
    text = soup.get_text(" ", strip=True)[:1500]
    m = re.search(r"(20\d{2})[-年/](\d{1,2})[-月/](\d{1,2})", text)
    if m:
        try:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat(timespec="seconds")
        except ValueError:
            return None
    return None


def article_summary(url: str, session: requests.Session, settings: dict) -> tuple:
    """补摘要与发布时间：返回 (summary, published_at)。优先 meta description / og:description。"""
    resp = session.get(url, timeout=settings.get("timeout", DEFAULT_TIMEOUT))
    resp.raise_for_status()
    if not resp.encoding or resp.encoding.lower() == "iso-8859-1":
        resp.encoding = resp.apparent_encoding or "utf-8"
    soup = BeautifulSoup(resp.text, "html.parser")
    published = page_published(soup)
    for name, attr in (("description", "name"), ("og:description", "property"),
                       ("twitter:description", "name")):
        tag = soup.find("meta", attrs={attr: name})
        text = clean_text(tag.get("content", "") if tag else "", 400)
        if len(text) >= _SUMMARY_MIN:
            return text, published
    for p in soup.find_all("p"):
        text = clean_text(p.get_text(" ", strip=True), 400)
        if len(text) >= _SUMMARY_MIN:
            return text, published
    return "", published


def enrich_summaries(items: list, settings: dict | None = None, workers: int = 4) -> int:
    """就地补齐摘要与发布时间。抓不到就保持原样，不影响主流程。"""
    cfg = {"timeout": 8}
    cfg.update(settings or {})
    targets = [it for it in items
               if len(it.get("summary") or "") < _SUMMARY_MIN
               or not it.get("published_at")]
    if not targets:
        return 0

    def work(item: dict) -> bool:
        try:
            text, published = article_summary(item["url"], _session(), cfg)
        except Exception:
            return False
        filled = False
        if text and len(item.get("summary") or "") < _SUMMARY_MIN:
            item["summary"] = text
            filled = True
        if published and not item.get("published_at"):
            item["published_at"] = published
            item["published"] = published
            item["published_at_source"] = "webpage"
            filled = True
        return filled

    filled = 0
    with futures.ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for ok in pool.map(work, targets):
            filled += 1 if ok else 0
    return filled


def fetch_all(sources: list, settings: dict | None = None) -> tuple:
    """全量扫描所有源（并发）。返回 (items, errors)；errors 为 [{source, error}]。"""
    cfg = {
        "timeout": DEFAULT_TIMEOUT,
        "max_per_source": DEFAULT_MAX_PER_SOURCE,
        "lookback_hours": DEFAULT_LOOKBACK_HOURS,
        "workers": DEFAULT_WORKERS,
    }
    cfg.update(settings or {})
    active = [s for s in sources if s.get("enabled", True) is not False]

    def work(source: dict):
        session = _session()  # 每个线程独立 session
        try:
            got, err = fetch_source(source, session, cfg)
        except Exception as exc:
            got, err = [], f"{type(exc).__name__} {exc}"
        return source, got, err

    items, errors = [], []
    with futures.ThreadPoolExecutor(max_workers=max(1, int(cfg["workers"]))) as pool:
        for source, got, err in pool.map(work, active):
            items.extend(got)
            if err and not got:
                errors.append({"source": source.get("name", "?"), "error": err})
    return items, errors
