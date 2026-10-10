"""SQLite 操作层（spec v2 数据模型）。

按 spec 第 9 节：不引入 ORM，直接写 SQL。
表：news（业务数据，url + module 联合唯一）、meta（运行状态键值）。
本文件同时负责把 v1 的旧库（只有 url 唯一约束）迁移到 v2 结构。
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime

from dedup import url_key

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "news.db")

# spec v2：综合 / AI / 民生 / 社会国内 / 社会国外
MODULES = ["comprehensive", "ai", "livelihood", "society_cn", "society_intl"]
# 综合模块由这四个单独模块混合而来
DOMAIN_MODULES = ["ai", "livelihood", "society_cn", "society_intl"]

TRANSLATION_STATUS = ["已翻译", "未翻译", "部分"]
HOTSPOT_STATUS = ["已生成", "未生成"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS news (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    module              TEXT NOT NULL,
    domain              TEXT,
    title               TEXT NOT NULL,
    title_zh            TEXT,
    title_en            TEXT,
    url                 TEXT NOT NULL,
    url_key             TEXT,
    sources             TEXT,
    summary             TEXT,
    summary_zh          TEXT,
    summary_en          TEXT,
    source_lang         TEXT,
    translation_status  TEXT,
    credibility         TEXT,
    credibility_auto    TEXT,
    credibility_basis   TEXT,
    conflict_note       TEXT,
    oral_prompt         TEXT,
    hotspot_score       INTEGER,
    highlight           TEXT,
    hotspot_status      TEXT,
    user_score          INTEGER,
    user_note           TEXT,
    published_at        TEXT,
    published_at_source TEXT,
    shown_at            TEXT,
    module_shown_at     TEXT,
    created_at          TEXT,
    UNIQUE (url, module)
);

CREATE INDEX IF NOT EXISTS idx_news_url          ON news(url);
CREATE INDEX IF NOT EXISTS idx_news_url_key      ON news(url_key);
CREATE INDEX IF NOT EXISTS idx_news_shown_at     ON news(shown_at);
CREATE INDEX IF NOT EXISTS idx_news_domain       ON news(domain);
CREATE INDEX IF NOT EXISTS idx_news_credibility  ON news(credibility);
CREATE INDEX IF NOT EXISTS idx_news_module       ON news(module);
CREATE INDEX IF NOT EXISTS idx_news_published_at ON news(published_at);
CREATE INDEX IF NOT EXISTS idx_news_module_shown ON news(module, module_shown_at);

CREATE TABLE IF NOT EXISTS meta (
    k TEXT PRIMARY KEY,
    v TEXT
);
"""

# v1 库缺的列：老库升级时逐列补
NEW_COLUMNS = [
    ("module", "TEXT NOT NULL DEFAULT 'comprehensive'"),
    ("title_zh", "TEXT"),
    ("title_en", "TEXT"),
    ("summary_zh", "TEXT"),
    ("summary_en", "TEXT"),
    ("source_lang", "TEXT"),
    ("translation_status", "TEXT"),
    ("hotspot_score", "INTEGER"),
    ("highlight", "TEXT"),
    ("hotspot_status", "TEXT"),
    ("published_at", "TEXT"),
    ("published_at_source", "TEXT"),
    ("module_shown_at", "TEXT"),
    ("credibility_auto", "TEXT"),
    ("credibility_basis", "TEXT"),
    ("conflict_note", "TEXT"),
    ("user_score", "INTEGER"),
    ("user_note", "TEXT"),
    ("url_key", "TEXT"),
]


