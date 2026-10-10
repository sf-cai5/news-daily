"""每日新闻聚合器 — Flask 入口与刷新引擎（spec v2）。

模块：comprehensive（综合） / ai / livelihood / society_cn / society_intl
- 单独模块：只抓本领域源 → 模块内去重 → 多源验证 → 可信度 → 热点对半分配 →
  时窗放宽 24h→48h→72h → 不跨领域补位。
- 综合模块：不独立抓取，依次触发四个单独模块刷新，再按配额混合。

刷新在后台线程里跑，POST 立刻返回，前端轮询 /api/refresh/status 看进度。
"""

from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime, timedelta

from flask import Flask, jsonify, render_template, request

import db
import fetcher
from dedup import dedup_url, is_duplicate, normalize_title, url_key
from prompt_builder import build_hotspot_prompt, build_prompt, build_translation_prompt
from verifier import (CREDIBILITY_EN, CREDIBILITY_LEVELS, CREDIBILITY_RANK, TIER_LABELS,
                      TIER_RANK, TIER_UPGRADED, WEIGHT_DOWNGRADE, WEIGHT_UPGRADE,
                      conflict_points, credibility_basis, judge_credibility, merge_events)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SOURCES_FILE = os.path.join(BASE_DIR, "sources.json")

DISPLAY_LIMIT = 10                 # 每个模块每页 10 条
HISTORY_PAGE_SIZE = 20             # 历史每页 20 条
HOT_THRESHOLD = 3                  # 评分 >= 3 算高热点
HOT_QUOTA = 5                      # 单独模块：高热点 5 条
NORMAL_QUOTA = 5                   # 单独模块：普通 5 条
WINDOWS = [24, 48, 72]             # 时窗放宽：24h → 48h → 72h
COMPREHENSIVE_QUOTA = {"ai": 3, "livelihood": 3, "society_cn": 2, "society_intl": 2}

# 纯英文关键词按整词匹配，避免 "ai" 命中 "said"/"air"
_WORD_RE = {}

MODULES = ["comprehensive"] + db.DOMAIN_MODULES
MODULE_LABELS = {
    "comprehensive": {"zh": "综合", "en": "All"},
    "ai": {"zh": "AI", "en": "AI"},
    "livelihood": {"zh": "民生", "en": "Livelihood"},
    "society_cn": {"zh": "社会国内", "en": "Society (China)"},
    "society_intl": {"zh": "社会国外", "en": "Society (World)"},
}
DOMAIN_LABELS = MODULE_LABELS

app = Flask(__name__)
app.config["JSON_AS_ASCII"] = False

# 同一时间只跑一个刷新任务（综合刷新本身就会依次触发四个模块）
REFRESH_LOCK = threading.Lock()
REFRESH_STATE = {m: {"status": "idle", "progress": "", "at": None, "result": None} for m in MODULES}
RUNNING = {"module": None}


# ---------------------------------------------------------------- 配置

def load_config() -> tuple:
    with open(SOURCES_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, list):
        return data, {}
    settings = dict(data.get("settings", {}))
    # 模块关键词门放在顶层，取用时并进 settings，调用方只认 settings
    if data.get("module_filters"):
        settings["module_filters"] = data["module_filters"]
    return data.get("sources", []), settings


# ---------------------------------------------------------------- 排序与挑选

def _rank(group: dict) -> tuple:
    return (
        CREDIBILITY_RANK.get(group.get("credibility", ""), 0),
        TIER_RANK.get(int(group.get("tier") or 4), 1),
        group.get("published_at") or "",
    )


def _within_window(group: dict, hours: int, now: datetime) -> bool:
    """发布时间落在窗口内。时间未知的条目算可选（排序时排最后）。"""
    ts = group.get("published_at") or group.get("published")
    if not ts:
        return True
    try:
        dt = datetime.fromisoformat(str(ts))
    except ValueError:
        return True
    if dt.tzinfo is not None:
        dt = dt.astimezone().replace(tzinfo=None)
    return dt >= now - timedelta(hours=hours)


