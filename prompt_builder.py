"""提示词生成：口播（中文一份）、热点评价、翻译。

三种提示词都只是文本模板，系统不调用任何 LLM API（spec v2 第 11 节）：
用户复制出去，在网页版大模型里生成，再用「填入译文 / 填入评价」把结果回填。
"""

from __future__ import annotations

from verifier import CREDIBILITY_EN

# spec v2 第 12 节：口播提示词模板不变，只保留中文一份
ORAL_TEMPLATE = """你是一名新闻视频口播文案撰稿人。请根据以下新闻信息，生成一段 100–150 字的口播文案。

要求：
1. 结构固定：事件 + 来源 + 可信度 + 一句点评
2. 风格：口语化、客观、适合视频口播
3. 不要用“据悉”“业内人士称”等模糊表述
4. 结尾加一句简短点评，不煽动情绪
5. 直接输出口播文案，不要加标题，不要解释

新闻信息：
- 标题：{title}
- 摘要：{summary}
- 来源：{sources}
- 可信度标签：{credibility}
- 原始链接：{url}
"""

# spec v2 第 5 节：热点评价提示词
HOTSPOT_TEMPLATE = """你是一名新闻编辑。请判断以下新闻的热点程度，并给出简短评价。

要求：
1. 热点评分：1–5 分，5 分最高（涉及公共卫生事件、社会冲突、政策争议、事故灾难、科技突破等）
2. 一句话看点：说明这条新闻为什么值得关注，不超过 30 字
3. 直接输出，格式：
   热点评分：X
   看点：...

新闻信息：
- 标题：{title_zh}
- 摘要：{summary_zh}
- 来源：{sources}
- 发布时间：{published_at}
"""

# spec v2 第 6 节：翻译提示词
TRANSLATION_TEMPLATE = """请将以下新闻翻译成英文（如果原文是英文，则翻译成中文）。保持新闻语气，不要添加评论。

标题：{title}
摘要：{summary}

输出格式：
标题：...
摘要：...
"""

# 禁止出现在口播里的模糊表述
VAGUE_PHRASES = ["据悉", "业内人士称", "据了解", "有消息称", "相关人士表示"]

# 提示词里的摘要裁剪长度：RSS 有的源直接给全文，不裁剪会把提示词撑得很长
SUMMARY_CLIP = 180


def clip(text: str, limit: int = SUMMARY_CLIP) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def _sources_text(item: dict) -> str:
    sources = item.get("sources") or []
    if isinstance(sources, str):
        return sources or "未标注"
    return "、".join(sources) if sources else "未标注"


def build_prompt(item: dict) -> str:
    """口播提示词（中文一份），存进 oral_prompt 字段。"""
    return ORAL_TEMPLATE.format(
        title=(item.get("title_zh") or item.get("title") or "").strip(),
        summary=clip(item.get("summary_zh") or item.get("summary") or "暂无摘要"),
        sources=_sources_text(item),
        credibility=(item.get("credibility") or "单一来源").strip(),
        url=(item.get("url") or "").strip(),
    )


def build_hotspot_prompt(item: dict) -> str:
    """热点评价提示词。评分与看点必须由用户回填，系统不自动生成。"""
    return HOTSPOT_TEMPLATE.format(
        title_zh=(item.get("title_zh") or item.get("title") or "").strip(),
        summary_zh=clip(item.get("summary_zh") or item.get("summary") or "暂无摘要"),
        sources=_sources_text(item),
        published_at=(item.get("published_at") or "发布时间未知").strip(),
    )


def build_translation_prompt(item: dict) -> str:
    """翻译提示词：原文是中文就译英，原文是英文就译中。"""
    zh_first = (item.get("source_lang") or "zh") == "zh"
    title = (item.get("title_zh") or item.get("title") or "").strip() if zh_first \
        else (item.get("title_en") or item.get("title") or "").strip()
    summary = (item.get("summary_zh") or item.get("summary") or "").strip() if zh_first \
        else (item.get("summary_en") or item.get("summary") or "").strip()
    return TRANSLATION_TEMPLATE.format(title=title, summary=clip(summary))


def credibility_en(label: str) -> str:
    return CREDIBILITY_EN.get(label, "")