@contextmanager
def connect():
    """一次连接，自动提交，退出即关闭。"""
    conn = sqlite3.connect(DB_PATH, timeout=20)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _table_exists(conn, name: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone()
    return row is not None


def _columns(conn) -> set:
    if not _table_exists(conn, "news"):
        return set()
    return {r["name"] for r in conn.execute("PRAGMA table_info(news)").fetchall()}


def _legacy_url_unique(conn) -> bool:
    """v1 的 news 表把 UNIQUE 写在 url 列上，会挡住“同一 url 出现在两个模块”。"""
    for row in conn.execute("PRAGMA index_list(news)").fetchall():
        if not row["unique"]:
            continue
        cols = [c["name"] for c in conn.execute(f"PRAGMA index_info('{row['name']}')").fetchall()]
        if cols == ["url"]:
            return True
    return False


def _guess_lang(text: str) -> str:
    """迁移旧数据用：汉字与拉丁字母比，判断原文语言。"""
    text = text or ""
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    latin = sum(1 for ch in text if ch.isalpha() and not ("\u4e00" <= ch <= "\u9fff"))
    if cjk >= 5 and cjk / max(1, cjk + latin) >= 0.3:
        return "zh"
    return "en"


def _migrate_legacy(conn) -> None:
    """把 v1 表重建为 v2 结构：老行归入综合模块，双语字段按原文语言落位。"""
    conn.executescript("ALTER TABLE news RENAME TO news_v1;")
    conn.executescript(SCHEMA)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(news_v1)").fetchall()}

    def pick(name, fallback="NULL"):
        return name if name in cols else fallback

    conn.execute(
        f"""
        INSERT OR IGNORE INTO news
            (module, domain, title, url, sources, summary, source_lang, translation_status,
             credibility, oral_prompt, hotspot_status, published_at, published_at_source,
             shown_at, module_shown_at, created_at)
        SELECT
            'comprehensive',
            {pick('domain')},
            {pick('title', "''")},
            {pick('url', "''")},
            {pick('sources')},
            {pick('summary')},
            NULL,
            '未翻译',
            {pick('credibility')},
            {pick('oral_prompt')},
            '未生成',
            {pick('created_at')},
            'unknown',
            {pick('shown_at')},
            {pick('shown_at')},
            {pick('created_at')}
        FROM news_v1
        """
    )
    conn.executescript("DROP TABLE news_v1;")
    # 老库没有语言字段，按标题+摘要判断，把双语字段放到正确的一侧
    for r in conn.execute("SELECT id, title, summary FROM news").fetchall():
        lang = _guess_lang(f"{r['title']} {r['summary'] or ''}")
        if lang == "zh":
            conn.execute("UPDATE news SET title_zh = ?, summary_zh = ?, source_lang = 'zh' WHERE id = ?",
                         (r["title"], r["summary"], r["id"]))
        else:
            conn.execute("UPDATE news SET title_en = ?, summary_en = ?, source_lang = 'en' WHERE id = ?",
                         (r["title"], r["summary"], r["id"]))


def purge_duplicates() -> int:
    """清掉历史遗留的重复行：同一 url_key + 同一模块只保留最早的一条。

    url_key 会把 http/https、www、utm 等差异归一，v1 时代或旧逻辑入库的数据
    可能因此留下成对的重复卡片（历史页看到的就是它们），启动时自动清一遍。
    """
    removed = 0
    with connect() as conn:
        rows = conn.execute("SELECT id, url, url_key, module FROM news ORDER BY id ASC").fetchall()
        seen = {}
        drop = []
        for r in rows:
            key = ((r["url_key"] or url_key(r["url"])), r["module"])
            if key in seen:
                drop.append(r["id"])
            else:
                seen[key] = r["id"]
        for news_id in drop:
            conn.execute("DELETE FROM news WHERE id = ?", (news_id,))
            removed += 1
    return removed


def backfill_url_key() -> int:
    """给老数据补 url_key，并让历史页能按它跨模块去重。"""
    filled = 0
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, url, url_key FROM news WHERE url_key IS NULL OR url_key = ''"
        ).fetchall()
        for r in rows:
            conn.execute("UPDATE news SET url_key = ? WHERE id = ?", (url_key(r["url"]), r["id"]))
            filled += 1
    return filled