def split_hotspot(eligible: list) -> tuple:
    """5 条高热点（评分 >= 3）+ 5 条普通；不够时互相补，标“高热点不足”。"""
    ordered = sorted(eligible, key=_rank, reverse=True)
    hot = [g for g in ordered if (g.get("hotspot_score") or 0) >= HOT_THRESHOLD]
    normal = [g for g in ordered if (g.get("hotspot_score") or 0) < HOT_THRESHOLD]

    picked = hot[:HOT_QUOTA] + normal[:NORMAL_QUOTA]
    used = {id(g) for g in picked}
    for g in ordered:  # 还差就用剩下的补
        if len(picked) >= DISPLAY_LIMIT:
            break
        if id(g) not in used:
            picked.append(g)
            used.add(id(g))
    picked = picked[:DISPLAY_LIMIT]
    flags = {
        "hotspot_short": len(hot) < HOT_QUOTA,
        "hotspot_count": len(hot),
        "normal_count": len(picked) - len(hot[:HOT_QUOTA]),
        "shortage": len(picked) < DISPLAY_LIMIT,
    }
    return picked, flags


def select_module(groups: list) -> tuple:
    """时窗放宽：先用 24h，凑不满 10 条放到 48h，再不行放到 72h。"""
    now = datetime.now()
    picked, flags, window_used = [], {}, WINDOWS[-1]
    for hours in WINDOWS:
        eligible = [g for g in groups if _within_window(g, hours, now)]
        picked, flags = split_hotspot(eligible)
        window_used = hours
        if len(picked) >= DISPLAY_LIMIT:
            break
    return picked, flags, window_used


# ---------------------------------------------------------------- 行构造

def keyword_match(text: str, keywords: list) -> bool:
    """模块关键词门：中文关键词按子串，纯英文关键词按整词（避免 ai 命中 said）。"""
    if not keywords:
        return True
    text = text or ""
    low = text.lower()
    for kw in keywords:
        if kw.isascii():
            pattern = _WORD_RE.get(kw)
            if pattern is None:
                pattern = re.compile(r"(?<![a-z0-9])" + re.escape(kw) + r"(?![a-z0-9])")
                _WORD_RE[kw] = pattern
            if pattern.search(low):
                return True
        elif kw in text:
            return True
    return False


def filter_by_keywords(candidates: list, keywords: list, fields: tuple = ("title",)) -> tuple:
    """按模块关键词过滤。默认只看标题：摘要里的背景文字（“AI 数据中心”“智能家居”）
    会把无关条目带进来，实测噪声太大。没有配置关键词的模块原样通过。
    """
    if not keywords:
        return candidates, 0
    kept, dropped = [], 0
    for it in candidates:
        text = " ".join(str(it.get(f) or "") for f in fields)
        if keyword_match(text, keywords):
            kept.append(it)
        else:
            dropped += 1
    return kept, dropped


def to_row(group: dict, module: str, ts: str) -> dict:
    """把合并后的候选转成 news 表的一行（双语字段按原文语言落位）。"""
    lang = group.get("source_lang") or "zh"
    title = (group.get("title") or "").strip()
    summary = (group.get("summary") or "").strip()
    published = group.get("published_at") or group.get("published")
    row = {
        "module": module,
        "domain": group.get("domain") or (module if module in db.DOMAIN_MODULES else ""),
        "title": title,
        "title_zh": title if lang == "zh" else None,
        "title_en": title if lang == "en" else None,
        "url": group.get("url") or "",
        "sources": group.get("sources") or [],
        "summary": summary,
        "summary_zh": summary if lang == "zh" else None,
        "summary_en": summary if lang == "en" else None,
        "source_lang": lang,
        "translation_status": "未翻译",
        "credibility": group.get("credibility") or "单一来源",
        "credibility_auto": group.get("credibility") or "单一来源",
        "credibility_basis": group.get("credibility_basis") or "",
        "conflict_note": "；".join(group.get("conflict_points") or []),
        "oral_prompt": "",
        "hotspot_score": None,
        "highlight": "",
        "hotspot_status": "未生成",
        "user_score": None,
        "user_note": None,
        "published_at": published,
        "published_at_source": group.get("published_at_source") or ("rss" if published else "unknown"),
        "shown_at": ts,
        "module_shown_at": ts,
        "created_at": ts,
    }
    row["oral_prompt"] = build_prompt(row)
    return row


# ---------------------------------------------------------------- 刷新：单独模块

