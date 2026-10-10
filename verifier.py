"""多源验证 + 可信度判定。

v2.1 起可信度按 sources.json 里的 Tier 等级自动判定（结果写 credibility_auto），
人工可以在前端覆盖 credibility；系统只在入库时判定一次，之后不再自动改写。

判定规则（按顺序命中第一条）：
1. 来源冲突（同一事件里数字对不上）→ 存疑，并给出冲突点
2. 有 Tier 1 源 + 至少 1 个 Tier 2 源，且无冲突 → 已证实
3. 有 Tier 1 源 + 其他源，或 ≥2 个 Tier 2 独立源 → 较可信
4. 只有 1 个源 → 单一来源
5. 其余（≥2 个源但全是 Tier 3–4）→ 存疑

另外做两件事：
- 抽取关键实体（机构、数字、拉丁词、引号内容）作为“同一事件”的判据；
- 把同一事件的多来源合并成一条，sources 列表保留全部来源。
"""

from __future__ import annotations

import re

from dedup import normalize_title, similarity

CREDIBILITY_LEVELS = ["已证实", "较可信", "单一来源", "存疑"]

CREDIBILITY_EN = {
    "已证实": "Verified",
    "较可信": "Likely true",
    "单一来源": "Single source",
    "存疑": "Questionable",
}

# 排序权重：配额挑选时优先要可信的
CREDIBILITY_RANK = {"已证实": 4, "较可信": 3, "单一来源": 2, "存疑": 1}
# Tier 数字越小越权威，这里换算成“越大越好”的排序权重
TIER_RANK = {1: 4, 2: 3, 3: 2, 4: 1}

TIER_LABELS = {
    1: {"zh": "Tier 1 官方权威", "en": "Tier 1 Official"},
    2: {"zh": "Tier 2 主流/行业标杆", "en": "Tier 2 Mainstream"},
    3: {"zh": "Tier 3 一般媒体", "en": "Tier 3 General"},
    4: {"zh": "Tier 4 未知来源", "en": "Tier 4 Unknown"},
}

# 同一事件的判定阈值
EVENT_TITLE_SIM = 0.88       # 标题几乎逐字相同 → 同一事件
EVENT_CORE_SIM = 0.72        # 去掉数字后的标题高度相似 → 同一事件
EVENT_TITLE_SIM_SOFT = 0.45  # 标题弱相似，但实体重叠足够 → 同一事件
EVENT_SHARED_ENTITIES = 2
EVENT_LCS_MIN = 6            # 去数字后最长公共子串 >= 6 字 → 同一事件
EVENT_LCS_RATIO = 0.5        # 且该子串至少覆盖较短标题的一半

_ORG_SUFFIX = (
    "部|局|委|院|署|厅|办|公司|集团|银行|大学|学院|研究院|研究所|委员会|政府|"
    "警方|公安|法院|检察院|中心|协会|组织|机构|实验室|基金会|平台|团队"
)
_ORG = re.compile(r"[\u4e00-\u9fa5A-Za-z]{2,8}?(?:" + _ORG_SUFFIX + r")")
_NUM = re.compile(r"\d+(?:\.\d+)?")
_DIGITS = re.compile(r"\d+(?:\.\d+)?")
_LATIN = re.compile(r"\b[A-Za-z][A-Za-z0-9\-.]{2,24}\b")
_QUOTED = re.compile(r"[“\"']([^”\"']{2,24})[”\"']")

_LATIN_STOP = {"the", "and", "for", "with", "from", "that", "this", "new", "how", "why",
               "what", "you", "your", "are", "was", "were", "will", "can", "not", "but",
               "com", "www", "http", "https", "html", "cn", "news"}


def extract_entities(text: str) -> dict:
    """规则抽取关键实体。MVP 用规则，够稳也够快。"""
    text = text or ""
    return {
        "orgs": set(_ORG.findall(text)),
        "nums": {str(_canon_number(n)) for n in _NUM.findall(text)},
        "latin": {w.lower() for w in _LATIN.findall(text) if w.lower() not in _LATIN_STOP},
        "quoted": set(_QUOTED.findall(text)),
    }