def init_db() -> dict:
    """建表 + 迁移 + 清重复。返回本次做了什么（方便自检与日志）。"""
    fresh = not os.path.exists(DB_PATH)
    with connect() as conn:
        actions = []
        if _table_exists(conn, "news") and _legacy_url_unique(conn):
            _migrate_legacy(conn)
            actions.append("legacy_table_rebuilt")
        conn.executescript(SCHEMA)
        cols = _columns(conn)
        for name, ddl in NEW_COLUMNS:
            if name not in cols:
                conn.execute(f"ALTER TABLE news ADD COLUMN {name} {ddl}")
                actions.append(f"added_column:{name}")
        # 补齐元数据字段，保证查询排序稳定
        conn.execute("UPDATE news SET module = 'comprehensive' WHERE module IS NULL OR module = ''")
        conn.execute("UPDATE news SET title_zh = title WHERE title_zh IS NULL AND source_lang = 'zh'")
        conn.execute("UPDATE news SET summary_zh = summary WHERE summary_zh IS NULL AND source_lang = 'zh'")
        conn.execute("UPDATE news SET hotspot_status = '未生成' WHERE hotspot_status IS NULL")
        conn.execute("UPDATE news SET translation_status = '未翻译' WHERE translation_status IS NULL")
        conn.execute("UPDATE news SET published_at_source = 'unknown' WHERE published_at_source IS NULL")
        conn.execute("UPDATE news SET shown_at = created_at WHERE shown_at IS NULL")
        conn.execute("UPDATE news SET module_shown_at = shown_at WHERE module_shown_at IS NULL")
        # 老数据没有 credibility_auto：把当前标签视为自动判定结果
        conn.execute("UPDATE news SET credibility_auto = credibility WHERE credibility_auto IS NULL")
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_news_url_module ON news(url, module)"
        )
    # 顺序很重要：先补 url_key，再清重复，最后才能建 (url_key, module) 唯一索引
    backfilled = backfill_url_key()
    removed = purge_duplicates()
    with connect() as conn:
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_news_url_key_module ON news(url_key, module)"
        )
    return {"fresh": fresh, "actions": actions, "url_key_backfilled": backfilled,
            "duplicates_removed": removed}


# ---------------------------------------------------------------- 写入

FIELDS = [
    "module", "domain", "title", "title_zh", "title_en", "url", "url_key", "sources", "summary",
    "summary_zh", "summary_en", "source_lang", "translation_status", "credibility",
    "credibility_auto", "credibility_basis", "conflict_note", "oral_prompt", "hotspot_score",
    "highlight", "hotspot_status", "user_score", "user_note", "published_at",
    "published_at_source", "shown_at", "module_shown_at", "created_at",
]


def insert_news(item: dict, module: str, module_shown_at: str) -> bool:
    """插入一条新闻；url + module 撞唯一索引则忽略，返回是否真的入库。"""
    row = dict(item)
    row["module"] = module
    row["module_shown_at"] = module_shown_at
    row.setdefault("shown_at", row["module_shown_at"])
    row.setdefault("created_at", row["module_shown_at"])
    # url_key：归一化后的比较键（去掉 utm、统一 http/https、忽略 www），入库时就存下来
    row["url_key"] = row.get("url_key") or url_key(row.get("url") or "")
    # 默认值：新入库的一律未翻译、未评价、发布时间出处未知（回填后才变）
    if not row.get("translation_status"):
        row["translation_status"] = "未翻译"
    if not row.get("hotspot_status"):
        row["hotspot_status"] = "未生成"
    if not row.get("published_at_source"):
        row["published_at_source"] = "unknown"
    if isinstance(row.get("sources"), (list, dict)):
        row["sources"] = json.dumps(row["sources"], ensure_ascii=False)
    row["sources"] = row.get("sources") or "[]"
    values = [row.get(f) for f in FIELDS]
    sql = (
        "INSERT OR IGNORE INTO news (" + ", ".join(FIELDS) + ") "
        "VALUES (" + ", ".join("?" for _ in FIELDS) + ")"
    )
    with connect() as conn:
        cur = conn.execute(sql, values)
        return cur.rowcount > 0


def update_credibility(news_id: int, credibility: str) -> bool:
    with connect() as conn:
        cur = conn.execute("UPDATE news SET credibility = ? WHERE id = ?", (credibility, news_id))
        return cur.rowcount > 0