def refresh_module(module: str) -> dict:
    """只抓该领域源，模块内独立去重，不跨领域补位。来源权重影响抓取与可信度微调。"""
    sources, settings = load_config()
    domain_sources = [s for s in sources if s.get("domain") == module]
    if not domain_sources:
        return {"ok": False, "module": module, "added": 0, "skipped": 0,
                "message": "该模块没有配置新闻源", "flags": {"shortage": True, "hotspot_short": True}}

    weights = db.source_weights_map()          # spec v2.3：👍/👎 累积出来的来源偏好
    candidates, errors = fetcher.fetch_all(domain_sources, settings, weights=weights)
    if not candidates:
        return {"ok": False, "module": module, "added": 0, "skipped": 0,
                "message": "抓取失败，请稍后重试", "errors": errors[:10],
                "flags": {"shortage": True, "hotspot_short": True}}

    # 模块关键词门（如 AI 模块只收 AI 相关条目），避免综合类科技源把杂事塞进来
    keywords = (settings.get("module_filters") or {}).get(module) or []
    candidates, filtered_out = filter_by_keywords(candidates, keywords)

    # 模块内去重（同一 url 只在本模块内唯一；不同模块互不影响）
    candidates = dedup_url(candidates)
    known_urls = {url_key(u) for u in db.module_urls(module)}
    known_titles = [normalize_title(t) for t in db.module_titles(module)]
    fresh, skipped = [], 0
    for it in candidates:
        if url_key(it["url"]) in known_urls:
            skipped += 1
            continue
        if is_duplicate(it["title"], known_titles):
            skipped += 1
            continue
        known_titles.append(normalize_title(it["title"]))
        fresh.append(it)

    if not fresh:
        message = "本轮抓到的都是已展示过的，无新增"
        if filtered_out:
            message += f"（另有 {filtered_out} 条被模块关键词过滤）"
        return {"ok": False, "module": module, "added": 0, "skipped": skipped,
                "filtered": filtered_out, "message": message,
                "flags": {"shortage": True, "hotspot_short": True}, "errors": errors[:10]}

    groups = merge_events(fresh)
    for g in groups:
        g["conflict_points"] = conflict_points(g)
        # 权重 ≥ +5 的 Tier 2 源视为 Tier 1.5、≤ -5 的降为 Tier 3（spec v2.3 第 3.2 节）
        g["credibility"] = judge_credibility(g, weights)
        g["credibility_basis"] = credibility_basis(g, weights)

    picked, flags, window_used = select_module(groups)
    if picked:
        try:
            fetcher.enrich_summaries(picked, settings)
        except Exception:
            pass

    ts = db.now_iso()
    added = 0
    for g in picked:
        if db.insert_news(to_row(g, module, ts), module, ts):
            added += 1

    flags["shortage"] = added < DISPLAY_LIMIT
    message = f"新增 {added} 条，跳过重复 {skipped} 条"
    if filtered_out:
        message += f" · 关键词过滤 {filtered_out} 条"
    if flags.get("hotspot_short"):
        message += f" · 高热点不足（{flags.get('hotspot_count', 0)}/{HOT_QUOTA}，先回填评价再统计）"
    if flags.get("shortage"):
        message += " · 今日不足"
    result = {"ok": True, "module": module, "added": added, "skipped": skipped,
              "filtered": filtered_out, "window_hours": window_used, "flags": flags,
              "message": message, "errors": errors[:10]}
    db.set_meta(**{f"refresh_{module}": result})
    return result


# ---------------------------------------------------------------- 刷新：综合

def _comprehensive_order(domain: str, rows: list) -> list:
    """民生与社会议题优先热点；AI 不强制热点，按发布时间倒序。"""
    if domain == "ai":
        return sorted(rows, key=lambda r: (r.get("published_at") is None,
                                           r.get("published_at") or ""), reverse=True)
    return sorted(rows, key=lambda r: ((r.get("hotspot_score") or 0),
                                       r.get("published_at") or ""), reverse=True)


def compose_comprehensive(ts: str | None = None) -> dict:
    """综合模块 = 各单独模块按配额混合：AI 不强制热点，其余优先热点；不跨领域补位。"""
    ts = ts or db.now_iso()
    added, skipped, by_domain, missing = 0, 0, {}, []
    for domain, quota in COMPREHENSIVE_QUOTA.items():
        rows = _comprehensive_order(domain, db.latest_module_batch(domain, limit=60))
        taken = 0
        for row in rows:
            if taken >= quota:
                break
            if db.insert_news(row, "comprehensive", ts):
                added += 1
                taken += 1
            else:
                skipped += 1
        by_domain[domain] = taken
        if taken < quota:
            missing.append(f"{MODULE_LABELS[domain]['zh']} {taken}/{quota}")

    shortage = bool(missing)
    message = f"新增 {added} 条，跳过重复 {skipped} 条"
    if shortage:
        message += " · 今日不足：" + "、".join(missing)
    return {"ok": True, "module": "comprehensive", "added": added, "skipped": skipped,
            "filtered": 0, "by_domain": by_domain,
            "flags": {"shortage": shortage, "hotspot_short": False},
            "message": message}