def _canon_number(raw: str) -> str:
    """数字归一：去掉尾部无意义 0，便于 3.0 与 3 互认。"""
    try:
        f = float(raw)
    except (TypeError, ValueError):
        return raw
    if f == int(f):
        return str(int(f))
    return str(f)


def _event_core(title: str) -> str:
    """事件核心：规范化标题后去掉数字。

    数字差异（12 万 / 30 万）不改变“这是同一件事”，却正是冲突检测要抓的东西。
    """
    return _DIGITS.sub("", normalize_title(title or ""))


def longest_common_substring_len(a: str, b: str) -> int:
    """最长公共子串长度（中文标题的事件指纹）。标题很短，DP 足够。"""
    if not a or not b:
        return 0
    prev = [0] * (len(b) + 1)
    best = 0
    for i in range(1, len(a) + 1):
        cur = [0] * (len(b) + 1)
        ai = a[i - 1]
        for j in range(1, len(b) + 1):
            if ai == b[j - 1]:
                cur[j] = prev[j - 1] + 1
                if cur[j] > best:
                    best = cur[j]
        prev = cur
    return best


def entity_tokens(item: dict) -> set:
    """把一条新闻的实体摊平成 token 集合，供重叠比对。"""
    ent = item.get("entities") or extract_entities(
        f"{item.get('title', '')} {item.get('summary', '')}"
    )
    item["entities"] = ent
    tokens = set()
    tokens |= {("org", o) for o in ent["orgs"]}
    tokens |= {("num", n) for n in ent["nums"]}
    tokens |= {("latin", w) for w in ent["latin"]}
    tokens |= {("quote", q) for q in ent["quoted"]}
    return tokens


def same_event(a: dict, b: dict) -> bool:
    """两条候选是否属于同一事件（跨领域不合并）。

    判据按强度递减：去数字后的公共子串 → 核心标题相似度 → 逐字近似 → 实体重叠。
    """
    if a.get("domain") != b.get("domain"):
        return False
    ta, tb = normalize_title(a.get("title", "")), normalize_title(b.get("title", ""))
    ca, cb = _event_core(a.get("title", "")), _event_core(b.get("title", ""))
    shared = entity_tokens(a) & entity_tokens(b)

    lcs = longest_common_substring_len(ca, cb)
    shortest = min(len(ca), len(cb))
    if lcs >= EVENT_LCS_MIN and shortest and lcs / shortest >= EVENT_LCS_RATIO:
        return True
    if similarity(ca, cb) >= EVENT_CORE_SIM:
        return True
    if similarity(ta, tb) >= EVENT_TITLE_SIM:
        return True
    if len(shared) >= EVENT_SHARED_ENTITIES and similarity(ca, cb) >= EVENT_TITLE_SIM_SOFT:
        return True
    return False


def merge_events(candidates: list) -> list:
    """贪心聚类：同一事件合并为一条，sources 收全，代表条取最优 Tier 的那条。"""
    groups = []
    for item in candidates:
        for g in groups:
            if same_event(g["_rep"], item):
                g["members"].append(item)
                if TIER_RANK.get(item.get("tier"), 1) > TIER_RANK.get(g["_rep"].get("tier"), 1):
                    g["_rep"] = item
                break
        else:
            groups.append({"members": [item], "_rep": item})

    out = []
    for g in groups:
        members = g["members"]
        rep = g["_rep"]
        sources, tiers, types, urls = [], [], [], []
        for m in members:
            if m["source"] not in sources:
                sources.append(m["source"])
                tiers.append(int(m.get("tier") or 4))
                types.append(m.get("type") or "other")
            urls.append(m["url"])
        published = sorted([m.get("published_at") or m.get("published") for m in members
                            if (m.get("published_at") or m.get("published"))])
        # 发布时间出处：优先 rss，其次 webpage，最后 unknown
        prov = [m.get("published_at_source") for m in members if m.get("published_at_source")]
        prov_rank = {"rss": 3, "webpage": 2, "unknown": 1}
        best_prov = max(prov, key=lambda p: prov_rank.get(p, 0)) if prov else "unknown"
        best_summary = max((m.get("summary") or "" for m in members), key=len, default="")
        langs = [m.get("source_lang") for m in members if m.get("source_lang")]
        best_tier = min(tiers) if tiers else 4
        out.append({
            "title": rep["title"],
            "url": rep["url"],
            "urls": urls,
            "summary": best_summary,
            "sources": sources,
            "tiers": tiers,
            "source_tiers": dict(zip(sources, tiers)),
            "tier": best_tier,
            "type": types[sources.index(rep["source"])] if rep["source"] in sources else "other",
            "domain": rep.get("domain", ""),
            "published": published[-1] if published else None,
            "published_at": published[-1] if published else None,
            "published_at_source": best_prov if published else "unknown",
            "source_lang": rep.get("source_lang") or (langs[0] if langs else "zh"),
            "members": members,
        })
    return out