def update_translation(news_id: int, lang: str, title: str, summary: str) -> dict | None:
    """人工回填译文。lang 为目标语言：en → 写 title_en/summary_en，zh → 写 _zh。"""
    if lang == "en":
        title_col, summary_col = "title_en", "summary_en"
    else:
        title_col, summary_col = "title_zh", "summary_zh"
    with connect() as conn:
        row = conn.execute("SELECT * FROM news WHERE id = ?", (news_id,)).fetchone()
        if not row:
            return None
        sets, params = [], []
        if title:
            sets.append(f"{title_col} = ?")
            params.append(title)
        if summary:
            sets.append(f"{summary_col} = ?")
            params.append(summary)
        if sets:
            params.append(news_id)
            conn.execute(f"UPDATE news SET {', '.join(sets)} WHERE id = ?", params)
        row = conn.execute("SELECT * FROM news WHERE id = ?", (news_id,)).fetchone()
    status = translation_status_of(dict(row))
    with connect() as conn:
        conn.execute("UPDATE news SET translation_status = ? WHERE id = ?", (status, news_id))
    out = dict(row)
    out["translation_status"] = status
    return out


def update_evaluation(news_id: int, score, highlight: str) -> dict | None:
    """人工回填热点评分与看点（系统绝不自动生成）。"""
    with connect() as conn:
        if not conn.execute("SELECT id FROM news WHERE id = ?", (news_id,)).fetchone():
            return None
        conn.execute(
            "UPDATE news SET hotspot_score = ?, highlight = ?, hotspot_status = ? WHERE id = ?",
            (score, highlight, "已生成" if score else "未生成", news_id),
        )
        row = conn.execute("SELECT * FROM news WHERE id = ?", (news_id,)).fetchone()
    return dict(row)


def update_user_score(news_id: int, score, note: str | None) -> dict | None:
    """内容评分（1–5 星）+ 可选一句话备注。score=None 表示清除评分。"""
    with connect() as conn:
        if not conn.execute("SELECT id FROM news WHERE id = ?", (news_id,)).fetchone():
            return None
        if note is None:
            conn.execute("UPDATE news SET user_score = ? WHERE id = ?", (score, news_id))
        else:
            conn.execute("UPDATE news SET user_score = ?, user_note = ? WHERE id = ?",
                         (score, note, news_id))
        row = conn.execute("SELECT * FROM news WHERE id = ?", (news_id,)).fetchone()
    return dict(row)


def translation_status_of(row: dict) -> str:
    """按译文完整度判定：两个语种都齐=已翻译，一条都没有=未翻译，其余=部分。"""
    source_lang = row.get("source_lang") or "zh"
    zh_title = row.get("title_zh") or (row.get("title") if source_lang == "zh" else None)
    zh_summary = row.get("summary_zh") or (row.get("summary") if source_lang == "zh" else None)
    en_title = row.get("title_en") or (row.get("title") if source_lang == "en" else None)
    en_summary = row.get("summary_en") or (row.get("summary") if source_lang == "en" else None)
    have_zh = bool(zh_title) and bool(zh_summary)
    have_en = bool(en_title) and bool(en_summary)
    if have_zh and have_en:
        return "已翻译"
    # 原文语言那一侧本来就有，所以只有“另一侧也没有”才算完全未翻译
    translated_side = (row.get("title_en") or row.get("summary_en")) if source_lang == "zh" \
        else (row.get("title_zh") or row.get("summary_zh"))
    if not translated_side:
        return "未翻译"
    return "部分"


# ---------------------------------------------------------------- 读取

def row_to_dict(row) -> dict:
    d = dict(row)
    try:
        d["sources"] = json.loads(d.get("sources") or "[]")
    except (TypeError, ValueError):
        d["sources"] = []
    return d