def refresh_comprehensive() -> dict:
    """综合模块不独立抓取：依次触发四个单独模块刷新，再按配额混合。"""
    module_results = {}
    for domain in db.DOMAIN_MODULES:
        label = MODULE_LABELS[domain]["zh"]
        REFRESH_STATE[domain].update(status="running",
                                     progress=f"综合刷新：正在刷新 {label} 模块…",
                                     at=db.now_iso())
        REFRESH_STATE["comprehensive"].update(status="running",
                                              progress=f"依次刷新子模块：{label}…",
                                              at=db.now_iso())
        try:
            module_results[domain] = refresh_module(domain)
        except Exception as exc:
            module_results[domain] = {"ok": False, "module": domain, "added": 0, "skipped": 0,
                                      "message": f"{type(exc).__name__}: {exc}",
                                      "flags": {"shortage": True, "hotspot_short": True}}
        REFRESH_STATE[domain].update(status="done", progress="", at=db.now_iso(),
                                     result=module_results[domain])
    REFRESH_STATE["comprehensive"].update(progress="按配额混合各模块…", at=db.now_iso())

    result = compose_comprehensive()
    result["sub_modules"] = module_results
    db.set_meta(refresh_comprehensive=result)
    return result


# ---------------------------------------------------------------- 刷新任务

def _refresh_job(module: str) -> None:
    try:
        REFRESH_STATE[module].update(status="running", progress="正在抓取新闻源…", at=db.now_iso())
        result = refresh_comprehensive() if module == "comprehensive" else refresh_module(module)
        status = "done" if result.get("ok") else "error"
        REFRESH_STATE[module].update(status=status, progress="", at=db.now_iso(), result=result)
    except Exception as exc:  # 任何异常都按抓取失败处理，保留上次展示
        message = "抓取失败，请稍后重试"
        result = {"ok": False, "module": module, "added": 0, "skipped": 0,
                  "message": f"{message}（{type(exc).__name__}: {exc}）",
                  "flags": {"shortage": True, "hotspot_short": True}}
        REFRESH_STATE[module].update(status="error", progress="", at=db.now_iso(), result=result)
        db.set_meta(**{f"refresh_{module}": result})
    finally:
        with REFRESH_LOCK:
            RUNNING["module"] = None


# ---------------------------------------------------------------- 序列化

def present(item: dict, lang: str = "zh", weights: dict | None = None) -> dict:
    """按界面语言产出展示字段：缺译文就回退原文并标“未翻译”。"""
    out = dict(item)
    if lang == "en":
        out["display_title"] = item.get("title_en") or item.get("title")
        out["display_summary"] = item.get("summary_en") or item.get("summary")
        has_translation = bool(item.get("title_en") or item.get("summary_en"))
    else:
        out["display_title"] = item.get("title_zh") or item.get("title")
        out["display_summary"] = item.get("summary_zh") or item.get("summary")
        has_translation = bool(item.get("title_zh") or item.get("summary_zh")) or \
            (item.get("source_lang") or "zh") == "zh"
    out["display_is_translation"] = has_translation
    out["translation_status"] = item.get("translation_status") or "未翻译"
    out["hotspot_status"] = item.get("hotspot_status") or "未生成"
    out["published_display"] = item.get("published_at") or ""
    out["published_unknown"] = not bool(item.get("published_at"))
    # 可信度：credibility 为最终展示值（可能被人工覆盖），credibility_auto 为系统判定值
    out["credibility_auto"] = item.get("credibility_auto") or item.get("credibility")
    out["credibility_manual"] = bool(item.get("credibility")) and \
        item.get("credibility_auto") != item.get("credibility")
    out["credibility_basis"] = item.get("credibility_basis") or basis_from_sources(item)
    out["user_score"] = item.get("user_score")
    out["user_note"] = item.get("user_note") or ""
    # spec v2.3：投票状态 + 该条各来源的当前权重（卡片上默认不显示，hover 才显示）
    out["user_vote"] = (item.get("user_vote") or "none")
    out["hidden"] = bool(item.get("hidden"))
    out["hidden_at"] = item.get("hidden_at") or ""
    out["hidden_basis"] = item.get("hidden_basis") or ""
    weight_map = weights if weights is not None else db.source_weights_map()
    out["source_weights"] = {name: int(weight_map.get(name, 0) or 0)
                             for name in (item.get("sources") or [])}
    # 三种提示词都由系统生成，用户复制出去给网页版大模型
    out["oral_prompt"] = item.get("oral_prompt") or build_prompt(item)
    out["hotspot_prompt"] = build_hotspot_prompt(item)
    out["translation_prompt"] = build_translation_prompt(item)
    return out