def conflict_points(group: dict) -> list:
    """找出同一事件里互相矛盾的数字，返回人类可读的冲突点。

    例：来源 A 说“新增就业 12 万人”，来源 B 说“30 万人” → ['12 × 30']。
    """
    members = group.get("members") or []
    if len(members) < 2:
        return []
    points = []
    for i in range(len(members)):
        for j in range(i + 1, len(members)):
            a, b = members[i], members[j]
            na = (a.get("entities") or extract_entities(a.get("title", "")))["nums"]
            nb = (b.get("entities") or extract_entities(b.get("title", "")))["nums"]
            if not na or not nb or (na & nb):
                continue
            shared_text = longest_common_substring_len(_event_core(a.get("title", "")),
                                                       _event_core(b.get("title", "")))
            if shared_text < EVENT_LCS_MIN:
                continue
            left = "/".join(sorted(na)[:2])
            right = "/".join(sorted(nb)[:2])
            points.append(f"{a.get('source', '?')} 说 {left} … {b.get('source', '?')} 说 {right}")
    return points[:3]


def tier_pairs(group: dict) -> list:
    """把 group 的来源与 Tier 对齐成 [(来源, Tier)]，同一来源只记一次。"""
    sources = [s for s in (group.get("sources") or []) if s]
    tiers = [int(t or 4) for t in (group.get("tiers") or [])]
    pairs = []
    for i, name in enumerate(sources):
        pairs.append((name, tiers[i] if i < len(tiers) else 4))
    return pairs


def credibility_basis(group: dict) -> str:
    """判定依据，直接显示在卡片上：`中国新闻网 / Tier 3，澎湃新闻 / Tier 2`。"""
    return "，".join(f"{name} / Tier {tier}" for name, tier in tier_pairs(group))


def judge_credibility(group: dict) -> str:
    """按来源 Tier 自动判定可信度（spec v2.2 第 3 节）。

    规则（严格按 Tier 与来源数，不看别的）：
    1. 只有 1 个源 → 单一来源（不管它是 Tier 几）
    2. 来源之间有冲突 → 存疑
    3. 出现 Tier 3 或 Tier 4 的源 → 存疑
    4. 全是 Tier 1 / Tier 2：同时有 T1 与 T2 → 已证实；只有 T2 → 较可信；只有 T1 → 已证实
    """
    pairs = tier_pairs(group)
    if not pairs:
        return "单一来源"
    if len(pairs) == 1:
        return "单一来源"
    if conflict_points(group):
        return "存疑"
    tiers = [tier for _name, tier in pairs]
    if any(tier >= 3 for tier in tiers):
        return "存疑"          # 混进了 Tier 3–4 的源
    if 1 in tiers and 2 in tiers:
        return "已证实"
    if 1 in tiers:
        return "已证实"        # 两个及以上 Tier 1 官方源，事实一致（spec 未列，按更强处理）
    if 2 in tiers:
        return "较可信"        # 只有 Tier 2
    return "存疑"
