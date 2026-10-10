# 每日新闻聚合器 — 使用与实现说明 / Daily News Aggregator — Guide

按项目规格文档 [`spec.md`](spec.md) 实现（含 v2 / v2.1 / v2.2 变更章节）。
技术栈写死不变：Python 3.10+ / Flask / SQLite / 纯 HTML+CSS+JS / requests + BeautifulSoup + feedparser /
双击 .bat 启动 / **不调用任何 LLM API**。

界面只有一个筛选维度：**模块** = 综合 | AI | 民生 | 社会国内 | 社会国外（没有单独的“领域”下拉）。
每个模块有自己的刷新按钮（标签上的 ⟳）、每页 10 条、独立分页；顶部有 今日 / 历史 与 中 / EN。
可信度严格按来源 **Tier 1–4** 自动判定，并在卡片上写出判定依据（如 `单一来源（中国新闻网 / Tier 3）`）。
内容评分 = 你手动点的 **1–5 星**；热点评分 = 大模型评估后你回填的 1–5 分。

> **English summary** — A local five-module news aggregator (spec v2 + v2.1 + v2.2).
> Only one filter dimension exists: **module** (All / AI / Livelihood / Society-China / Society-World); there is no
> separate "domain" dropdown. Each module has its own refresh button (⟳ on the tab), 10 items per page and its own
> pagination; the All module never fetches — it mixes the four domain modules by quota (AI 3 / Livelihood 3 /
> Society-China 2 / Society-World 2). Credibility is judged strictly from each source's **tier 1–4** and the card
> prints the basis, e.g. `Single source (China News / Tier 3)`. Two independent rating systems: **your 1–5 star
> content score** (`user_score`, clicked on the card) and the **LLM-assessed hotspot score** you paste back.
> History de-duplicates the same story across modules (one row per story, domain module wins), the containers are
> cleared before every render, and each view issues exactly one request.
> Run: `pip install -r requirements.txt`, double-click `start.bat`, open <http://localhost:5000>.
> Checks: `python selftest.py` (205 assertions) and `node frontend_check.js` (13 checks).

---

## 1. 快速开始 / Quick start

```bat
pip install --upgrade pip
pip install -r requirements.txt
```

**双击 `start.bat`** → 浏览器打开 <http://localhost:5000>。首屏为空列表，点某个模块的 ⟳ 或「刷新」抓一批即可。

要清空数据库（出现脏数据时）：**关掉服务窗口 → 删除 `news.db` → 重新双击 `start.bat`**。
程序启动时也会自动补 `url_key` 并清理历史重复行，所以正常情况不需要手动删库。

## 2. 目录结构

```
news-daily/
├── app.py               # Flask 入口 + 刷新引擎 + 全部 API
├── db.py                # SQLite：数据模型、索引、旧库(v1)迁移、重复数据清理、历史筛选分页
├── fetcher.py           # RSS 优先 / 网页兜底 / 并发 / 停更守卫 / 发布时间与出处 / Tier 取值
├── dedup.py             # URL 规范化（去 utm、统一 http/https、忽略 www）+ 标题相似度
├── verifier.py          # 实体抽取、同事件合并、Tier 可信度判定、冲突点
├── prompt_builder.py    # 三种提示词：口播（中文）、热点评价、翻译
├── sources.json         # 新闻源（每条带 tier / type）+ 模块关键词门
├── templates/index.html # 唯一页面
├── static/app.js        # 引导层：i18n、请求、复制、toast、视图切换（不碰视图容器）
├── static/today.js      # 今日视图（模块标签、卡片、评分、译文/评价回填）
├── static/history.js    # 历史视图（筛选、分页、详情，只读）
├── static/style.css     # 样式（固定卡片高度、3 行摘要、2–3 列网格）
├── start.bat            # 双击启动（ASCII-only，见第 9 节）
├── selftest.py          # 后端离线自检：192 项断言
├── frontend_check.js    # 前端离线自检：DOM 桩跑两个视图（node 运行）
├── probe_sources.py     # 新闻源健康检查
├── requirements.txt
├── news.db              # 运行时生成（自动迁移、自动清理重复）
├── README.md
└── spec.md
```