_TIER_CACHE = {"mtime": None, "map": {}}


def source_tier_map() -> dict:
    """源名 → {tier, type}，前端在来源列表与判定依据里显示分级用。

    sources.json 改过才重新读，卡片渲染是逐条调用，不能每次都读文件。
    """
    try:
        mtime = os.path.getmtime(SOURCES_FILE)
    except OSError:
        mtime = None
    if mtime is not None and _TIER_CACHE["mtime"] != mtime:
        sources, _settings = load_config()
        _TIER_CACHE["map"] = {s.get("name", ""): {"tier": fetcher.source_tier(s),
                                                  "type": s.get("type", "other")}
                              for s in sources}
        _TIER_CACHE["mtime"] = mtime
    return _TIER_CACHE["map"]


def basis_from_sources(item: dict) -> str:
    """老数据没有存判定依据时，按当前来源分级现算一遍。"""
    tiers = source_tier_map()
    names = item.get("sources") or []
    parts = []
    for name in names:
        tier = (tiers.get(name) or {}).get("tier", 4)
        parts.append(f"{name} / Tier {tier}")
    return "，".join(parts)


def weights_map() -> dict:
    """当前来源权重。每个请求读一次，避免逐条卡片重复查库。"""
    return db.source_weights_map()


def hidden_filter() -> str | None:
    """历史页的已隐藏筛选：'' / 'only' / 'visible'。"""
    raw = (request.args.get("hidden") or "").strip().lower()
    if raw in ("1", "only", "true", "yes"):
        return "only"
    if raw in ("0", "visible", "false", "no"):
        return "visible"
    return None


def filter_kwargs() -> dict:
    return {
        "domain": (request.args.get("domain") or "").strip() or None,
        "credibility": (request.args.get("credibility") or "").strip() or None,
        "min_hotspot": request.args.get("min_hotspot") or None,
        "user_score": request.args.get("user_score") or None,
        "min_user_score": request.args.get("min_user_score") or None,
        "q": (request.args.get("q") or "").strip() or None,
    }


def pagination(page_size_default: int) -> tuple:
    try:
        page = max(1, int(request.args.get("page", 1)))
    except (TypeError, ValueError):
        page = 1
    try:
        page_size = int(request.args.get("page_size", page_size_default))
    except (TypeError, ValueError):
        page_size = page_size_default
    page_size = max(1, min(100, page_size))
    return page, page_size


# ---------------------------------------------------------------- 路由

@app.route("/")
def index():
    return render_template("index.html")


@app.get("/api/meta")
def api_meta():
    """前端初始化用：模块清单、配额、四级标签、双语标签、上次刷新结果。"""
    meta = db.get_meta()
    return jsonify({
        "modules": [{"key": m, "label": MODULE_LABELS[m]} for m in MODULES],
        "domain_modules": db.DOMAIN_MODULES,
        "module_labels": MODULE_LABELS,
        "comprehensive_quota": COMPREHENSIVE_QUOTA,
        "module_quota": {"hot": HOT_QUOTA, "normal": NORMAL_QUOTA, "hot_threshold": HOT_THRESHOLD},
        "display_limit": DISPLAY_LIMIT,
        "history_page_size": HISTORY_PAGE_SIZE,
        "windows": WINDOWS,
        "credibility_levels": CREDIBILITY_LEVELS,
        "credibility_en": CREDIBILITY_EN,
        "translation_status_values": db.TRANSLATION_STATUS,
        "hotspot_status_values": db.HOTSPOT_STATUS,
        "tier_labels": TIER_LABELS,
        "source_tiers": source_tier_map(),
        # spec v2.3：投票与来源权重
        "vote_values": db.VOTE_VALUES,
        "source_weights": weights_map(),
        "weight_range": {"min": db.WEIGHT_MIN, "max": db.WEIGHT_MAX},
        "weight_rules": {
            "tier2_upgrade": WEIGHT_UPGRADE, "tier2_upgraded_to": TIER_UPGRADED,
            "tier2_downgrade": WEIGHT_DOWNGRADE, "tier2_downgraded_to": 3,
            "fetch_priority": fetcher.WEIGHT_PRIORITY,
            "fetch_depriority": fetcher.WEIGHT_DEPRIORITY,
            "decay_monthly": db.WEIGHT_DECAY_RATIO,
        },
        "weight_decay_month": meta.get(db.WEIGHT_DECAY_KEY, ""),
        "last_refresh": {k.replace("refresh_", ""): v for k, v in meta.items() if k.startswith("refresh_")},
        "stats": db.stats(),
    })