def _filters(where: list, params: list, domain=None, credibility=None, module=None,
             min_hotspot=None, q=None, user_score=None, min_user_score=None) -> None:
    if domain:
        where.append("domain = ?")
        params.append(domain)
    if credibility:
        where.append("credibility = ?")
        params.append(credibility)
    if module:
        where.append("module = ?")
        params.append(module)
    if min_hotspot:
        where.append("hotspot_score IS NOT NULL AND hotspot_score >= ?")
        params.append(int(min_hotspot))
    if user_score:
        where.append("user_score = ?")
        params.append(int(user_score))
    if min_user_score:
        where.append("user_score IS NOT NULL AND user_score >= ?")
        params.append(int(min_user_score))
    if q:
        # 只搜标题与摘要（spec 禁止对新闻全文做全文搜索，库里本来也不存全文）
        like = f"%{q}%"
        where.append(
            "(title LIKE ? OR title_zh LIKE ? OR title_en LIKE ? "
            "OR summary LIKE ? OR summary_zh LIKE ? OR summary_en LIKE ?)"
        )
        params.extend([like] * 6)


def _order(sort: str) -> str:
    if sort == "hotspot":
        return "ORDER BY (hotspot_score IS NULL), hotspot_score DESC, (published_at IS NULL), published_at DESC"
    if sort == "shown":
        return "ORDER BY module_shown_at DESC, id DESC"
    # 默认：新闻发布时间倒序，时间未知排最后
    return "ORDER BY (published_at IS NULL), published_at DESC, id DESC"


def today_items(module: str, page: int = 1, page_size: int = 10, sort: str = "published",
                day: str | None = None, **kw) -> tuple:
    """某模块“今日”条目：今天展示过的全部（一天内多次刷新会累积），分页返回。

    模块内 url_key 有唯一索引，所以这里天然不会有重复卡片。
    """
    day = day or datetime.now().strftime("%Y-%m-%d")
    where, params = ["module = ?", "substr(module_shown_at, 1, 10) = ?"], [module, day]
    _filters(where, params, **kw)
    where_sql = " AND ".join(where)
    offset = max(0, (page - 1) * page_size)
    with connect() as conn:
        total = conn.execute(f"SELECT COUNT(*) AS c FROM news WHERE {where_sql}", params).fetchone()["c"]
        rows = conn.execute(
            f"SELECT * FROM news WHERE {where_sql} {_order(sort)} LIMIT ? OFFSET ?",
            params + [page_size, offset],
        ).fetchall()
    return [row_to_dict(r) for r in rows], total


def latest_module_batch(module: str, limit: int = 60, **kw) -> list:
    """该模块最近一次刷新的批次（综合模块按配额混合时从这里取）。"""
    where, params = ["module = ?"], [module]
    _filters(where, params, **kw)
    where_sql = " AND ".join(where)
    with connect() as conn:
        row = conn.execute(
            f"SELECT MAX(module_shown_at) AS m FROM news WHERE {where_sql}", params
        ).fetchone()
        stamp = row["m"] if row else None
        if not stamp:
            return []
        rows = conn.execute(
            f"SELECT * FROM news WHERE {where_sql} AND module_shown_at = ? "
            "ORDER BY (published_at IS NULL), published_at DESC, id DESC LIMIT ?",
            params + [stamp, limit],
        ).fetchall()
    return [row_to_dict(r) for r in rows]


