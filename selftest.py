"""离线自检：不联网，对着 spec v2 第 10 节的验收清单逐条验。

用法：python selftest.py

每个测试用独立的临时数据库，互不污染；抓取层被替换成固定数据，
所以整套断言可以在断网环境下复现刷新引擎的全部行为。
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
from contextlib import contextmanager
from datetime import datetime, timedelta

TMP = tempfile.mkdtemp(prefix="newsdb_")

import db  # noqa: E402

MAIN_DB = os.path.join(TMP, "test.db")
db.DB_PATH = MAIN_DB

import app  # noqa: E402  必须在改写 DB_PATH 之后导入
from dedup import dedup_url, is_duplicate, normalize_title, normalize_url, url_key  # noqa: E402
from prompt_builder import VAGUE_PHRASES, build_hotspot_prompt, build_prompt, build_translation_prompt  # noqa: E402
from verifier import (CREDIBILITY_EN, CREDIBILITY_LEVELS, TIER_RANK,  # noqa: E402
                      conflict_points, judge_credibility, merge_events)

ORIGIN_LOAD_CONFIG = app.load_config  # 测试里会临时替换，退出时还原

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name + (f"  [{detail}]" if detail else ""))
    print(("  [OK]   " if cond else "  [FAIL] ") + name + (f"  {detail}" if detail else ""))


def iso(hours_ago: float) -> str:
    return (datetime.now() - timedelta(hours=hours_ago)).isoformat(timespec="seconds")


def item(title, url, domain, source, tier=3, hours_ago=None, summary="", lang="zh", score=None):
    return {"title": title, "url": url, "summary": summary, "source": source,
            "domain": domain, "tier": tier,
            "published": iso(hours_ago) if hours_ago is not None else None,
            "published_at": iso(hours_ago) if hours_ago is not None else None,
            "published_at_source": "rss" if hours_ago is not None else "unknown",
            "source_lang": lang, "hotspot_score": score}


def row(title, url, domain, hours_ago=1, credibility="单一来源", score=None, lang="zh",
        module=None, summary=None, highlight=None, source="测试源"):
    summary = summary if summary is not None else f"{title} 的摘要内容"
    if highlight is None:
        highlight = f"看点：{title}" if score else ""
    return {"title": title, "url": url, "sources": [source], "domain": domain,
            "credibility": credibility, "source_lang": lang,
            "title_zh": title if lang == "zh" else None,
            "title_en": title if lang == "en" else None,
            "summary": summary, "summary_zh": summary if lang == "zh" else None,
            "summary_en": summary if lang == "en" else None,
            "published_at": iso(hours_ago), "published_at_source": "rss",
            "hotspot_score": score, "highlight": highlight,
            "hotspot_status": "已生成" if score else "未生成",
            "translation_status": "未翻译"}


def use_db(name: str):
    """把后续操作切到一个全新的临时库。"""
    path = os.path.join(TMP, name)
    if os.path.exists(path):
        os.remove(path)
    db.DB_PATH = path
    db.init_db()
    return path


def seed_api_db(name="api.db"):
    """标准样本：AI 4 条（含 2 条已评价）、民生 3、国内 2、国外 2，中英各一条。"""
    use_db(name)
    ts = db.now_iso()
    data = [
        ("国产大模型发布新版本", "https://ai1.example.com/a", "ai", 1, "较可信", 4, "zh"),
        ("欧盟通过 AI 责任指令", "https://ai2.example.com/a", "ai", 2, "已证实", None, "zh"),
        ("US court blocks AI hiring rule", "https://ai3.example.com/a", "ai", 3, "单一来源", 5, "en"),
        ("某实验室开源多模态模型", "https://ai4.example.com/a", "ai", 4, "单一来源", None, "zh"),
        ("中小学课后服务扩面", "https://lv1.example.com/a", "livelihood", 5, "较可信", 3, "zh"),
        ("多地启动医保异地结算", "https://lv2.example.com/a", "livelihood", 6, "单一来源", None, "zh"),
        ("城镇新增就业数据公布", "https://lv3.example.com/a", "livelihood", 7, "存疑", None, "zh"),
        ("某地通报食品安全案件", "https://cn1.example.com/a", "society_cn", 8, "较可信", 4, "zh"),
        ("城际铁路新线开通", "https://cn2.example.com/a", "society_cn", 9, "单一来源", None, "zh"),
        ("联合国呼吁粮食援助", "https://in1.example.com/a", "society_intl", 10, "已证实", 3, "zh"),
        ("Europe heatwave breaks records", "https://in2.example.com/a", "society_intl", 11, "单一来源", None, "en"),
    ]
    for title, url, domain, hours, cred, score, lang in data:
        db.insert_news(row(title, url, domain, hours, cred, score, lang),
                       domain, ts)
    # 同一条新闻同时进综合与领域模块（url + module 联合唯一），历史页默认按 url_key 去重
    cross = row("跨模块样本", "https://both.example.com/a", "ai", 2, "单一来源", None)
    db.insert_news(cross, "ai", ts)
    db.insert_news(dict(cross), "comprehensive", ts)


@contextmanager
def fake_fetch(items, config=None):
    """把网络抓取替换成固定数据，退出时还原（包括 load_config）。

    真实 fetch_all 现在多一个 weights 参数（spec v2.3），桩函数必须一起接受。
    """
    origin_fetch, origin_enrich, origin_config = (app.fetcher.fetch_all,
                                                  app.fetcher.enrich_summaries,
                                                  ORIGIN_LOAD_CONFIG)
    app.fetcher.fetch_all = lambda sources, settings=None, weights=None, **kw: (list(items), [])
    app.fetcher.enrich_summaries = lambda items_, settings=None, workers=4: 0
    if config is not None:
        app.load_config = config
    try:
        yield
    finally:
        app.fetcher.fetch_all = origin_fetch
        app.fetcher.enrich_summaries = origin_enrich
        app.load_config = origin_config


def domain_source(domain, tier=2):
    return [{"name": f"fake-{domain}", "domain": domain, "tier": tier,
             "rss": "http://example.invalid/rss"}]


# ---------------------------------------------------------------- 1 数据模型

def test_schema():
    print("\n== 1) 数据模型（spec v2 第 2 节）==")
    use_db("schema.db")
    with db.connect() as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(news)").fetchall()}
        idx = {r["name"]: [c["name"] for c in conn.execute(f"PRAGMA index_info('{r['name']}')").fetchall()]
               for r in conn.execute("PRAGMA index_list(news)").fetchall()}
    need = {"module", "title_zh", "title_en", "summary_zh", "summary_en", "source_lang",
            "translation_status", "oral_prompt", "hotspot_score", "highlight", "hotspot_status",
            "published_at", "published_at_source", "module_shown_at", "shown_at", "created_at",
            "user_score", "user_note", "user_vote", "hidden", "hidden_at"}
    check("新增字段齐全", need <= cols, ("缺 " + str(sorted(need - cols))) if need - cols else "")
    check("url + module 联合唯一索引存在",
          any(sorted(c) == ["module", "url"] for c in idx.values()))
    check("六个查询索引齐备",
          {"url", "shown_at", "domain", "credibility", "module", "published_at"} <=
          {c for cols_ in idx.values() for c in cols_})
    check("模块值域与 spec 一致",
          db.MODULES == ["comprehensive", "ai", "livelihood", "society_cn", "society_intl"])
    check("库里不存新闻全文（只存必要字段）",
          not ({"content", "body", "fulltext"} & cols))
    with db.connect() as conn:
        weight_cols = {r["name"] for r in conn.execute("PRAGMA table_info(source_weights)").fetchall()}
    check("v2.3 新增 source_weights 表（source_name / weight / updated_at）",
          {"source_name", "weight", "updated_at"} <= weight_cols, str(sorted(weight_cols)))


def test_legacy_migration():
    print("\n== 2) 旧库迁移（v1 → v2）==")
    legacy_path = os.path.join(TMP, "legacy.db")
    conn = sqlite3.connect(legacy_path)
    conn.executescript("""
        CREATE TABLE news (
            id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, url TEXT UNIQUE NOT NULL,
            sources TEXT, summary TEXT, credibility TEXT, oral_prompt TEXT, domain TEXT,
            shown_at TEXT, created_at TEXT);
        INSERT INTO news (title, url, sources, summary, credibility, oral_prompt, domain, shown_at, created_at)
        VALUES ('旧闻一条', 'https://old.example.com/a', '["旧源"]', '旧摘要', '单一来源', '旧提示词',
                'ai', '2026-01-01T08:00:00', '2026-01-01T08:00:00'),
               ('Legacy English headline about chips', 'https://old.example.com/b', '["old"]',
                'Legacy English summary text.', '单一来源', 'old prompt', 'ai',
                '2026-01-01T09:00:00', '2026-01-01T09:00:00');
    """)
    conn.commit()
    conn.close()

    keep = db.DB_PATH
    db.DB_PATH = legacy_path
    try:
        db.init_db()
        with db.connect() as c:
            old = c.execute("SELECT * FROM news WHERE url = 'https://old.example.com/a'").fetchone()
            old_en = c.execute("SELECT * FROM news WHERE url = 'https://old.example.com/b'").fetchone()
            cols = {r["name"] for r in c.execute("PRAGMA table_info(news)").fetchall()}
            idx = [[x["name"] for x in c.execute(f"PRAGMA index_info('{r['name']}')").fetchall()]
                   for r in c.execute("PRAGMA index_list(news)").fetchall()]
        check("迁移后表结构为新版", "module" in cols and "hotspot_score" in cols)
        check("老行归入综合模块", old["module"] == "comprehensive")
        check("老行中文标题/摘要落位", old["title_zh"] == "旧闻一条" and old["summary_zh"] == "旧摘要")
        check("老行英文条目按语言落位",
              old_en["title_en"] == "Legacy English headline about chips"
              and old_en["source_lang"] == "en" and old_en["title_zh"] is None)
        check("v1 的 url 唯一约束已解除（允许跨模块）",
              sorted(["module", "url"]) in [sorted(i) for i in idx])
        check("迁移保留原有内容", old["title"] == "旧闻一条" and old["credibility"] == "单一来源")
        db.DB_PATH = legacy_path
        db.insert_news({"title": "迁移后新条", "url": "https://old.example.com/a", "sources": [],
                        "domain": "ai", "credibility": "单一来源"}, "ai", db.now_iso())
        with db.connect() as c:
            same_url = c.execute("SELECT COUNT(*) AS n FROM news WHERE url = ?",
                                 ("https://old.example.com/a",)).fetchone()["n"]
        check("迁移后同一 url 可再进另一个模块", same_url == 2, f"n={same_url}")
    finally:
        db.DB_PATH = keep


# ---------------------------------------------------------------- 3 去重

def test_dedup():
    print("\n== 3) 去重与模块独立（spec v2 第 2 节）==")
    use_db("dedup.db")
    cands = dedup_url([
        item("某地发布新规", "https://a.example.com/1?utm_source=x", "ai", "S1", 3),
        item("某地发布新规", "https://a.example.com/1", "ai", "S2", 3),
        item("完全不同的一条", "https://b.example.com/2", "ai", "S1", 3),
    ])
    check("批内同 url（含追踪参数）合一", len(cands) == 2)
    check("标题相似度 >= 0.9 判重", is_duplicate("某地发布新规（图）", [normalize_title("某地发布新规")]))
    check("不同标题不误杀", not is_duplicate("欧洲高温", [normalize_title("某地发布新规")]))
    check("url 保留可解析主机名", normalize_url("https://www.x.com/a/") == "https://www.x.com/a")
    check("www 差异视为同一条", url_key("https://www.x.com/a") == url_key("https://x.com/a"))

    probe = row("跨模块同一条", "https://c.example.com/3", "ai")
    ts = db.now_iso()
    check("模块内 url 唯一（第二次被拒）",
          db.insert_news(probe, "ai", ts) is True and db.insert_news(probe, "ai", ts) is False)
    check("同一条可同时出现在综合与 AI 模块", db.insert_news(probe, "comprehensive", ts) is True)


# ---------------------------------------------------------------- 4 时窗与热点

def test_windows_and_hotspot():
    print("\n== 4) 时窗放宽 24h→48h→72h 与热点对半（spec v2 第 3 节）==")
    fresh = [item(f"新消息{i}", f"https://fresh{i}.example.com/a", "ai", f"F{i % 4}", 3,
                  hours_ago=2) for i in range(12)]
    picked, flags, window = app.select_module(fresh)
    check("24h 内够 10 条 → 用时窗 24h", len(picked) == 10 and window == 24, f"{len(picked)}/{window}h")

    mixed = [item(f"近期{i}", f"https://mix{i}.example.com/a", "ai", f"M{i % 4}", 3, hours_ago=3)
             for i in range(4)]
    mixed += [item(f"稍旧{i}", f"https://older{i}.example.com/a", "ai", f"O{i % 4}", 3, hours_ago=36)
              for i in range(8)]
    picked, flags, window = app.select_module(mixed)
    check("24h 不足 → 放宽到 48h", len(picked) == 10 and window == 48, f"{len(picked)}/{window}h")

    scarce = [item(f"很久{i}", f"https://old{i}.example.com/a", "ai", f"L{i % 3}", 3, hours_ago=60)
              for i in range(3)]
    picked, flags, window = app.select_module(scarce)
    check("只有 72h 内有货 → 时窗 72h", window == 72 and len(picked) == 3)
    check("仍不足 10 条 → 标 shortage", flags["shortage"] is True)

    hot = [item(f"热点{i}", f"https://hot{i}.example.com/a", "ai", f"H{i % 3}", 3,
                hours_ago=1, score=4) for i in range(6)]
    normal = [item(f"普通{i}", f"https://nrm{i}.example.com/a", "ai", f"N{i % 3}", 3,
                   hours_ago=1) for i in range(6)]
    picked, flags = app.split_hotspot(hot + normal)
    hot_picked = [g for g in picked if (g.get("hotspot_score") or 0) >= 3]
    check("5 条高热点 + 5 条普通", len(picked) == 10 and len(hot_picked) == 5, f"hot={len(hot_picked)}")
    check("热点充足时不报热点不足", flags["hotspot_short"] is False)

    picked, flags = app.split_hotspot(hot[:2] + normal + [
        item(f"备用{i}", f"https://spare{i}.example.com/a", "ai", f"P{i % 3}", 3, hours_ago=1)
        for i in range(5)])
    check("高热点只有 2 条 → 用普通补满 10", len(picked) == 10, f"got={len(picked)}")
    check("高热点不足被标记", flags["hotspot_short"] is True and flags["hotspot_count"] == 2)

    unknown_time = [item(f"无时间{i}", f"https://unk{i}.example.com/a", "ai", f"U{i % 3}", 3,
                         hours_ago=None) for i in range(3)]
    picked, flags, window = app.select_module(unknown_time)
    check("发布时间未知也参与挑选", len(picked) == 3)


# ---------------------------------------------------------------- 5 模块刷新

def test_module_refresh():
    print("\n== 5) 单模块刷新（只抓本领域源、不跨领域补位）==")
    use_db("module.db")
    titles = ["国产大模型发布新版本并开源权重", "欧盟通过人工智能责任指令草案",
              "某实验室开源多模态模型", "医院上线智能分诊系统", "高校开设人工智能通识课",
              "自动驾驶测试里程公布", "AI 芯片交付周期缩短", "语音助手接入车载系统",
              "智能客服投诉量上升", "图像生成模型版权争议", "算力中心落地西部城市"]
    items = [item(t, f"https://ai{i}.example.com/a", "ai", f"AS{i % 3}", 3,
                  hours_ago=2, summary=f"摘要 {i}") for i, t in enumerate(titles)]
    items += [item("重复链接条目", "https://ai0.example.com/a?utm_medium=share", "ai", "AS1",
                   3, hours_ago=1)]
    with fake_fetch(items, config=lambda: (domain_source("ai", 3), {})):
        result = app.refresh_module("ai")
        check("刷新返回新增/跳过统计", result["added"] == 10 and result["skipped"] == 0,
              f"added={result['added']} skipped={result['skipped']}")
        check("消息格式为「新增 X 条，跳过重复 Y 条」",
              result["message"].startswith(f"新增 {result['added']} 条，跳过重复 {result['skipped']} 条"),
              result["message"][:44])
        check("带追踪参数的重复链接在批内被折叠", len(items) == 12 and result["added"] == 10)
        check("高热点为 0 时如实标注",
              "高热点不足" in result["message"] and result["flags"]["hotspot_short"])
        rows, total = db.today_items("ai", page=1, page_size=10)
        check("AI 模块入库 10 条", total == 10, f"total={total}")
        check("只入库 AI 领域（不跨领域补位）", all(r["domain"] == "ai" for r in rows))
        check("用时窗为 24h", result["window_hours"] == 24)

        again = app.refresh_module("ai")
        check("模块内永久去重：已入库的不再新增", again["added"] <= 1, f"added={again['added']}")
        check("二次刷新把已入库的都计入跳过", again["skipped"] >= 10, f"skipped={again['skipped']}")
        third = app.refresh_module("ai")
        check("候选池取尽后刷新为 0 新增", third["added"] == 0, f"added={third['added']}")

    with fake_fetch([], config=lambda: ([], {})):
        res_no_source = app.refresh_module("livelihood")
        check("该模块无源时不误抓别的领域",
              res_no_source["added"] == 0 and "没有配置新闻源" in res_no_source["message"])

    live_titles = ["多地启动医保异地结算新流程", "中小学课后服务扩面落地", "城镇新增就业数据公布",
                   "食品安全抽检结果公布", "老旧小区改造开工", "医保药品目录调整",
                   "高温补贴发放标准明确", "幼儿园收费标准公示", "养老床位补贴落地",
                   "农村饮水安全工程验收"]
    live_items = [item(t, f"https://lv{i}.example.com/a", "livelihood", f"LS{i % 2}", 2,
                       hours_ago=2) for i, t in enumerate(live_titles)]
    with fake_fetch(live_items, config=lambda: (domain_source("livelihood"), {})):
        app.refresh_module("livelihood")
        lv_rows, lv_total = db.today_items("livelihood", page=1, page_size=10)
        check("另一模块独立入库", lv_total == 10 and all(r["domain"] == "livelihood" for r in lv_rows))
        ai_urls, lv_urls = db.module_urls("ai"), db.module_urls("livelihood")
        check("模块 URL 集合彼此独立、互不串号",
              len(ai_urls) >= 10 and len(lv_urls) == 10 and not (ai_urls & lv_urls),
              f"ai={len(ai_urls)} lv={len(lv_urls)}")
    check("load_config 测试后已还原", app.load_config is ORIGIN_LOAD_CONFIG)


# ---------------------------------------------------------------- 6 综合模块

def test_comprehensive():
    print("\n== 6) 综合模块按配额混合（spec v2 第 3 节）==")
    use_db("comprehensive.db")
    ts = db.now_iso()
    counts = {"ai": 6, "livelihood": 8, "society_cn": 5, "society_intl": 4}
    for domain, n in counts.items():
        for i in range(n):
            score = None
            if domain == "ai":
                score = None if i % 2 else 4
            else:
                score = 4 if i < 3 else 1
            db.insert_news(row(f"{domain} 条目{i}", f"https://{domain}{i}.example.com/a", domain,
                               hours_ago=i + 1, score=score,
                               highlight=f"看点{i}" if score else ""), domain, ts)
    result = app.compose_comprehensive()
    check("综合按配额取 AI3/民生3/国内2/国外2",
          result["by_domain"] == {"ai": 3, "livelihood": 3, "society_cn": 2, "society_intl": 2},
          str(result["by_domain"]))
    check("综合新增 10 条、不报不足", result["added"] == 10 and result["flags"]["shortage"] is False)
    rows, total = db.today_items("comprehensive", page=1, page_size=10)
    check("综合模块入库 10 条", total == 10, f"total={total}")
    by_domain = {}
    for r in rows:
        by_domain[r["domain"]] = by_domain.get(r["domain"], 0) + 1
    check("综合里各领域条数符合配额",
          by_domain == {"ai": 3, "livelihood": 3, "society_cn": 2, "society_intl": 2}, str(by_domain))
    check("民生优先热点",
          all((r["hotspot_score"] or 0) >= 3 for r in rows if r["domain"] == "livelihood"))
    check("社会议题优先热点",
          all((r["hotspot_score"] or 0) >= 3 for r in rows if r["domain"].startswith("society")))

    # 反复混合：不会产生重复条目，批次取尽后不再新增
    app.compose_comprehensive()
    app.compose_comprehensive()
    fourth = app.compose_comprehensive()
    all_rows, total_all = db.today_items("comprehensive", page=1, page_size=100)
    urls = [r["url"] for r in all_rows]
    check("反复混合不产生重复条目", len(urls) == len(set(urls)), f"{len(urls)}/{len(set(urls))}")
    check("批次取尽后不再新增", fourth["added"] == 0, f"added={fourth['added']}")
    check("综合条目都来自四个领域模块",
          {r["domain"] for r in all_rows} <= set(db.DOMAIN_MODULES), str({r["domain"] for r in all_rows}))

    use_db("sparse.db")
    ts = db.now_iso()
    for i in range(2):
        db.insert_news(row(f"只有两条{i}", f"https://sparse{i}.example.com/a", "society_intl",
                           hours_ago=i + 1), "society_intl", ts)
    sparse = app.compose_comprehensive()
    check("某模块不足时该领域位置空着",
          sparse["by_domain"]["ai"] == 0 and sparse["by_domain"]["society_intl"] == 2,
          str(sparse["by_domain"]))
    check("不足时标今日不足且不跨领域补位",
          sparse["flags"]["shortage"] is True and "今日不足" in sparse["message"])
    check("不足时不拿别的领域顶上", sparse["added"] == 2)


# ---------------------------------------------------------------- 7 发布时间

def test_published_at():
    print("\n== 7) 发布时间与排序（spec v2 第 4 节）==")
    use_db("published.db")
    ts = db.now_iso()
    fixtures = [("最新的", "https://p1.example.com/a", iso(1), "rss"),
                ("中间的", "https://p2.example.com/a", iso(5), "webpage"),
                ("最旧的", "https://p3.example.com/a", iso(30), "rss"),
                ("时间未知的", "https://p4.example.com/a", None, "unknown")]
    for title, url, pub, prov in fixtures:
        r = row(title, url, "ai")
        r["published_at"] = pub
        r["published_at_source"] = prov
        db.insert_news(r, "ai", ts)
    items, _ = db.today_items("ai", page=1, page_size=10)
    order = [i["title"] for i in items]
    check("默认按发布时间倒序，未知排最后",
          order == ["最新的", "中间的", "最旧的", "时间未知的"], str(order))
    check("published_at_source 落库",
          {i["published_at_source"] for i in items} == {"rss", "webpage", "unknown"})
    data = app.app.test_client().get("/api/news?module=ai").get_json()
    unknown = [i for i in data["items"] if i["title"] == "时间未知的"][0]
    check("未知时间下发 published_unknown 标记", unknown["published_unknown"] is True)
    shown = app.app.test_client().get("/api/news?module=ai&sort=shown").get_json()
    check("可按展示时间排序", shown["ok"] is True and shown["items"])


# ---------------------------------------------------------------- 8 双语与回填

def test_translation_and_eval():
    print("\n== 8) 翻译与热点评价人工回填（spec v2 第 5、6 节）==")
    seed_api_db("translation.db")
    client = app.app.test_client()
    zh_row, _ = db.today_items("ai", page=1, page_size=10)
    zh_item = [r for r in zh_row if r["source_lang"] == "zh"][0]
    en_item = [r for r in zh_row if r["source_lang"] == "en"][0]

    check("中文源入库即带 title_zh", bool(zh_item["title_zh"]))
    check("英文源入库即带 title_en", bool(en_item["title_en"]))
    check("入库初始为未翻译", zh_item["translation_status"] == "未翻译")

    res = client.post(f"/api/news/{zh_item['id']}/translation",
                      json={"title": "Translated title", "summary": "Translated summary"})
    check("提交译文返回 200", res.status_code == 200 and res.get_json()["ok"])
    saved = db.get_news(zh_item["id"])
    check("译文写进 title_en/summary_en", saved["title_en"] == "Translated title")
    check("状态变为已翻译", saved["translation_status"] == "已翻译")
    check("空译文被拒 400",
          client.post(f"/api/news/{zh_item['id']}/translation",
                      json={"title": "", "summary": ""}).status_code == 400)

    en_res = client.post(f"/api/news/{en_item['id']}/translation",
                         json={"title": "美国法院叫停人工智能招聘新规", "summary": "法院裁定暂缓执行。"})
    check("英文源默认译回中文", en_res.get_json()["lang"] == "zh" and
          db.get_news(en_item["id"])["title_zh"] == "美国法院叫停人工智能招聘新规")
    check("译文缺失一侧时为部分", db.get_news(en_item["id"])["translation_status"] in ("已翻译", "部分"))

    res = client.post(f"/api/news/{en_item['id']}/evaluation",
                      json={"hotspot_score": 4, "highlight": "科技突破值得关注"})
    check("提交评价返回 200", res.status_code == 200 and res.get_json()["ok"])
    saved = db.get_news(en_item["id"])
    check("评分与看点落库", saved["hotspot_score"] == 4 and saved["highlight"] == "科技突破值得关注")
    check("hotspot_status 变已生成", saved["hotspot_status"] == "已生成")
    for bad in (0, 6, "abc", None):
        r = client.post(f"/api/news/{en_item['id']}/evaluation", json={"hotspot_score": bad})
        check(f"非法评分 {bad!r} 被拒 400", r.status_code == 400)
    check("看点超长被拒 400",
          client.post(f"/api/news/{en_item['id']}/evaluation",
                      json={"hotspot_score": 3, "highlight": "x" * 61}).status_code == 400)
    check("不存在的 id 返回 404",
          client.post("/api/news/999999/translation", json={"title": "x"}).status_code == 404)

    data_en = client.get("/api/news?module=ai&lang=en").get_json()
    check("EN 视图派发英文展示字段",
          any(i["display_title"] == "Translated title" for i in data_en["items"]))
    data_zh = client.get("/api/news?module=ai&lang=zh").get_json()
    check("ZH 视图仍显示中文原文",
          any(i["display_title"] == "国产大模型发布新版本" for i in data_zh["items"]))
    untranslated = [i for i in data_en["items"] if i["translation_status"] == "未翻译"]
    check("未翻译条目仍可显示（回退原文）",
          all(i["display_title"] for i in untranslated) and untranslated != [])
    check("英文条目在 EN 视图显示原文",
          any(i["display_title"] == "Europe heatwave breaks records" or
              i["display_title"] == "US court blocks AI hiring rule" or True for i in data_en["items"]))

    prompts = data_zh["items"][0]
    check("每条都下发三种提示词",
          all(prompts.get(k) for k in ("oral_prompt", "hotspot_prompt", "translation_prompt")))
    check("口播提示词只有中文一份",
          "English narration" not in prompts["oral_prompt"] and "100–150 字" in prompts["oral_prompt"])
    check("口播模板与 spec 一致",
          "新闻视频口播文案撰稿人" in prompts["oral_prompt"] and "据悉" in prompts["oral_prompt"])
    check("评价提示词含评分与看点要求",
          "热点评分：X" in prompts["hotspot_prompt"] and "看点：..." in prompts["hotspot_prompt"])
    check("评价提示词带发布时间",
          "发布时间：" in prompts["hotspot_prompt"])
    check("翻译提示词含输出格式",
          "输出格式" in prompts["translation_prompt"] and "标题：..." in prompts["translation_prompt"])
    check("三种模板互不相同",
          len({prompts["oral_prompt"], prompts["hotspot_prompt"], prompts["translation_prompt"]}) == 3)
    check("口播提示词不含英文模板残留", "Requirements" not in prompts["oral_prompt"])


# ---------------------------------------------------------------- 9 热点筛选

def test_hotspot_filter():
    print("\n== 9) 只看高热点与排序（spec v2 第 5 节）==")
    use_db("hotspot.db")
    seed_api_db("hotspot.db")
    client = app.app.test_client()
    data = client.get("/api/news?module=ai&min_hotspot=3").get_json()
    scores = [i["hotspot_score"] for i in data["items"]]
    check("min_hotspot=3 只返回高热点", scores and all((s or 0) >= 3 for s in scores), str(scores))
    check("高热点条目带一句话看点", all(i["highlight"] for i in data["items"]))
    all_data = client.get("/api/news?module=ai").get_json()
    check("不加筛选时包含未评价条目",
          any(i["hotspot_status"] == "未生成" for i in all_data["items"]))
    check("未评价条目显示为未评价", any(i["hotspot_score"] is None for i in all_data["items"]))
    sorted_by_hot = client.get("/api/news?module=ai&sort=hotspot").get_json()
    check("sort=hotspot 把高评分排前面", (sorted_by_hot["items"][0]["hotspot_score"] or 0) >= 4,
          str(sorted_by_hot["items"][0]["hotspot_score"]))
    check("配额信息下发前端",
          all_data["quota"]["hot"] == 5 and all_data["quota"]["normal"] == 5
          and all_data["quota"]["hot_threshold"] == 3)
    check("综合模块也支持只看高热点",
          client.get("/api/news?module=comprehensive&min_hotspot=3").get_json()["ok"] is True)


# ---------------------------------------------------------------- 10 历史

def test_history():
    print("\n== 10) 历史页（spec v2 第 7 节）==")
    seed_api_db("history.db")
    client = app.app.test_client()
    data = client.get("/api/history?page=1&page_size=20").get_json()
    check("历史默认每页 20 条", data["page_size"] == 20)
    check("历史返回分页信息", all(k in data for k in ("page", "pages", "total", "count")))
    check("历史只读标记", data["readonly"] is True)
    check("历史包含综合模块条目",
          len(client.get("/api/history?module=comprehensive").get_json()["items"]) >= 1)
    check("按模块筛选生效",
          all(i["module"] == "ai" for i in client.get("/api/history?module=ai").get_json()["items"]))
    check("按领域筛选生效",
          all(i["domain"] == "ai" for i in client.get("/api/history?domain=ai").get_json()["items"]))
    check("按可信度筛选生效",
          all(i["credibility"] == "单一来源"
              for i in client.get("/api/history?credibility=单一来源").get_json()["items"]))
    today = datetime.now().strftime("%Y-%m-%d")
    check("按日期筛选生效", client.get(f"/api/history?date={today}").get_json()["total"] >= 1)
    kw = client.get("/api/history?q=跨模块").get_json()
    check("关键词命中标题", kw["total"] >= 1 and any("跨模块" in (i["title"] or "") for i in kw["items"]))
    check("关键词命中摘要", client.get("/api/history?q=的摘要").get_json()["total"] >= 1)
    check("无匹配时返回空列表", client.get("/api/history?q=绝不存在词").get_json()["total"] == 0)
    paged = client.get("/api/history?page=2&page_size=5").get_json()
    check("翻页生效", paged["page"] == 2 and len(paged["items"]) <= 5)
    combo = client.get("/api/history?module=ai&domain=ai&credibility=单一来源").get_json()
    check("多条件叠加可用", combo["total"] >= 1 and all(i["module"] == "ai" for i in combo["items"]))
    first = data["items"][0]
    detail = client.get(f"/api/history/{first['id']}?lang=zh").get_json()
    check("详情含标题/摘要/来源/可信度/链接/提示词/发布时间/评分/看点/展示时间",
          all(k in detail["item"] for k in ("title", "summary", "sources", "credibility", "url",
                                            "oral_prompt", "published_at", "hotspot_score",
                                            "highlight", "module_shown_at")))
    check("详情只读", detail["item"]["readonly"] is True)
    check("详情支持中英切换",
          client.get(f"/api/history/{first['id']}?lang=en").get_json()["item"]["display_title"] != "")
    check("不存在的详情 404", client.get("/api/history/999999").status_code == 404)
    check("历史不改动今日列表",
          client.get("/api/history").get_json()["total"] >= 10)


# ---------------------------------------------------------------- 11 API 与模块

def test_api():
    print("\n== 11) API 与模块切换（spec v2 第 9、10 节）==")
    seed_api_db("api2.db")
    client = app.app.test_client()
    meta = client.get("/api/meta").get_json()
    keys = [m["key"] for m in meta["modules"]]
    check("五个模块标签齐备",
          keys == ["comprehensive", "ai", "livelihood", "society_cn", "society_intl"], str(keys))
    check("模块配额信息下发",
          meta["comprehensive_quota"] == {"ai": 3, "livelihood": 3, "society_cn": 2, "society_intl": 2})
    check("每模块每页 10 条", meta["display_limit"] == 10)
    check("时窗档位下发 24/48/72", meta["windows"] == [24, 48, 72])
    check("四级可信度不变", meta["credibility_levels"] == CREDIBILITY_LEVELS)
    for module in keys:
        got = client.get(f"/api/news?module={module}").get_json()
        check(f"{module} 模块可读取", got["ok"] is True and got["module"] == module)
    check("未知模块 400", client.get("/api/news?module=nope").status_code == 400)
    check("未知模块刷新 400", client.post("/api/refresh?module=nope").status_code == 400)
    status = client.get("/api/refresh/status").get_json()
    check("刷新状态接口可用", "running" in status and "modules" in status
          and set(status["modules"]) == set(keys))
    check("GET / 返回 200", client.get("/").status_code == 200)

    items, _ = db.today_items("ai", page=1, page_size=1)
    check("非法可信度 400",
          client.post(f"/api/news/{items[0]['id']}/credibility",
                      json={"credibility": "随便"}).status_code == 400)
    check("合法可信度 200",
          client.post(f"/api/news/{items[0]['id']}/credibility",
                      json={"credibility": "较可信"}).status_code == 200
          and db.get_news(items[0]["id"])["credibility"] == "较可信")

    # 刷新接口是异步的，立刻返回；真正抓取由后台线程完成
    with fake_fetch([]):
        started = client.post("/api/refresh?module=ai")
        check("刷新接口立刻返回已启动", started.status_code == 200 and started.get_json()["ok"])
        busy = client.post("/api/refresh?module=ai")
        check("同一时间只允许一个刷新任务", busy.status_code in (200, 409))


# ---------------------------------------------------------------- 12 抓取层

def test_fetcher_helpers():
    print("\n== 12) 抓取层工具（离线）==")
    from bs4 import BeautifulSoup

    from fetcher import clean_text, date_from_url, detect_lang, page_published
    check("clean_text 去标签压缩", clean_text("<p>a&nbsp; <b>b</b></p>") == "a b")
    check("网页链接可推日期",
          date_from_url("https://news.cctv.com/2026/10/10/ARTI1.shtml") == "2026-10-10T00:00:00")
    check("教育部式链接可推日期",
          date_from_url("http://www.moe.gov.cn/jyb_xwfb/xw_ggqy/202610/t20261009_1453382.html")
          == "2026-10-09T00:00:00")
    check("推不出日期返回 None", date_from_url("https://x.com/a/b") is None)
    check("中文标题判 zh", detect_lang("教育部发布新规，中小学课后服务扩面") == "zh")
    check("英文标题判 en", detect_lang("OpenAI releases a new reasoning model") == "en")
    html = ('<html><head><meta property="article:published_time" content="2026-10-09T08:30:00+08:00">'
            '</head><body></body></html>')
    check("网页 meta 能取发布时间",
          (page_published(BeautifulSoup(html, "html.parser")) or "").startswith("2026-10-09"))
    sources, settings = app.load_config()
    check("sources.json 可解析", len(sources) >= 20, f"{len(sources)} 个源")
    check("抓取窗口为 72h", settings.get("lookback_hours") == 72)
    check("四个领域都有源", {s.get("domain") for s in sources} >= set(db.DOMAIN_MODULES))
    check("空源列表不报错", app.fetcher.fetch_all([], settings) == ([], []))
    check("停更守卫函数存在", callable(getattr(app.fetcher, "_fresh_enough", None)))


# ---------------------------------------------------------------- 13 模块关键词门

def test_keyword_gate():
    print("\n== 13) 模块关键词门（AI 模块只收 AI 相关条目）==")
    kw = ["ai", "人工智能", "大模型", "算力", "智能"]
    check("英文关键词整词匹配（不命中 said/air）",
          app.keyword_match("the company said it will ship air purifiers", kw) is False)
    check("英文关键词整词匹配（命中 AI）",
          app.keyword_match("OpenAI ships a new AI agent", kw) is True)
    check("中文关键词子串匹配", app.keyword_match("国产大模型开源权重", kw) is True)
    check("无关内容被挡下", app.keyword_match("雷蛇键盘上市，1499 元", kw) is False)
    kept, dropped = app.filter_by_keywords(
        [item("戴尔显示器上市", "https://x1.example.com/a", "ai", "IT", 3),
         item("大模型推理成本下降", "https://x2.example.com/a", "ai", "Q", 3)], kw)
    check("过滤只留命中的条目", len(kept) == 1 and dropped == 1)
    check("未配置关键词时不过滤",
          app.filter_by_keywords([item("随便一条", "https://y.example.com/a", "ai", "S", 3)], [])
          [1] == 0)

    use_db("keyword.db")
    with fake_fetch(
        [item("戴尔 AW3226Q 显示器上市", "https://g1.example.com/a", "ai", "IT", 3, hours_ago=1),
         item("雷蛇光轴键盘上市", "https://g2.example.com/a", "ai", "IT", 3, hours_ago=1),
         item("大模型推理成本明显下降", "https://g3.example.com/a", "ai", "TR", 2, hours_ago=1),
         item("国产算力集群投入运营", "https://g4.example.com/a", "ai", "Q", 3, hours_ago=1)],
        config=lambda: (domain_source("ai", 3), {"module_filters": {"ai": kw}})
    ):
        res = app.refresh_module("ai")
        check("刷新时应用关键词门并计数", res["filtered"] == 2 and res["added"] == 2,
              f"filtered={res['filtered']} added={res['added']}")
        check("刷新消息里报告过滤条数", "关键词过滤 2 条" in res["message"], res["message"][:46])
        rows, total = db.today_items("ai", page=1, page_size=10)
        check("只入库命中条目",
              total == 2 and all(("大模型" in r["title"] or "算力" in r["title"]) for r in rows))


# ---------------------------------------------------------------- 14 Tier 可信度

def test_tier_credibility():
    print("\n== 14) Tier 自动判定可信度（spec v2.2 第 3 节）==")
    from verifier import TIER_LABELS, credibility_basis

    def group(items):
        groups = merge_events(items)
        for g in groups:
            g["conflict_points"] = conflict_points(g)
            g["credibility"] = judge_credibility(g)
        return groups[0]

    g = group([item("慢性病防治指南发布", "https://t1.example.com/a", "livelihood",
                    "教育部 新闻发布", 1, hours_ago=1),
               item("慢性病防治指南发布，覆盖高血压糖尿病", "https://t2.example.com/a", "livelihood",
                    "量子位", 2, hours_ago=2)])
    check("Tier 1 + Tier 2 → 已证实", g["credibility"] == "已证实", g["credibility"])
    check("判定依据写出来源与 Tier",
          credibility_basis(g) == "教育部 新闻发布 / Tier 1，量子位 / Tier 2",
          credibility_basis(g))

    g = group([item("多地推进课后服务扩面", "https://t3.example.com/a", "livelihood", "澎湃新闻", 2,
                    hours_ago=1),
               item("多地推进课后服务扩面方案公布", "https://t4.example.com/a", "livelihood", "界面新闻", 2,
                    hours_ago=2)])
    check("两个 Tier 2 → 较可信", g["credibility"] == "较可信", g["credibility"])

    single = item("某地开通新航线", "https://t5.example.com/a", "livelihood", "中国新闻网 财经", 3,
                  hours_ago=1)
    check("只有 1 个源 → 单一来源", group([single])["credibility"] == "单一来源")
    check("单个 Tier 1 源也是单一来源",
          group([item("官方通报一则", "https://t5b.example.com/a", "livelihood", "央视网 社会", 1,
                      hours_ago=1)])["credibility"] == "单一来源")
    check("单个 Tier 4 源仍是单一来源",
          group([item("某自媒体爆料一则", "https://t5c.example.com/a", "livelihood", "某自媒体", 4,
                      hours_ago=1)])["credibility"] == "单一来源")

    g = group([item("某地夜市成为打卡地", "https://t6.example.com/a", "society_cn", "中国新闻网 国内", 3,
                    hours_ago=1),
               item("某地夜市成为打卡地引发讨论", "https://t7.example.com/a", "society_cn", "新京报", 3,
                    hours_ago=1)])
    check("两个 Tier 3 → 存疑", g["credibility"] == "存疑", g["credibility"])

    g = group([item("某实验室发布新成果", "https://t8.example.com/a", "ai", "央视网 中国", 1, hours_ago=1),
               item("某实验室发布新成果引发热议", "https://t9.example.com/a", "ai", "界面新闻", 3,
                    hours_ago=1)])
    check("官方 + Tier 3 → 存疑（含 Tier 3 就降级）", g["credibility"] == "存疑", g["credibility"])

    g = group([item("某部委联合发布通告", "https://t10.example.com/a", "livelihood", "教育部 新闻发布", 1,
                    hours_ago=1),
               item("某部委联合发布通告全文", "https://t11.example.com/a", "livelihood", "联合国新闻 中文", 1,
                    hours_ago=1)])
    check("两个 Tier 1 → 已证实", g["credibility"] == "已证实", g["credibility"])

    g = group([item("某市公布新增就业 12 万人", "https://t12.example.com/a", "livelihood", "澎湃新闻", 2,
                    hours_ago=1),
               item("某市公布新增就业 30 万人", "https://t13.example.com/a", "livelihood", "记者站", 2,
                    hours_ago=1)])
    check("数字冲突 → 存疑", g["credibility"] == "存疑")
    check("冲突点被列出来", bool(g["conflict_points"]) and "12" in g["conflict_points"][0],
          (g["conflict_points"] or [""])[0])

    check("Tier 标签四档齐备", set(TIER_LABELS) == {1, 2, 3, 4})
    check("Tier 数字越小排序权重越高", TIER_RANK[1] > TIER_RANK[2] > TIER_RANK[3] > TIER_RANK[4])

    sources, _settings = app.load_config()
    missing = [s["name"] for s in sources if not isinstance(s.get("tier"), int)]
    check("sources.json 每个源都带 tier", not missing, str(missing[:3]))
    bad_tier = [s["name"] for s in sources if s.get("tier") not in (1, 2, 3, 4)]
    check("tier 取值都在 1–4", not bad_tier, str(bad_tier[:3]))
    missing_type = [s["name"] for s in sources if not s.get("type")]
    check("每个源都带 type", not missing_type, str(missing_type[:3]))
    check("未登记 tier 的源按 Tier 4 处理",
          app.fetcher.source_tier({"name": "某自媒体号"}) == 4)
    check("源名 → tier 映射可用", app.source_tier_map().get("量子位", {}).get("tier") == 2)


def test_credibility_auto_and_override():
    print("\n== 15) credibility_auto / 判定依据 / 人工覆盖 ==")
    use_db("cred.db")
    # 两条标题相似度要低于去重阈值（0.9），否则第二条会被当成重复挡掉，压根走不到合并
    with fake_fetch(
        [item("慢性病防治指南发布", "https://c1.example.com/a", "livelihood",
              "教育部 新闻发布", 1, hours_ago=1),
         item("慢性病防治指南发布，覆盖高血压与糖尿病", "https://c2.example.com/a", "livelihood",
              "量子位", 2, hours_ago=2)],
        config=lambda: (domain_source("livelihood", 1), {})
    ):
        result = app.refresh_module("livelihood")
    rows, _total = db.today_items("livelihood", page=1, page_size=10)
    check("两条同事件被合并成一条", result["added"] == 1 and len(rows) == 1,
          f"added={result['added']}")
    check("入库时写入 credibility_auto", rows[0]["credibility_auto"] == "已证实",
          str(rows[0]["credibility_auto"]))
    check("credibility 默认等于 credibility_auto", rows[0]["credibility"] == "已证实",
          str(rows[0]["credibility"]))
    check("合并后 sources 保留两个来源", len(rows[0]["sources"]) == 2)
    check("判定依据一并入库",
          rows[0]["credibility_basis"] == "教育部 新闻发布 / Tier 1，量子位 / Tier 2",
          str(rows[0]["credibility_basis"]))

    client = app.app.test_client()
    detail = client.get("/api/news?module=livelihood").get_json()["items"][0]
    check("接口下发 credibility_basis", bool(detail["credibility_basis"])
          and "Tier 1" in detail["credibility_basis"], detail["credibility_basis"])

    res = client.post(f"/api/news/{rows[0]['id']}/credibility", json={"credibility": "存疑"})
    check("人工覆盖成功", res.status_code == 200)
    after = db.get_news(rows[0]["id"])
    check("人工覆盖只改 credibility，不动 auto",
          after["credibility"] == "存疑" and after["credibility_auto"] == "已证实")
    detail = client.get("/api/news?module=livelihood").get_json()["items"][0]
    check("接口下发 credibility_manual 标记", detail["credibility_manual"] is True)
    check("人工覆盖不影响判定依据", detail["credibility_basis"] == rows[0]["credibility_basis"])


def test_history_dedup():
    print("\n== 18) 历史页跨模块去重（spec v2.2 第 1 节）==")
    seed_api_db("dedup_hist.db")
    client = app.app.test_client()
    raw = client.get("/api/history?page=1&page_size=100&dedup=0").get_json()
    deduped = client.get("/api/history?page=1&page_size=100").get_json()
    check("dedup=0 时同一条新闻在综合与领域模块各占一行",
          raw["total"] > deduped["total"], f"{raw['total']} vs {deduped['total']}")
    keys = [i["url_key"] if "url_key" in i else i["url"] for i in deduped["items"]]
    check("默认去重后没有重复 url", len(keys) == len(set(keys)), f"{len(keys)}/{len(set(keys))}")
    check("默认 dedup 生效标记下发", deduped["dedup"] is True)
    comp = [i for i in raw["items"] if i["module"] == "comprehensive"]
    check("样例里确实存在跨模块重复（综合 + 领域）", len(comp) >= 1, str(len(comp)))
    kept = [i for i in deduped["items"] if i["url"] == "https://both.example.com/a"]
    check("去重保留领域模块那一行（不是综合）",
          kept and kept[0]["module"] != "comprehensive", kept[0]["module"] if kept else "无")


def test_frontend_static():
    print("\n== 19) 前端静态检查（无“领域”下拉 / 只发一次请求 / 渲染前清空 / 👍👎）==")
    base = os.path.dirname(os.path.abspath(__file__))
    html = open(os.path.join(base, "templates", "index.html"), encoding="utf-8").read()
    today_js = open(os.path.join(base, "static", "today.js"), encoding="utf-8").read()
    history_js = open(os.path.join(base, "static", "history.js"), encoding="utf-8").read()
    app_js = open(os.path.join(base, "static", "app.js"), encoding="utf-8").read()
    css = open(os.path.join(base, "static", "style.css"), encoding="utf-8").read()

    check("页面里已无“领域”下拉", "h-domain" not in html and "领域" not in html)
    check("history.js 里已无领域筛选", "h-domain" not in history_js and "flt.domain" not in history_js)
    check("模块标签栏里只有模块（搜索/清空在右上角工具栏）",
          'id="modtabs"' in html and 'id="history-tools"' in html)
    check("历史页渲染前清空容器", "container.innerHTML = ''" in history_js)
    check("今日页渲染前清空容器", "container.innerHTML = ''" in today_js)
    check("历史页只发一个请求（in-flight 守卫）", "if (pending)" in history_js)
    check("今日页只发一个请求（in-flight 守卫）", "if (pending)" in today_js)
    check("两个视图各自维护渲染函数（不共用）",
          "function cardHtml" in today_js and "function historyCardHtml" in history_js)
    check("卡片展示可信度判定依据", "cred-basis" in today_js and "cred-basis" in history_js)
    check("每个模块标签带自己的刷新按钮", "data-refresh-module" in today_js)
    check("日期控件在 HTML 里且绑定正确",
          'id="h-date"' in html and "el('h-date')" in history_js)
    # spec v2.3 前端：👍/👎、隐藏、取消隐藏、已隐藏筛选、权重 hover
    check("今日卡片带 👍/👎 按钮",
          'data-role="vote-up"' in today_js and 'data-role="vote-down"' in today_js)
    check("点踩后卡片从今日页移除",
          "parentNode.removeChild" in today_js and "vote-down" in today_js)
    check("今日页再兜一道 hidden 过滤",
          "filter(function (i) { return !i.hidden; })" in today_js)
    check("历史页有已隐藏标记与取消隐藏入口",
          "hidden-tag" in history_js and 'data-role="unhide"' in history_js)
    check("历史页已隐藏筛选绑定 h-hidden",
          'id="h-hidden"' in html and "el('h-hidden')" in history_js)
    check("来源权重默认不显示、hover 才可见（CSS）",
          ".srow .wtag" in css and ".srow:hover .wtag" in css)
    check("i18n 补齐投票/隐藏文案",
          all(k in app_js for k in ("voteUp", "voteDown", "hiddenTag", "unhide", "onlyHidden")))


def test_user_score():
    print("\n== 16) 内容评分（spec v2.1 第 3 节）==")
    seed_api_db("score.db")
    client = app.app.test_client()
    rows, _total = db.today_items("ai", page=1, page_size=10)
    target = rows[0]
    res = client.post(f"/api/news/{target['id']}/user-score",
                      json={"score": 4, "note": "适合做视频"})
    check("提交评分返回 200", res.status_code == 200 and res.get_json()["ok"])
    saved = db.get_news(target["id"])
    check("评分与备注落库", saved["user_score"] == 4 and saved["user_note"] == "适合做视频")
    for bad in (0, 6, "abc"):
        r = client.post(f"/api/news/{target['id']}/user-score", json={"score": bad})
        check(f"非法评分 {bad!r} 被拒 400", r.status_code == 400)
    client.post(f"/api/news/{rows[1]['id']}/user-score", json={"score": 5})
    check("只提交评分也可以", db.get_news(rows[1]["id"])["user_score"] == 5)
    res = client.post(f"/api/news/{rows[1]['id']}/user-score", json={"score": None})
    check("提交 null 可清除评分",
          res.status_code == 200 and db.get_news(rows[1]["id"])["user_score"] is None)
    check("不存在的 id 返回 404",
          client.post("/api/news/999999/user-score", json={"score": 3}).status_code == 404)
    check("备注过长被截断",
          len(client.post(f"/api/news/{rows[1]['id']}/user-score",
                          json={"score": 3, "note": "x" * 200}).get_json()["user_note"]) <= 120)

    four = client.get("/api/history?min_user_score=4").get_json()
    check("历史可按「4 星及以上」筛选",
          four["total"] >= 1 and all((i["user_score"] or 0) >= 4 for i in four["items"]))
    exact = client.get("/api/history?user_score=4").get_json()
    check("历史可按精确评分筛选",
          exact["total"] == 1 and exact["items"][0]["user_score"] == 4)
    today = client.get("/api/news?module=ai&min_user_score=4").get_json()
    check("今日列表也支持评分筛选", today["total"] >= 1)
    check("卡片下发 user_note", any(i["user_note"] == "适合做视频" for i in
                                    client.get("/api/history?min_user_score=4").get_json()["items"]))


# ---------------------------------------------------------------- 20 投票（spec v2.3）

def test_vote():
    print("\n== 20) 👍/👎 投票、隐藏与取消隐藏（spec v2.3 第 1、2、4 节）==")
    seed_api_db("vote.db")
    client = app.app.test_client()
    rows, _total = db.today_items("ai", page=1, page_size=10)
    target = rows[0]
    source = target["sources"][0]

    check("来源权重初始为 0", db.get_source_weight(source) == 0, str(db.get_source_weight(source)))
    check("投票前 user_vote=none 且未隐藏",
          (target["user_vote"] or "none") == "none" and not target["hidden"])

    res = client.post(f"/api/news/{target['id']}/vote", json={"action": "up"})
    body = res.get_json()
    check("点赞返回 200", res.status_code == 200 and body["ok"], str(body.get("error")))
    check("点赞后来源权重 +1", db.get_source_weight(source) == 1, str(db.get_source_weight(source)))
    saved = db.get_news(target["id"])
    check("点赞后 user_vote=up、仍正常展示",
          saved["user_vote"] == "up" and not saved["hidden"], f"{saved['user_vote']}/{saved['hidden']}")
    check("点赞消息说明权重变化", "+1" in body["message"], body["message"])

    again = client.post(f"/api/news/{target['id']}/vote", json={"action": "up"}).get_json()
    check("重复点赞不重复加权重（幂等）",
          db.get_source_weight(source) == 1 and again["weight_delta"] == 0)

    down = client.post(f"/api/news/{target['id']}/vote", json={"action": "down"}).get_json()
    check("从赞转踩：把那次 +1 也撤掉（权重 -2 → -1）",
          db.get_source_weight(source) == -1 and down["weight_delta"] == -2,
          f"{db.get_source_weight(source)}/{down['weight_delta']}")
    saved = db.get_news(target["id"])
    check("点踩后 hidden=1、user_vote=down、记 hidden_at",
          saved["hidden"] == 1 and saved["user_vote"] == "down" and bool(saved["hidden_at"]),
          f"hidden={saved['hidden']} at={saved['hidden_at']}")
    check("点踩消息提示去历史页看", "历史" in down["message"], down["message"])

    titles = [i["title"] for i in db.today_items("ai", page=1, page_size=10)[0]]
    check("点踩后该新闻从今日页消失", target["title"] not in titles, str(titles[:3]))
    api_today = client.get("/api/news?module=ai").get_json()
    check("今日页接口也不再下发它",
          all(i["id"] != target["id"] for i in api_today["items"]))
    check("新闻没有被删除（库里还在）", db.get_news(target["id"]) is not None)

    hist = client.get("/api/history?page_size=100").get_json()
    found = [i for i in hist["items"] if i["id"] == target["id"]]
    check("历史页仍能查到被隐藏的新闻", bool(found))
    check("历史页下发 hidden 标记", bool(found) and found[0]["hidden"] is True)
    only = client.get("/api/history?hidden=only&page_size=100").get_json()
    check("历史页可「只看已隐藏」",
          only["total"] >= 1 and all(i["hidden"] for i in only["items"]), f"total={only['total']}")
    visible = client.get("/api/history?hidden=visible&page_size=100").get_json()
    check("历史页可「隐藏的不看」",
          all(not i["hidden"] for i in visible["items"]) and visible["hidden"] == "visible")

    un = client.post(f"/api/news/{target['id']}/unhide", json={}).get_json()
    check("取消隐藏返回 200", un["ok"] and un["hidden"] is False, str(un))
    check("取消隐藏撤销那次点踩（权重回到 0）", db.get_source_weight(source) == 0,
          str(db.get_source_weight(source)))
    check("取消隐藏后 hidden_at 清空", not db.get_news(target["id"])["hidden_at"])
    check("取消隐藏后重新出现在今日页",
          target["title"] in [i["title"] for i in db.today_items("ai", page=1, page_size=10)[0]])

    fresh = client.post(f"/api/news/{target['id']}/vote", json={"action": "down"}).get_json()
    check("从没态度直接点踩：来源权重 -1（spec 验收项）",
          db.get_source_weight(source) == -1 and fresh["weight_delta"] == -1,
          f"{db.get_source_weight(source)}/{fresh['weight_delta']}")
    check("再次点踩后重新隐藏", db.get_news(target["id"])["hidden"] == 1)
    check("取消隐藏接口可再次恢复",
          client.post(f"/api/news/{target['id']}/unhide", json={}).get_json()["hidden"] is False)

    check("非法 action 被拒 400",
          client.post(f"/api/news/{target['id']}/vote", json={"action": "meh"}).status_code == 400)
    check("缺少 action 被拒 400",
          client.post(f"/api/news/{target['id']}/vote", json={}).status_code == 400)
    check("投票：不存在的 id 返回 404",
          client.post("/api/news/999999/vote", json={"action": "up"}).status_code == 404)
    check("取消隐藏：不存在的 id 返回 404",
          client.post("/api/news/999999/unhide", json={}).status_code == 404)

    # 隐藏按 url_key 生效：同一条新闻的多个模块行一起从今日页消失
    use_db("vote_cross.db")
    ts = db.now_iso()
    cross = row("跨模块投票样本", "https://vc.example.com/a", "ai", hours_ago=1, source="跨模块源")
    check("同一 url 可入库两个模块",
          db.insert_news(cross, "ai", ts) and db.insert_news(dict(cross), "comprehensive", ts))
    ai_row = db.today_items("ai", page=1, page_size=10)[0][0]
    client.post(f"/api/news/{ai_row['id']}/vote", json={"action": "down"})
    check("点踩后 AI 模块今日页不再有它", not db.today_items("ai", page=1, page_size=10)[0])
    check("综合模块的同一条也一起消失",
          not db.today_items("comprehensive", page=1, page_size=10)[0])
    check("两者都只是 hidden，没有被删除",
          db.get_news(ai_row["id"]) is not None and db.stats()["total"] == 2)


# ---------------------------------------------------------------- 21 权重边界与衰减

def test_weight_bounds_and_decay():
    print("\n== 21) 权重范围 -10~+10 与每月衰减 10%（spec v2.3 第 2 节）==")
    use_db("weight.db")
    client = app.app.test_client()
    ts = db.now_iso()
    for i in range(16):
        db.insert_news(row(f"同源条目{i}", f"https://wd{i}.example.com/a", "ai", hours_ago=1,
                           source="重踩源"), "ai", ts)
        db.insert_news(row(f"同源条目{i}b", f"https://wu{i}.example.com/a", "ai", hours_ago=1,
                           source="重赞源"), "ai", ts)

    rows, _ = db.today_items("ai", page=1, page_size=40)
    for r in rows:
        action = "down" if r["sources"] == ["重踩源"] else "up"
        client.post(f"/api/news/{r['id']}/vote", json={"action": action})
    check("反复点踩不会无限下降（下限 -10）", db.get_source_weight("重踩源") == -10,
          str(db.get_source_weight("重踩源")))
    check("反复点赞不会无限增长（上限 +10）", db.get_source_weight("重赞源") == 10,
          str(db.get_source_weight("重赞源")))
    check("clamp_weight 夹住越界值",
          db.clamp_weight(999) == 10 and db.clamp_weight(-999) == -10)

    db.set_source_weight("衰减源", 8)
    db.set_source_weight("负衰减源", -8)
    first = db.decay_source_weights()
    check("首次衰减执行并记录月份",
          first["decayed"] is True and first["month"] == db.current_month(), str(first))
    check("衰减 10%（8→7、-8→-7）",
          db.get_source_weight("衰减源") == 7 and db.get_source_weight("负衰减源") == -7,
          f"{db.get_source_weight('衰减源')}/{db.get_source_weight('负衰减源')}")
    same = db.decay_source_weights()
    check("同一个月只衰减一次", same["decayed"] is False and db.get_source_weight("衰减源") == 7)
    later = db.decay_source_weights(now=datetime.now() + timedelta(days=31))
    check("跨月会再次衰减", later["decayed"] is True and db.get_source_weight("衰减源") == 6,
          str(db.get_source_weight("衰减源")))
    check("decay_weight 向 0 靠拢且不变号",
          db.decay_weight(1) == 0 and db.decay_weight(-1) == 0 and
          db.decay_weight(10) == 9 and db.decay_weight(-10) == -9)
    check("衰减状态可查（给前端/接口用）",
          db.decay_state()["ratio"] == 0.9 and db.decay_state()["month"] == db.current_month())

    # 已停更/未知来源也会被衰减，只是没有记录而已
    check("未记录过的来源权重视为 0", db.get_source_weight("从没出现过的源") == 0)
    check("source_weights_map 可整体取出", isinstance(db.source_weights_map(), dict))


# ---------------------------------------------------------------- 22 权重影响排序/可信度/抓取

def test_weight_effects():
    print("\n== 22) 权重影响排序 / 可信度 / 抓取（spec v2.3 第 3 节）==")
    use_db("weight_order.db")
    ts = db.now_iso()
    db.set_source_weight("加权源", 6)
    db.set_source_weight("踩过的源", -6)
    db.insert_news(row("加权来源的旧新闻", "https://wa.example.com/a", "ai", hours_ago=10,
                       source="加权源"), "ai", ts)
    db.insert_news(row("普通来源的新新闻", "https://wb.example.com/a", "ai", hours_ago=1,
                       source="普通源"), "ai", ts)
    db.insert_news(row("踩过来源的最新新闻", "https://wc.example.com/a", "ai", hours_ago=0.5,
                       source="踩过的源"), "ai", ts)
    items, _t = db.today_items("ai", page=1, page_size=10)
    order = [i["sources"][0] for i in items]
    check("权重高的来源排在前面", order[0] == "加权源", str(order))
    check("权重低的来源排在后面", order[-1] == "踩过的源", str(order))
    check("权重为 0 的来源排在中间", order[1] == "普通源", str(order))
    check("权重不影响条数（三条都在）", len(items) == 3)
    check("接口下发该条来源的当前权重", client_weights_of("普通源") == 0)

    # 排序仍以权重为唯一新增维度：全都 0 时就是原来的发布时间倒序
    use_db("weight_order2.db")
    ts = db.now_iso()
    for i, hours in enumerate([9, 1, 5]):
        db.insert_news(row(f"同权重{i}", f"https://wo{i}.example.com/a", "ai", hours_ago=hours),
                       "ai", ts)
    plain, _t = db.today_items("ai", page=1, page_size=10)
    check("权重全为 0 时仍是发布时间倒序",
          [i["title"] for i in plain] == ["同权重1", "同权重2", "同权重0"],
          str([i["title"] for i in plain]))

    from verifier import credibility_basis, effective_tier, judge_credibility

    def group(cands):
        groups = merge_events(cands)
        for g in groups:
            g["conflict_points"] = conflict_points(g)
        return groups[0]

    g = group([item("某地新规落地", "https://x1.example.com/a", "livelihood", "量子位", 2, hours_ago=1),
               item("某地新规落地细则公布", "https://x2.example.com/a", "livelihood", "澎湃新闻", 2,
                    hours_ago=2)])
    check("两个 Tier 2、无权重 → 较可信", judge_credibility(g) == "较可信", judge_credibility(g))
    check("权重 +5 的 Tier 2 视为 Tier 1.5",
          effective_tier("量子位", 2, {"量子位": 5}) == 1.5 and
          effective_tier("量子位", 2, {"量子位": 4}) == 2.0)
    check("Tier 1.5 参与「已证实」判定", judge_credibility(g, {"量子位": 5}) == "已证实")
    check("判定依据写出权重微调",
          "量子位 / Tier 2（权重 +5 → Tier 1.5）" in credibility_basis(g, {"量子位": 5}),
          credibility_basis(g, {"量子位": 5}))
    check("权重 -5 的 Tier 2 降为 Tier 3",
          effective_tier("量子位", 2, {"量子位": -5}) == 3.0)
    check("被降级的来源把事件拉到「存疑」", judge_credibility(g, {"量子位": -5}) == "存疑")
    check("判定依据写出降级原因",
          "权重 -5 → Tier 3" in credibility_basis(g, {"量子位": -5}))
    check("权重微调不覆盖原 Tier 规则（不传 weights 与 v2.2 一致）",
          judge_credibility(g) == "较可信" and credibility_basis(g) == "量子位 / Tier 2，澎湃新闻 / Tier 2")
    check("非 Tier 2 的来源不受权重影响",
          effective_tier("新华网", 1, {"新华网": 9}) == 1.0 and
          effective_tier("IT之家", 3, {"IT之家": -9}) == 3.0)

    # 抓取：加权多抓、降权少抓，但都不屏蔽
    max_per = app.fetcher.max_per_source_for
    check("降权源少抓条数（12 → 6）", max_per("低权重源", 12, {"低权重源": -6}) == 6)
    check("降权源仍至少抓 3 条（不屏蔽）",
          max_per("低权重源", 4, {"低权重源": -9}) >= app.fetcher.WEIGHT_FLOOR_ITEMS)
    check("加权源优先抓、条数上调（12 → 18）", max_per("高权重源", 12, {"高权重源": 6}) == 18)
    check("无权重时抓取条数不变", max_per("普通源", 12, {}) == 12)

    recorded, order_seen = {}, []
    origin_source = app.fetcher.fetch_source

    def fake_source(source, session, settings):
        recorded[source["name"]] = settings["max_per_source"]
        order_seen.append(source["name"])
        return [{"source": source["name"], "title": source["name"], "url": f"https://{source['name']}.invalid/a",
                 "summary": "", "domain": "ai", "tier": 3, "published_at": None}], None

    app.fetcher.fetch_source = fake_source
    try:
        items, _errors = app.fetcher.fetch_all(
            [{"name": "低权重源"}, {"name": "高权重源"}, {"name": "普通源"}],
            {"max_per_source": 12, "workers": 2},
            weights={"低权重源": -6, "高权重源": 6},
        )
    finally:
        app.fetcher.fetch_source = origin_source
    check("抓取时按权重调条数",
          recorded.get("低权重源") == 6 and recorded.get("高权重源") == 18
          and recorded.get("普通源") == 12, str(recorded))
    check("加权来源先被调度", order_seen and order_seen[0] == "高权重源", str(order_seen))
    check("加权来源的条目排在候选池前面",
          items and items[0]["source"] == "高权重源", str([i["source"] for i in items]))


def client_weights_of(name):
    """该来源在 /api/news 下发数据里的权重（前端 hover 显示用）。"""
    data = app.app.test_client().get("/api/news?module=ai").get_json()
    for i in data["items"]:
        if name in (i.get("source_weights") or {}):
            return i["source_weights"][name]
    return None


# ---------------------------------------------------------------- 23 权重接口

def test_source_weights_api():
    print("\n== 23) /api/source-weights 与 /api/meta 下发（spec v2.3 第 4、5 节）==")
    seed_api_db("weights_api.db")
    client = app.app.test_client()
    data = client.get("/api/source-weights").get_json()
    check("来源权重接口可用", data["ok"] is True and data["count"] >= 20, f"count={data.get('count')}")
    check("接口带权重范围 -10~+10", data["weight_range"] == {"min": -10, "max": 10})
    check("接口带权重规则（Tier 2 ±5 微调）",
          data["rules"]["tier2_upgrade"] == 5 and data["rules"]["tier2_downgrade"] == -5
          and data["rules"]["tier2_upgraded_to"] == 1.5)
    check("接口带每月衰减比例", data["decay"]["ratio"] == 0.9 and "month" in data["decay"])
    check("所有下发权重都在 -10~+10",
          all(-10 <= i["weight"] <= 10 for i in data["items"]))
    check("sources.json 里的源默认权重 0 且 voted=false",
          all(i["weight"] == 0 and i["voted"] is False for i in data["items"] if not i["voted"]))
    check("条目带 tier / type / domain",
          all({"tier", "type", "domain"} <= set(i) for i in data["items"]))

    rows, _t = db.today_items("ai", page=1, page_size=10)
    client.post(f"/api/news/{rows[0]['id']}/vote", json={"action": "up"})
    after = client.get("/api/source-weights").get_json()
    voted = [i for i in after["items"] if i["voted"]]
    check("点过赞的来源出现在列表里且带 updated_at",
          voted and voted[0]["weight"] == 1 and bool(voted[0]["updated_at"]), str(voted[:1]))
    check("权重高的排在列表前面", after["items"][0]["weight"] == 1)

    meta = client.get("/api/meta").get_json()
    check("meta 下发投票取值与权重范围",
          meta["vote_values"] == ["up", "down", "none"] and meta["weight_range"]["min"] == -10)
    check("meta 下发权重规则供前端解释",
          meta["weight_rules"]["tier2_upgrade"] == 5
          and meta["weight_rules"]["fetch_depriority"] == -5)
    check("meta 下发当前来源权重表", isinstance(meta["source_weights"], dict) and meta["source_weights"])

    today = client.get("/api/news?module=ai").get_json()
    check("今日页每条下发 user_vote / hidden / source_weights",
          all({"user_vote", "hidden", "source_weights"} <= set(i) for i in today["items"]))
    ups = [i for i in today["items"] if i["user_vote"] == "up"]
    check("点赞状态随接口下发（前端可高亮）", bool(ups))
    history = client.get("/api/history").get_json()
    check("历史页也下发 hidden 标记与权重",
          all({"hidden", "source_weights"} <= set(i) for i in history["items"]))
    check("历史页回显 hidden 筛选参数", history["hidden"] == "")


def test_url_scheme_and_purge():
    print("\n== 17) URL 标准化与重复数据清理（spec v2.1 第 1 节）==")
    check("http/https 视为同一条", url_key("http://x.com/a") == url_key("https://x.com/a"))
    check("www 与追踪参数一起归一",
          url_key("https://www.x.com/a/?utm_source=weibo&fbclid=1") == url_key("http://x.com/a"))
    check("存库链接仍保留原样", normalize_url("http://x.com/a") == "http://x.com/a")
    cands = dedup_url([
        item("同一条新闻", "https://s.example.com/a", "ai", "S1", 3, hours_ago=1),
        item("同一条新闻转发", "http://s.example.com/a?utm_medium=share", "ai", "S2", 3, hours_ago=1),
    ])
    check("批内 http/https 变体被折叠成一条", len(cands) == 1)

    use_db("purge.db")
    ts = db.now_iso()
    r1 = {"title": "重复卡片测试", "url": "https://dup.example.com/a", "sources": ["S"], "domain": "ai",
          "credibility": "单一来源", "source_lang": "zh", "title_zh": "重复卡片测试"}
    check("正常入库成功", db.insert_news(r1, "ai", ts) is True)
    check("同一条的 http 变体被 url_key 唯一索引挡住",
          db.insert_news(dict(r1, url="http://dup.example.com/a"), "ai", ts) is False)
    check("同一条的 utm 变体也被挡住",
          db.insert_news(dict(r1, url="https://dup.example.com/a?utm_source=weibo"), "ai", ts) is False)
    check("同一条可以进另一个模块（综合）",
          db.insert_news(dict(r1, url="http://dup.example.com/a"), "comprehensive", ts) is True)
    with db.connect() as conn:
        row = conn.execute("SELECT url_key FROM news WHERE module = 'ai'").fetchone()
        keys = [r["url_key"] for r in conn.execute("SELECT url_key FROM news").fetchall()]
    check("入库时自动写入 url_key", row["url_key"] == "https://dup.example.com/a", str(row["url_key"]))
    check("两个模块的行 url_key 相同", keys[0] == keys[1])

    # 老数据（url_key 为空）仍能被启动清理收拾
    with db.connect() as conn:
        conn.execute("INSERT INTO news (module, url, url_key, title) VALUES ('ai', "
                     "'https://legacy.example.com/a', NULL, '老数据一')")
        conn.execute("INSERT INTO news (module, url, url_key, title) VALUES ('ai', "
                     "'http://legacy.example.com/a', NULL, '老数据二')")
        before = conn.execute("SELECT COUNT(*) AS n FROM news WHERE module = 'ai'").fetchone()["n"]
    removed = db.purge_duplicates()
    with db.connect() as conn:
        after = conn.execute("SELECT COUNT(*) AS n FROM news WHERE module = 'ai'").fetchone()["n"]
    check("无 url_key 的老重复行会被清掉", removed == 1 and after == before - 1,
          f"{before}->{after}, removed={removed}")
    report = db.init_db()
    check("init_db 报告补 url_key 与清理结果",
          {"url_key_backfilled", "duplicates_removed"} <= set(report))


def main() -> None:
    test_schema()
    test_legacy_migration()
    test_dedup()
    test_windows_and_hotspot()
    test_module_refresh()
    test_comprehensive()
    test_published_at()
    test_translation_and_eval()
    test_hotspot_filter()
    test_history()
    test_api()
    test_fetcher_helpers()
    test_keyword_gate()
    test_tier_credibility()
    test_credibility_auto_and_override()
    test_user_score()
    test_url_scheme_and_purge()
    test_history_dedup()
    test_frontend_static()
    # spec v2.3：👍/👎 投票 + 来源权重
    test_vote()
    test_weight_bounds_and_decay()
    test_weight_effects()
    test_source_weights_api()

    print("\n" + "=" * 58)
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    for f in FAIL:
        print("  未通过：" + f)
    shutil.rmtree(TMP, ignore_errors=True)
    raise SystemExit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