@app.get("/api/news")
def api_news():
    module = (request.args.get("module") or "comprehensive").strip()
    if module not in MODULES:
        return jsonify({"ok": False, "error": "未知模块", "modules": MODULES}), 400
    lang = "en" if (request.args.get("lang") or "zh").lower().startswith("en") else "zh"
    sort = (request.args.get("sort") or "published").strip()
    page, page_size = pagination(DISPLAY_LIMIT)

    items, total = db.today_items(module, page=page, page_size=page_size, sort=sort,
                                  **filter_kwargs())
    pages = max(1, (total + page_size - 1) // page_size)
    counts = db.module_counts(module)
    meta = db.get_meta()
    wmap = weights_map()
    return jsonify({
        "ok": True,
        "module": module,
        "module_label": MODULE_LABELS[module],
        "lang": lang,
        "sort": sort,
        "page": page,
        "page_size": page_size,
        "pages": pages,
        "total": total,
        "count": len(items),
        "items": [present(i, lang, wmap) for i in items],
        "counts": counts,
        "quota": COMPREHENSIVE_QUOTA if module == "comprehensive"
        else {"hot": HOT_QUOTA, "normal": NORMAL_QUOTA, "hot_threshold": HOT_THRESHOLD},
        "shortage": counts["total"] < DISPLAY_LIMIT,
        "credibility_levels": CREDIBILITY_LEVELS,
        "credibility_en": CREDIBILITY_EN,
        "domain_labels": DOMAIN_LABELS,
        "tier_labels": TIER_LABELS,
        "source_tiers": source_tier_map(),
        "source_weights": wmap,
        "weight_range": {"min": db.WEIGHT_MIN, "max": db.WEIGHT_MAX},
        "refresh_state": REFRESH_STATE[module],
        "last_refresh": meta.get(f"refresh_{module}"),
        "db_total": db.stats()["total"],
    })


@app.post("/api/refresh")
def api_refresh():
    module = (request.args.get("module") or "comprehensive").strip()
    if module not in MODULES:
        return jsonify({"ok": False, "error": "未知模块", "modules": MODULES}), 400
    with REFRESH_LOCK:
        if RUNNING["module"]:
            return jsonify({"ok": False, "busy": True, "running": RUNNING["module"],
                            "message": "已有刷新任务在进行"}), 409
        RUNNING["module"] = module
        REFRESH_STATE[module].update(status="running", progress="排队中…", at=db.now_iso())
    threading.Thread(target=_refresh_job, args=(module,), daemon=True).start()
    return jsonify({"ok": True, "started": True, "module": module})


@app.get("/api/refresh/status")
def api_refresh_status():
    meta = db.get_meta()
    return jsonify({
        "running": RUNNING["module"],
        "modules": REFRESH_STATE,
        "last_refresh": {k.replace("refresh_", ""): v for k, v in meta.items() if k.startswith("refresh_")},
    })


@app.get("/api/history")
def api_history():
    lang = "en" if (request.args.get("lang") or "zh").lower().startswith("en") else "zh"
    module = (request.args.get("module") or "").strip() or None
    if module and module not in MODULES:
        return jsonify({"ok": False, "error": "未知模块", "modules": MODULES}), 400
    date = (request.args.get("date") or "").strip() or None
    page, page_size = pagination(HISTORY_PAGE_SIZE)
    sort = (request.args.get("sort") or "published").strip()
    # 历史默认按 url_key 跨模块去重，避免同一条新闻在综合与领域模块各显示一遍
    dedup = (request.args.get("dedup") or "1").strip() not in ("0", "false", "no")
    kw = filter_kwargs()
    if module:
        kw["module"] = module
    hidden = hidden_filter()
    items, total = db.history_items(page=page, page_size=page_size, date=date, sort=sort,
                                    dedup=dedup, hidden=hidden, **kw)
    pages = max(1, (total + page_size - 1) // page_size)
    wmap = weights_map()
    return jsonify({
        "ok": True,
        "lang": lang,
        "page": page,
        "page_size": page_size,
        "pages": pages,
        "total": total,
        "count": len(items),
        "items": [present(i, lang, wmap) for i in items],
        "dedup": dedup,
        "hidden": hidden or "",
        "filters": {"date": date, "module": module, "hidden": hidden or "",
                    **{k: v for k, v in kw.items() if k != "module"}},
        "available_dates": db.module_dates(module or "comprehensive"),
        "modules": MODULES,
        "module_labels": MODULE_LABELS,
        "domain_labels": DOMAIN_LABELS,
        "credibility_levels": CREDIBILITY_LEVELS,
        "credibility_en": CREDIBILITY_EN,
        "tier_labels": TIER_LABELS,
        "source_tiers": source_tier_map(),
        "source_weights": wmap,
        "weight_range": {"min": db.WEIGHT_MIN, "max": db.WEIGHT_MAX},
        "readonly": True,
    })


@app.get("/api/history/<int:news_id>")
def api_history_detail(news_id: int):
    lang = "en" if (request.args.get("lang") or "zh").lower().startswith("en") else "zh"
    item = db.get_news(news_id)
    if not item:
        return jsonify({"ok": False, "error": "新闻不存在"}), 404
    detail = present(item, lang, weights_map())
    detail["readonly"] = True
    return jsonify({"ok": True, "item": detail})


@app.post("/api/news/<int:news_id>/vote")
def api_vote(news_id: int):
    """👍/👎（spec v2.3 第 4 节）。

    - up：来源权重 +1，新闻照常展示
    - down：来源权重 -1，该新闻从今日页隐藏（历史页仍可查，可取消隐藏）
    绝不删除新闻，也绝不因点踩屏蔽整个来源。
    """
    data = request.get_json(silent=True) or {}
    action = (data.get("action") or "").strip().lower()
    if action not in db.VOTE_VALUES:
        return jsonify({"ok": False, "error": "action 必须是 up / down / none",
                        "allowed": db.VOTE_VALUES}), 400
    if not db.get_news(news_id):
        return jsonify({"ok": False, "error": "新闻不存在"}), 404
    updated = db.update_vote(news_id, action)
    if not updated:
        return jsonify({"ok": False, "error": "投票失败，请稍后重试"}), 400
    delta = updated["weight_delta"]
    if action == "down":
        message = f"已隐藏，可在历史页查看（来源权重 {delta:+d}）"
    elif action == "up":
        message = f"已点赞（来源权重 {delta:+d}）"
    else:
        message = "已撤销投票"
    if action == "up" and updated["previous_vote"] == "up":
        message = "已经点过赞了，权重不变"
    return jsonify({
        "ok": True,
        "id": news_id,
        "action": action,
        "user_vote": updated["user_vote"],
        "hidden": bool(updated["hidden"]),
        "hidden_at": updated["hidden_at"] or "",
        "weights": updated["weights"],
        "weight_delta": delta,
        "weight_range": {"min": db.WEIGHT_MIN, "max": db.WEIGHT_MAX},
        "message": message,
    })


@app.post("/api/news/<int:news_id>/unhide")
def api_unhide(news_id: int):
    """取消隐藏：恢复展示，并回退那次点踩的权重（spec v2.3 第 2、5 节）。"""
    if not db.get_news(news_id):
        return jsonify({"ok": False, "error": "新闻不存在"}), 404
    updated = db.unhide_news(news_id)
    if not updated:
        return jsonify({"ok": False, "error": "取消隐藏失败，请稍后重试"}), 400
    return jsonify({
        "ok": True,
        "id": news_id,
        "user_vote": updated["user_vote"],
        "hidden": bool(updated["hidden"]),
        "hidden_at": "",
        "weights": updated["weights"],
        "restored_ids": updated.get("restored_ids") or [],
        "message": "已取消隐藏，该新闻恢复展示",
    })


@app.get("/api/source-weights")
def api_source_weights():
    """查看所有来源权重（spec v2.3 第 4 节）：sources.json 里的源默认 0，点过 👍/👎 的带真实值。"""
    sources, _settings = load_config()
    stored = {r["source_name"]: r for r in db.source_weight_rows()}
    tiers = source_tier_map()
    items = []
    seen = set()
    for s in sources:
        name = s.get("name", "")
        if not name or name in seen:
            continue
        seen.add(name)
        row = stored.get(name) or {}
        items.append({
            "source_name": name,
            "weight": int(row.get("weight", 0) or 0),
            "updated_at": row.get("updated_at", ""),
            "tier": (tiers.get(name) or {}).get("tier", fetcher.source_tier(s)),
            "type": s.get("type", "other"),
            "domain": s.get("domain", ""),
            "voted": name in stored,
        })
    # 已经不在 sources.json 里、但历史上被投过票的来源也一并列出
    for name, row in stored.items():
        if name in seen:
            continue
        items.append({"source_name": name, "weight": int(row["weight"] or 0),
                      "updated_at": row["updated_at"], "tier": 4, "type": "other",
                      "domain": "", "voted": True})
    items.sort(key=lambda r: (-r["weight"], r["source_name"]))
    return jsonify({
        "ok": True,
        "count": len(items),
        "items": items,
        "weight_range": {"min": db.WEIGHT_MIN, "max": db.WEIGHT_MAX},
        "rules": {
            "tier2_upgrade": WEIGHT_UPGRADE,
            "tier2_upgraded_to": TIER_UPGRADED,
            "tier2_downgrade": WEIGHT_DOWNGRADE,
            "decay_monthly": db.WEIGHT_DECAY_RATIO,
        },
        "decay": db.decay_state(),
        "readonly": True,
    })


@app.post("/api/news/<int:news_id>/user-score")
def api_set_user_score(news_id: int):
    """内容评分（1–5 星）+ 可选备注。点击星星即保存，系统不自动评分。"""
    data = request.get_json(silent=True) or {}
    if not db.get_news(news_id):
        return jsonify({"ok": False, "error": "新闻不存在"}), 404
    raw = data.get("score")
    if raw in (None, "", "null"):
        score = None                      # 取消评分
    else:
        try:
            score = int(raw)
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "评分必须是 1–5 的整数"}), 400
        if score < 1 or score > 5:
            return jsonify({"ok": False, "error": "评分必须是 1–5 的整数"}), 400
    note = data.get("note")
    if note is not None:
        note = str(note).strip()[:120]
    updated = db.update_user_score(news_id, score, note)
    return jsonify({"ok": True, "id": news_id, "user_score": updated["user_score"],
                    "user_note": updated["user_note"] or ""})