## 3. 来源 Tier 与可信度自动判定

每条源在 `sources.json` 里带 `tier`（1–4）与 `type`（official / mainstream / international / industry / other）：

| Tier | 含义 | 例子 |
|---|---|---|
| 1 | 官方权威，默认可信度极高 | 教育部、央视网、新华网、人民网、中国政府网、联合国新闻 |
| 2 | 主流 / 行业标杆，有采编规范 | 澎湃新闻、量子位、机器之心、MIT Tech Review、TechCrunch、ABC / NPR / France 24 / TASS / BBC / NYT / Guardian |
| 3 | 一般媒体 / 商业媒体，可能有商业立场 | 中国新闻网各频道、界面新闻、新京报、中国新闻周刊、IT之家、爱范儿、少数派、Solidot、VentureBeat |
| 4 | 未登记来源，默认最低 | 任何没写 `tier` 的源 |

判定规则（严格按来源数与 Tier，命中第一条即定）：

| 条件 | 标签 |
|---|---|
| 只有 1 个源（**不管 Tier 几**） | 单一来源 |
| 来源之间有冲突（同事件数字对不上） | 存疑 |
| **出现 Tier 3 或 Tier 4 的源** | 存疑 |
| 全是 T1 / T2，且同时有 T1 与 T2 | 已证实 |
| 全是 T1 / T2，只有 T2 | 较可信 |
| 全是 T1（两个及以上官方源，一致） | 已证实 |

**卡片必须写出判定依据**：`单一来源（中国新闻网 / Tier 3）`、`较可信（NPR World / Tier 2，TASS / Tier 2）`。
系统判定存 `credibility_auto`，展示值 `credibility` 默认等于它，人工点击修改后卡片出现「人工」小标，
hover 仍能看到自动判定值；冲突点写进 `conflict_note`（形如 `澎湃新闻 说 12 … 记者站 说 30`）。

调整分级：觉得某源靠谱就把 `tier` 从 3 改成 2，常发假新闻就改成 4，重启服务即生效。

实测：真实抓取里 `society_intl` 的一条被 NPR 与 TASS（都是 Tier 2）同时报道 → 自动判「较可信」。**注意规则是保守的**：
只要事件里混进一个 Tier 3 的源就降级为「存疑」，这样标签不会高估来源质量；如果你更想要「按最强的一对来源判定」，
改 `verifier.judge_credibility` 一处即可。

## 4. 模块与刷新逻辑

| 模块 | 来源 | 每页 | 分配规则 |
|---|---|---|---|
| 综合 comprehensive | 不抓取，混合四个单独模块 | 10 | AI 3 / 民生 3 / 社会国内 2 / 社会国外 2；民生与社会议题优先热点，AI 不强制 |
| AI / 民生 / 社会国内 / 社会国外 | 只抓本领域源 | 10 | 5 条高热点（评分 ≥ 3）+ 5 条普通 |

单独模块：`只抓本领域源 → 关键词门 → 模块内去重 → 多源验证 → Tier 可信度 → 热点对半 → 时窗 24h→48h→72h → 不跨领域补位`。
综合模块：`依次触发四个模块刷新 → 各取配额 → 混合入库`；某模块不足时该位置空着并标「今日不足」。
每个模块标签都带自己的 ⟳ 刷新按钮；顶部的「刷新」按钮刷当前模块。刷新是异步的（POST 立即返回，前端轮询进度），
同一时间只允许一个刷新任务。

## 5. 数据模型（news 表）

