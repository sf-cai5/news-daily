"""SQLite 操作层（spec v2 数据模型 + v2.3 投票/来源权重）。

按 spec 第 9 节：不引入 ORM，直接写 SQL。
表：news（业务数据，url + module 联合唯一）、source_weights（来源权重）、meta（运行状态键值）。
本文件同时负责把 v1 的旧库（只有 url 唯一约束）迁移到 v2 结构，
并在启动时补上 v2.3 的三列（user_vote / hidden / hidden_at）与 source_weights 表。
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

# spec v2.3：👍/👎 投票与来源权重
VOTE_VALUES = ["up", "down", "none"]
# 每种态度对来源权重的贡献：👍 +1，👎 -1，没态度 0（权重永远等于当前态度，不累加刷分）
VOTE_CONTRIBUTION = {"up": 1, "down": -1, "none": 0}
WEIGHT_MIN = -10           # 下限：不得无限下降
WEIGHT_MAX = 10            # 上限：不得无限增长
WEIGHT_DECAY_RATIO = 0.9   # 每月自动衰减 10%
WEIGHT_DECAY_KEY = "weight_decay_month"   # meta 里记录上次衰减的月份，保证每月只衰减一次

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
    user_vote           TEXT DEFAULT 'none',
    hidden              INTEGER DEFAULT 0,
    hidden_at           TEXT,
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
-- 注意：idx_news_hidden 不能在 SCHEMA 里建。老库的 news 表还没有 hidden 列，
-- 而 ALTER TABLE 补列是在 executescript(SCHEMA) 之后才跑的，这里建索引会报 no such column。
-- 于是它统一在 init_db() 的补列循环之后创建（见 init_db）。

-- spec v2.3：来源权重（点👍来源 +1、点👎来源 -1，范围 -10 ~ +10）
CREATE TABLE IF NOT EXISTS source_weights (
    source_name TEXT PRIMARY KEY,
    weight      INTEGER DEFAULT 0,
    updated_at  TEXT
);

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
    # spec v2.3 的三列迁移（老库没有表结构时会在这里逐列补上）
    ("user_vote", "TEXT DEFAULT 'none'"),
    ("hidden", "INTEGER DEFAULT 0"),
    ("hidden_at", "TEXT"),
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
        # hidden 列补好之后才建索引（老库升级路径，见 SCHEMA 里的注释）
        conn.execute("CREATE INDEX IF NOT EXISTS idx_news_hidden ON news(hidden)")
        # 补齐元数据字段，保证查询排序稳定
        conn.execute("UPDATE news SET module = 'comprehensive' WHERE module IS NULL OR module = ''")
        conn.execute("UPDATE news SET title_zh = title WHERE title_zh IS NULL AND source_lang = 'zh'")
        conn.execute("UPDATE news SET summary_zh = summary WHERE summary_zh IS NULL AND source_lang = 'zh'")
        conn.execute("UPDATE news SET hotspot_status = '未生成' WHERE hotspot_status IS NULL")
        conn.execute("UPDATE news SET translation_status = '未翻译' WHERE translation_status IS NULL")
        conn.execute("UPDATE news SET published_at_source = 'unknown' WHERE published_at_source IS NULL")
        conn.execute("UPDATE news SET shown_at = created_at WHERE shown_at IS NULL")
        conn.execute("UPDATE news SET module_shown_at = shown_at WHERE module_shown_at IS NULL")
        # spec v2.3：老数据默认没投过票、没被隐藏
        conn.execute("UPDATE news SET user_vote = 'none' WHERE user_vote IS NULL OR user_vote = ''")
        conn.execute("UPDATE news SET hidden = 0 WHERE hidden IS NULL")
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
    "highlight", "hotspot_status", "user_score", "user_note", "user_vote", "hidden", "hidden_at",
    "published_at", "published_at_source", "shown_at", "module_shown_at", "created_at",
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
    # spec v2.3：新入库的一律没投过票、没被隐藏
    row["user_vote"] = row.get("user_vote") or "none"
    if row.get("hidden") is None:
        row["hidden"] = 0
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


# ---------------------------------------------------------------- 来源权重（spec v2.3）

def clamp_weight(value) -> int:
    """权重限死在 -10 ~ +10：不无限增长，也不无限下降。"""
    try:
        weight = int(value)
    except (TypeError, ValueError):
        weight = 0
    return max(WEIGHT_MIN, min(WEIGHT_MAX, weight))


def source_weights_map() -> dict:
    """来源名 → 权重（没记录过的来源视为 0）。表还不存在时返回空字典。"""
    try:
        with connect() as conn:
            rows = conn.execute("SELECT source_name, weight FROM source_weights").fetchall()
    except sqlite3.OperationalError:
        return {}
    return {r["source_name"]: clamp_weight(r["weight"]) for r in rows}


def source_weight_rows() -> list:
    """给 /api/source-weights 用：全部有记录的来源，权重高的在前。"""
    try:
        with connect() as conn:
            rows = conn.execute(
                "SELECT source_name, weight, updated_at FROM source_weights "
                "ORDER BY weight DESC, source_name"
            ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [{"source_name": r["source_name"], "weight": clamp_weight(r["weight"]),
             "updated_at": r["updated_at"] or ""} for r in rows]


def get_source_weight(name: str) -> int:
    return source_weights_map().get(name or "", 0)


def _upsert_weight(conn, name: str, delta: int, at: str | None = None) -> int:
    """同一个连接里「读 → 加 delta → 夹到 -10~+10 → 写回」，返回新权重。"""
    row = conn.execute("SELECT weight FROM source_weights WHERE source_name = ?", (name,)).fetchone()
    current = clamp_weight(row["weight"]) if row else 0
    new = clamp_weight(current + int(delta))
    conn.execute(
        "INSERT INTO source_weights (source_name, weight, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(source_name) DO UPDATE SET weight = excluded.weight, updated_at = excluded.updated_at",
        (name, new, at or now_iso()),
    )
    return new


def set_source_weight(name: str, weight) -> int:
    """直接设置权重（人工校正 / 自检用），仍会夹在 -10 ~ +10。"""
    name = (name or "").strip()
    value = clamp_weight(weight)
    if not name:
        return value
    with connect() as conn:
        conn.execute(
            "INSERT INTO source_weights (source_name, weight, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(source_name) DO UPDATE SET weight = excluded.weight, updated_at = excluded.updated_at",
            (name, value, now_iso()),
        )
    return value


def decay_weight(value, ratio: float = WEIGHT_DECAY_RATIO) -> int:
    """每月衰减 10%：向 0 靠拢但不变号（8 → 7，-8 → -7，1 → 0）。"""
    try:
        weight = int(value)
    except (TypeError, ValueError):
        return 0
    return clamp_weight(int(weight * ratio))


def current_month(now: datetime | None = None) -> str:
    return (now or datetime.now()).strftime("%Y-%m")


def decay_state(now: datetime | None = None) -> dict:
    """权重衰减状态：上一次衰减的月份、当前月份、本月是否还没衰减。"""
    month = current_month(now)
    last = get_meta().get(WEIGHT_DECAY_KEY) or ""
    return {"ratio": WEIGHT_DECAY_RATIO, "last_month": last, "month": month,
            "pending": last != month}


def decay_source_weights(now: datetime | None = None, force: bool = False) -> dict:
    """每月自动衰减 10%，防止偏见累积；同一个月只衰减一次（月份记在 meta 里）。"""
    now = now or datetime.now()
    month = now.strftime("%Y-%m")
    last = get_meta().get(WEIGHT_DECAY_KEY)
    if not force and last == month:
        return {"decayed": False, "month": month, "last_month": last, "sources": 0}
    changed = 0
    with connect() as conn:
        rows = conn.execute("SELECT source_name, weight FROM source_weights").fetchall()
        for r in rows:
            old, new = clamp_weight(r["weight"]), decay_weight(r["weight"])
            if new != old:
                conn.execute(
                    "UPDATE source_weights SET weight = ?, updated_at = ? WHERE source_name = ?",
                    (new, now_iso(), r["source_name"]),
                )
                changed += 1
        conn.execute(
            "INSERT INTO meta (k, v) VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET v = excluded.v",
            (WEIGHT_DECAY_KEY, json.dumps(month, ensure_ascii=False)),
        )
    return {"decayed": True, "month": month, "last_month": last, "sources": changed}


def _source_names(row) -> list:
    """一条新闻的来源名列表（sources 是 JSON 数组文本）。"""
    try:
        return [str(s) for s in json.loads(row["sources"] or "[]") if s]
    except (TypeError, ValueError):
        return []


def update_vote(news_id: int, action: str) -> dict | None:
    """👍/👎：写 user_vote、按需隐藏，并同步该条来源的权重。

    - up：user_vote=up，取消隐藏；该来源权重 +1
    - down：user_vote=down，hidden=1，记 hidden_at；该来源权重 -1
    - none：撤销投票：解除隐藏，权重回到「没态度」

    权重永远等于「当前态度」而不是累加点击次数：同一种态度连点多次只算一次
    （👍→👍 不变），从赞改成踩就是 -2（把那次 +1 也撤掉），所以反复点踩最多把
    权重压到 -10，符合 spec「不得无限下降」。
    """
    action = (action or "").strip().lower()
    if action not in VOTE_VALUES:
        return None
    at = now_iso()
    with connect() as conn:
        row = conn.execute("SELECT * FROM news WHERE id = ?", (news_id,)).fetchone()
        if not row:
            return None
        prev = (row["user_vote"] or "none").lower()
        if prev not in VOTE_CONTRIBUTION:
            prev = "none"
        delta = VOTE_CONTRIBUTION[action] - VOTE_CONTRIBUTION[prev]
        weights = {}
        if delta:
            for name in _source_names(row):
                weights[name] = _upsert_weight(conn, name, delta, at)
        if action == "down":
            conn.execute("UPDATE news SET user_vote = 'down', hidden = 1, hidden_at = ? WHERE id = ?",
                         (at, news_id))
        else:
            conn.execute("UPDATE news SET user_vote = ?, hidden = 0, hidden_at = NULL WHERE id = ?",
                         ("up" if action == "up" else "none", news_id))
        updated = conn.execute("SELECT * FROM news WHERE id = ?", (news_id,)).fetchone()
    out = dict(updated)
    out["weights"] = weights
    out["weight_delta"] = delta
    out["previous_vote"] = prev
    return out


def unhide_news(news_id: int) -> dict | None:
    """取消隐藏：恢复展示，并撤销那次点踩（权重 +1 回退）。

    隐藏是按 url_key 生效的（同一条新闻的多个模块行一起从今日页消失），
    所以取消隐藏也按 url_key 一起恢复，避免「综合行还藏着」的残留状态。
    """
    at = now_iso()
    with connect() as conn:
        row = conn.execute("SELECT * FROM news WHERE id = ?", (news_id,)).fetchone()
        if not row:
            return None
        key = row["url_key"] or url_key(row["url"] or "")
        siblings = conn.execute(
            "SELECT * FROM news WHERE IFNULL(url_key, url) = ?", (key,)
        ).fetchall()
        weights, restored = {}, []
        for r in siblings:
            prev = (r["user_vote"] or "none").lower()
            if prev == "down":            # 只有真的点过踩的行才回退权重
                for name in _source_names(r):
                    weights[name] = _upsert_weight(conn, name, 1, at)
                conn.execute(
                    "UPDATE news SET user_vote = 'none', hidden = 0, hidden_at = NULL WHERE id = ?",
                    (r["id"],),
                )
            else:
                conn.execute("UPDATE news SET hidden = 0, hidden_at = NULL WHERE id = ?", (r["id"],))
            restored.append(r["id"])
        updated = conn.execute("SELECT * FROM news WHERE id = ?", (news_id,)).fetchone()
    out = dict(updated)
    out["weights"] = weights
    out["weight_delta"] = 1 if weights else 0
    out["previous_vote"] = (row["user_vote"] or "none").lower()
    out["restored_ids"] = restored
    return out


def hidden_url_keys() -> set:
    """被隐藏新闻的 url_key 集合：今日页据此整条隐藏（跨模块一起消失）。"""
    with connect() as conn:
        rows = conn.execute("SELECT url, url_key FROM news WHERE hidden = 1").fetchall()
    return {(r["url_key"] or url_key(r["url"])) for r in rows}


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

def _json1_available() -> bool:
    """排序要按来源权重，需要 json_extract；没有 JSON1 就退化成纯时间排序。"""
    global _JSON1
    if _JSON1 is None:
        try:
            sqlite3.connect(":memory:").execute("SELECT json_valid('[]')").fetchone()
            _JSON1 = True
        except sqlite3.Error:
            _JSON1 = False
    return _JSON1


_JSON1 = None

# 该行的来源权重：取第一个来源（sources JSON 数组的第一个元素）在 source_weights 里的权重
_WEIGHT_KEY = (
    "COALESCE((SELECT w.weight FROM source_weights w WHERE w.source_name = "
    "CASE WHEN json_valid(news.sources) THEN json_extract(news.sources, '$[0]') END), 0)"
)

# spec v2.3：点踩过的新闻从今日页消失（同一 url_key 的所有模块行一起消失）
_HIDDEN_EXCLUDE = (
    "COALESCE(url_key, url) NOT IN "
    "(SELECT COALESCE(url_key, url) FROM news WHERE hidden = 1)"
)


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
    """排序：权重高的来源优先排在前面（spec v2.3 第 3.1 节），其余部分保持原规则。

    没有点过赞/踩时全部权重为 0，等于不参与排序，所以默认仍是「发布时间倒序」
    （spec v2 第 4 节），只有真的投过票的来源才会被提前/压后。
    """
    weight_key = f"{_WEIGHT_KEY} DESC, " if _json1_available() else ""
    if sort == "hotspot":
        return (f"ORDER BY {weight_key}(hotspot_score IS NULL), hotspot_score DESC, "
                "(published_at IS NULL), published_at DESC")
    if sort == "shown":
        return "ORDER BY module_shown_at DESC, id DESC"
    if sort == "user_score":
        return f"ORDER BY {weight_key}(user_score IS NULL), user_score DESC, id DESC"
    # 默认：新闻发布时间倒序，时间未知排最后
    return f"ORDER BY {weight_key}(published_at IS NULL), published_at DESC, id DESC"


def today_items(module: str, page: int = 1, page_size: int = 10, sort: str = "published",
                day: str | None = None, **kw) -> tuple:
    """某模块“今日”条目：今天展示过的全部（一天内多次刷新会累积），分页返回。

    模块内 url_key 有唯一索引，所以这里天然不会有重复卡片；
    点过踩（hidden=1）的新闻整条隐藏，不出现在今日页（spec v2.3 第 2 节）。
    """
    day = day or datetime.now().strftime("%Y-%m-%d")
    where, params = ["module = ?", "substr(module_shown_at, 1, 10) = ?", _HIDDEN_EXCLUDE], [module, day]
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
    """该模块最近一次刷新的批次（综合模块按配额混合时从这里取）。隐藏的不参与混合。"""
    where, params = ["module = ?", _HIDDEN_EXCLUDE], [module]
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
                  sort: str = "published", dedup: bool = True,
                  hidden: str | None = None, **kw) -> tuple:
    """历史列表：可按日期/模块/可信度/我的评分/关键词筛选，只读。

    dedup=True 时按 url_key 跨模块去重：同一条新闻可能既在综合又在 AI 模块里，
    历史列表只显示一条（优先保留领域模块那一行，它信息更具体）。
    hidden：None=全部（含已隐藏）；'only'=只看已隐藏；'visible'=隐藏的不显示。
    被点踩的新闻在历史里照样查得到，并带上「已隐藏」标记（spec v2.3 第 2 节）。
    """
    where, params = [], []
    if date:
        where.append("(substr(published_at, 1, 10) = ? OR (published_at IS NULL AND substr(module_shown_at, 1, 10) = ?))")
        params.extend([date, date])
    if hidden == "only":
        where.append("hidden = 1")
    elif hidden == "visible":
        where.append("COALESCE(hidden, 0) = 0")
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
    out = [row_to_dict(r) for r in rows]
    # 去重后留下的那一行可能不是被隐藏的那一行，这里按 url_key 统一补上隐藏标记
    keys = hidden_url_keys()
    for item in out:
        key = item.get("url_key") or url_key(item.get("url") or "")
        own = bool(item.get("hidden"))
        if own or key in keys:
            item["hidden"] = 1
            item["hidden_basis"] = "" if own else "同一条新闻在其它模块被标记为已隐藏"
    return out, total


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
    """今日该模块的条数与热点分布，用于配额显示（被点踩隐藏的不计入）。"""
    day = day or datetime.now().strftime("%Y-%m-%d")
    with connect() as conn:
        rows = conn.execute(
            "SELECT domain, credibility, hotspot_score FROM news "
            f"WHERE module = ? AND substr(module_shown_at, 1, 10) = ? AND {_HIDDEN_EXCLUDE}",
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