@app.post("/api/news/<int:news_id>/credibility")
def api_set_credibility(news_id: int):
    data = request.get_json(silent=True) or {}
    label = (data.get("credibility") or "").strip()
    if label not in CREDIBILITY_LEVELS:
        return jsonify({"ok": False, "error": "可信度标签必须是四级之一",
                        "allowed": CREDIBILITY_LEVELS}), 400
    if not db.get_news(news_id):
        return jsonify({"ok": False, "error": "新闻不存在"}), 404
    db.update_credibility(news_id, label)
    return jsonify({"ok": True, "id": news_id, "credibility": label})


@app.post("/api/news/<int:news_id>/translation")
def api_set_translation(news_id: int):
    data = request.get_json(silent=True) or {}
    item = db.get_news(news_id)
    if not item:
        return jsonify({"ok": False, "error": "新闻不存在"}), 404
    lang = (data.get("lang") or "").strip().lower()
    if lang not in ("zh", "en"):
        lang = "en" if (item.get("source_lang") or "zh") == "zh" else "zh"
    title = (data.get("title") or "").strip()
    summary = (data.get("summary") or "").strip()
    if not title and not summary:
        return jsonify({"ok": False, "error": "标题和摘要至少填一个"}), 400
    updated = db.update_translation(news_id, lang, title, summary)
    return jsonify({"ok": True, "id": news_id, "lang": lang,
                    "translation_status": updated["translation_status"]})