| 字段 | 说明 |
|---|---|
| module / domain | 模块（comprehensive/ai/livelihood/society_cn/society_intl）与来源领域 |
| title / title_zh / title_en | 原始标题 + 中英标题 |
| summary / summary_zh / summary_en | 原始摘要 + 中英摘要 |
| url / **url_key** | 原链接 + 归一化比较键（去 utm、统一 http/https、忽略 www），`(url_key, module)` 唯一 |
| source_lang / translation_status | 原文语言；已翻译 / 未翻译 / 部分（只在回填译文时重算） |
| credibility / credibility_auto / **credibility_basis** / conflict_note | 展示值 / 系统判定值 / **判定依据** / 冲突点 |
| oral_prompt | 中文口播提示词（入库时生成） |
| hotspot_score / highlight / hotspot_status | 热点评分 1–5 / 一句话看点 / 已生成•未生成（**由你把大模型结果回填**） |
| **user_score / user_note** | **你的内容评分 1–5 星 + 可选备注（手动点击，系统绝不自动打分）** |
| published_at / published_at_source | 新闻发布时间 + 出处（rss / webpage / unknown） |
| shown_at / module_shown_at / created_at | 首次展示 / 该模块展示 / 入库时间 |

索引：url、url_key、shown_at、domain、credibility、module、published_at、`(url_key, module)` 唯一。库里**不存新闻全文**。

## 6. 两套评分 + 三种人工回填

| 操作 | 入口 | 结果 |
|---|---|---|
| **内容评分（你的主观认可度）** | **卡片 5 颗星，点击即存** | 写 `user_score` / `user_note` |
| 热点评分（客观热点程度） | 「复制评价提示词」→ 大模型 → 「填入评价」 | 写 `hotspot_score` / `highlight` |
| 口播提示词 | 卡片「复制口播提示词」 | 复制到大模型产出 100–150 字口播 |
| 翻译 | 「复制翻译提示词」→ 「填入译文」 | 写 `title_en/summary_en`（英文源写回中文侧） |

两套评分互不干扰：`user_score` 是你打的分，`hotspot_score` 是模型评估后回填的分。
评分接口：`POST /api/news/<id>/user-score`，body `{"score": 1..5, "note": "可选"}`；`score: null` 清除评分，非法值 400。
历史页与今日页都能按评分筛（`min_user_score=4` = 只看 4 星以上；`user_score=4` = 精确匹配）。

## 7. API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/` | 页面 |
| GET | `/api/meta` | 模块清单、配额、四级标签、Tier 映射、来源分级表、时窗档位（前端只取一次） |
| GET | `/api/news?module=&credibility=&sort=&min_hotspot=&min_user_score=&lang=&page=&page_size=` | 今日新闻（每页 10 条） |
| POST | `/api/refresh?module=` | 触发刷新（异步） |
| GET | `/api/refresh/status` | 各模块刷新状态与进度 |
| GET | `/api/history?date=&module=&credibility=&q=&user_score=&min_user_score=&dedup=&page=1&page_size=20&lang=` | 历史列表（只读，默认跨模块去重） |
| GET | `/api/history/<id>?lang=` | 历史详情 |
| POST | `/api/news/<id>/credibility` | 人工覆盖可信度 |
| POST | `/api/news/<id>/translation` | 提交译文 |
| POST | `/api/news/<id>/evaluation` | 提交热点评分与看点 |
| POST | `/api/news/<id>/user-score` | 提交我的内容评分与备注 |

`q` 只搜标题与摘要（六个字段），**不做全文搜索**；`dedup=0` 可关闭历史去重（用于排查）；
`min_hotspot=3` 只看高热点；`min_user_score=4` 只看 4 星以上。`domain` 参数服务端仍接受，界面不再使用。

## 8. 前端结构

- 三个文件各管一摊：`app.js` 引导与纯工具（i18n / 请求 / `/api/meta` 缓存 / 复制 / toast / 视图切换），
  `today.js` 只碰 `#today-view` 的 DOM，`history.js` 只碰 `#history-view` 的 DOM，**没有共用的渲染函数**。