def history_items(page: int = 1, page_size: int = 20, date: str | None = None,
                  sort: str = "published", dedup: bool = True, **kw) -> tuple:
    """历史列表：可按日期/模块/可信度/我的评分/关键词筛选，只读。

    dedup=True 时按 url_key 跨模块去重：同一条新闻可能既在综合又在 AI 模块里，
    历史列表只显示一条（优先保留领域模块那一行，它信息更具体）。
    """
    where, params = [], []
    if date:
        where.append("(substr(published_at, 1, 10) = ? OR (published_at IS NULL AND substr(module_shown_at, 1, 10) = ?))")
        params.extend([date, date])
    _filters(where, params, **kw)
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""
    offset = max(0, (page - 1) * page_size)

    with connect() as conn:
        if dedup:
            keep_sql = (
                "SELECT COALESCE(MIN(CASE WHEN module <> 'comprehensive' THEN id END), MIN(id)) "
                f"FROM news{where_sql} GROUP BY COALESCE(url_key, url)"
            )
            total = conn.execute(
                f"SELECT COUNT(*) AS c FROM (SELECT 1 FROM news{where_sql} "
                "GROUP BY COALESCE(url_key, url))",
                params,
            ).fetchone()["c"]
            rows = conn.execute(
                f"SELECT * FROM news WHERE id IN ({keep_sql}) {_order(sort)} LIMIT ? OFFSET ?",
                params + [page_size, offset],
            ).fetchall()
        else:
            total = conn.execute(f"SELECT COUNT(*) AS c FROM news{where_sql}", params).fetchone()["c"]
            rows = conn.execute(
                f"SELECT * FROM news{where_sql} {_order(sort)} LIMIT ? OFFSET ?",
                params + [page_size, offset],
            ).fetchall()
    return [row_to_dict(r) for r in rows], total


def get_news(news_id: int) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM news WHERE id = ?", (news_id,)).fetchone()
    return row_to_dict(row) if row else None


def module_urls(module: str) -> set:
    """该模块已入库的 url（模块内永久去重）。"""
    with connect() as conn:
        rows = conn.execute("SELECT url FROM news WHERE module = ?", (module,)).fetchall()
    return {r["url"] for r in rows if r["url"]}


def module_titles(module: str) -> list:
    with connect() as conn:
        rows = conn.execute("SELECT title, title_zh, title_en FROM news WHERE module = ?", (module,)).fetchall()
    out = []
    for r in rows:
        for key in ("title", "title_zh", "title_en"):
            if r[key]:
                out.append(r[key])
    return out


def module_dates(module: str) -> list:
    """历史筛选用：库里已有的日期。"""
    with connect() as conn:
        rows = conn.execute(
            "SELECT DISTINCT substr(COALESCE(published_at, module_shown_at), 1, 10) AS d "
            "FROM news WHERE module = ? ORDER BY d DESC LIMIT 90",
            (module,),
        ).fetchall()
    return [r["d"] for r in rows if r["d"]]


def module_counts(module: str, day: str | None = None) -> dict:
    """今日该模块的条数与热点分布，用于配额显示。"""
    day = day or datetime.now().strftime("%Y-%m-%d")
    with connect() as conn:
        rows = conn.execute(
            "SELECT domain, credibility, hotspot_score FROM news "
            "WHERE module = ? AND substr(module_shown_at, 1, 10) = ?",
            (module, day),
        ).fetchall()
    by_domain, hot, normal, unscored = {}, 0, 0, 0
    for r in rows:
        by_domain[r["domain"]] = by_domain.get(r["domain"], 0) + 1
        score = r["hotspot_score"]
        if score is None:
            unscored += 1
        elif score >= 3:
            hot += 1
        else:
            normal += 1
    return {"total": len(rows), "by_domain": by_domain, "hot": hot, "normal": normal,
            "unscored": unscored, "day": day}


def stats() -> dict:
    with connect() as conn:
        total = conn.execute("SELECT COUNT(*) AS c FROM news").fetchone()["c"]
        today = conn.execute(
            "SELECT COUNT(*) AS c FROM news WHERE substr(module_shown_at, 1, 10) = ?",
            (datetime.now().strftime("%Y-%m-%d"),),
        ).fetchone()["c"]
    return {"total": total, "today": today}


# ---------------------------------------------------------------- meta

def set_meta(**kv) -> None:
    with connect() as conn:
        for k, v in kv.items():
            conn.execute(
                "INSERT INTO meta (k, v) VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET v = excluded.v",
                (k, json.dumps(v, ensure_ascii=False)),
            )


def get_meta() -> dict:
    with connect() as conn:
        rows = conn.execute("SELECT k, v FROM meta").fetchall()
    out = {}
    for r in rows:
        try:
            out[r["k"]] = json.loads(r["v"])
        except (TypeError, ValueError):
            out[r["k"]] = r["v"]
    return out


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")