@app.post("/api/news/<int:news_id>/evaluation")
def api_set_evaluation(news_id: int):
    """回填热点评分与看点。系统绝不自动生成（spec v2 第 11 节）。"""
    data = request.get_json(silent=True) or {}
    if not db.get_news(news_id):
        return jsonify({"ok": False, "error": "新闻不存在"}), 404
    raw_score = data.get("hotspot_score")
    try:
        score = int(raw_score)
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "热点评分必须是 1–5 的整数"}), 400
    if score < 1 or score > 5:
        return jsonify({"ok": False, "error": "热点评分必须是 1–5 的整数"}), 400
    highlight = (data.get("highlight") or "").strip()
    if len(highlight) > 60:
        return jsonify({"ok": False, "error": "一句话看点不超过 30 字（最长 60 字符）"}), 400
    updated = db.update_evaluation(news_id, score, highlight)
    return jsonify({"ok": True, "id": news_id, "hotspot_score": updated["hotspot_score"],
                    "highlight": updated["highlight"], "hotspot_status": updated["hotspot_status"]})


def startup() -> dict:
    """建库/迁移 + 补 url_key + 清重复 + 每月一次的来源权重衰减（spec v2.3 第 2 节）。"""
    report = db.init_db()
    report["weight_decay"] = db.decay_source_weights()
    return report


if __name__ == "__main__":
    startup()
    app.run(host="127.0.0.1", port=5000, debug=False, threaded=True)