- 所有 DOM 取值走 `el(id)` + `if (node)` 守卫，初始化统一包在 `DOMContentLoaded` 里。
- 每个视图渲染前 `container.innerHTML = ''`；事件只绑定一次；**同一时刻只发一个请求**（in-flight 守卫），
  `/api/meta` 两个视图共用一次请求。
- 卡片固定高度 `--card-h: 320px`，标题 2 行、摘要 3 行截断；网格一行 2 条，宽屏 3 条，窄屏 1 条。
- 模块标签栏里**只有模块**（标签 + 各自的 ⟳）；「搜索 / 清空筛选」在右上角工具栏（`#history-tools`）。
- 日期控件 `<input type="date">` 带深色配色与「今天」快捷键。

## 9. 实测记录（本机 2026-10-10）

- `python selftest.py` → **205 项断言全过**；`node frontend_check.js` → **13 项全过**。
- 按你的要求清空 `news.db`、重启、重新抓取：9 秒完成，五个模块各入库 10 条。
- **历史页去重**：关闭去重 50 行 → 默认 40 行，标题重复 0 条，保留的确实是领域模块那一行。
- **判定依据**实测：`单一来源（IT之家 / Tier 3）`、`较可信（NPR World / Tier 2，TASS / Tier 2）`。
- **评分持久化**：点 5 星 + 备注 → 重新请求仍在；`min_user_score=4`、`user_score=5` 筛选均正确。
- 前端自检覆盖你报过的两类问题：元素全为 null 时不抛错、卡片数与数据条数一致且渲染前容器被清空；
  另外断言「启动只请求一次 /api/news 和 /api/meta」「切到历史页只请求一次 /api/history」。

## 10. 和 spec 的偏差（都写明了原因）

1. **`start.bat` 没有照抄 spec 第 10 节三行**：`python app.py` 会阻塞，后面的 `start http://...` 永远执行不到。
   现在先起一个延时 2 秒的辅助窗口开浏览器，再启动 Flask；整个文件只用 ASCII（cmd 读 .bat 用当前代码页）。
2. **新增文件**：`requirements.txt`、`selftest.py`、`frontend_check.js`、`probe_sources.py`、`README.md`，
   以及库里一个 `meta` 表。
3. **提示词里的摘要裁剪到 180 字**；库里仍存完整摘要。
4. **AI 模块关键词门**（`module_filters.ai`）：只按**标题**命中关键词收条（中文子串、纯英文整词），
   避免科技综合源把显示器、键盘、汽车这类杂讯塞进 AI 模块；被挡条数写进刷新消息（实测一次挡 68 条）。
5. **综合模块连续刷新会继续消化同一批次的剩余条目**，直到批次取尽（模块内永久去重决定）。
6. **可信度规则的两处补全**：全是 Tier 1（两个及以上官方源、无冲突）判「已证实」；
   含 Tier 3/4 的多源事件判「存疑」是 spec 的保守规则，若想改成「按最强的一对来源判定」，改
   `verifier.judge_credibility` 一处即可。

## 11. 已知限制 / Known limits

- **跨语言不合并**：中英文源报道同一件事不会并成一条（字符相似度判定不了跨语言），会各算一条；
  翻译提示词就是为这一步准备的。
- **高热点配额要靠回填**：新条目没有评分，所以「5 条高热点」在首次刷新时必然是 0/5（spec 禁止自动评分）。
- **历史里同一条新闻可能出现在多个模块**：这是 `(url, module)` 联合唯一的结果。历史页默认按 `url_key` 去重只显示一行
  （优先领域模块那一行）；若想看到全部模块行，加 `dedup=0`。
- **网页抓来的条目可能没有摘要或发布时间**：已做原文补全（meta、`<time>`、链接日期），个别站点仍补不到。
- **开发服务器**只监听 127.0.0.1；spec 不让上 Docker/CI，所以不引 waitress/gunicorn。
- 启动时 requests 可能打印一条 urllib3/chardet 版本告警，是环境里第三方库版本不匹配，不影响功能。
