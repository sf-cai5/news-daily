/* 前端离线自检（node frontend_check.js）：用最小 DOM 桩跑 today.js / history.js。
   用法：node frontend_check.js [static目录]   （默认 static）
   覆盖：
   1) "Cannot set properties of null (setting 'textContent')" —— 元素取不到时不应抛错；
   2) 新闻卡片成对重复 —— 渲染出的卡片数必须等于数据条数，且渲染前容器被清空；
   3) spec v2.3 —— 卡片带 👍/👎、已隐藏条目不进今日页、历史页带「已隐藏」标记与「取消隐藏」入口。 */
'use strict';
const fs = require('fs');
const path = require('path');

const dir = process.argv[2] || 'static';
const files = ['app.js', 'today.js', 'history.js'];

function makeElement(id) {
  const kids = [];
  const el = {
    id, value: '', textContent: '', innerHTML: '', className: '', title: '',
    placeholder: '', dataset: {},
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
    style: {},
    attributes: {},
    addEventListener(type, fn) { (this._h = this._h || {})[type] = fn; },
    removeEventListener() {},
    setAttribute(k, v) { this.attributes[k] = v; },
    getAttribute(k) { return k === 'data-id' ? this.attributes['data-id'] : (this.attributes[k] || null); },
    appendChild(c) { kids.push(c); return c; },
    removeChild(c) { return c; },
    select() {}, focus() {}, blur() {}, click() {},
    closest() { return null; },
    matches() { return false; },
    querySelector() { return null; },
    querySelectorAll() { return []; }
  };
  Object.defineProperty(el, '_kids', { value: kids });
  return el;
}

function harness(mode) {
  const handlers = {};
  const elements = {};
  const registry = {};

  global.window = global;
  global.localStorage = { getItem: () => null, setItem() {} };
  try {
    Object.defineProperty(global, 'navigator', { value: { clipboard: null }, configurable: true });
  } catch (e) { /* Node 已有只读 navigator */ }
  global.document = {
    documentElement: { lang: '' },
    body: makeElement('body'),
    getElementById(id) {
      if (mode === 'null') { return null; }
      if (!elements[id]) { elements[id] = makeElement(id); }
      return elements[id];
    },
    querySelectorAll() { return []; },
    createElement() { return makeElement('new'); },
    addEventListener(ev, fn) { (handlers[ev] = handlers[ev] || []).push(fn); }
  };

  const items = [
    { id: 1, module: 'ai', domain: 'ai', credibility: '较可信', credibility_auto: '较可信',
      credibility_manual: false, title: 'A', title_zh: 'A', display_title: 'A', summary: 's',
      display_summary: 's', sources: ['量子位', 'IT之家'], url: 'https://a.example.com/1',
      source_lang: 'zh', translation_status: '未翻译', hotspot_status: '未生成', hotspot_score: null,
      user_score: null, user_note: '', published_at: '2026-10-10T01:00:00', published_unknown: false,
      module_shown_at: '2026-10-10T02:00:00', oral_prompt: 'p1', hotspot_prompt: 'p2',
      translation_prompt: 'p3', highlight: '', conflict_note: '',
      user_vote: 'none', hidden: false, hidden_at: '', source_weights: { '量子位': 3, 'IT之家': -2 } },
    { id: 2, module: 'ai', domain: 'ai', credibility: '单一来源', credibility_auto: '单一来源',
      credibility_manual: false, title: 'B', title_zh: 'B', display_title: 'B', summary: 's2',
      display_summary: 's2', sources: ['IT之家'], url: 'https://a.example.com/2',
      source_lang: 'en', translation_status: '未翻译', hotspot_status: '已生成', hotspot_score: 4,
      user_score: 3, user_note: 'n', published_at: null, published_unknown: true,
      module_shown_at: '2026-10-10T02:00:00', oral_prompt: 'p1', hotspot_prompt: 'p2',
      translation_prompt: 'p3', highlight: '看点', conflict_note: '',
      user_vote: 'up', hidden: false, hidden_at: '', source_weights: { 'IT之家': -2 } },
    // 被点踩隐藏的条目：今日页不渲染，历史页要带「已隐藏」标记与「取消隐藏」按钮
    { id: 3, module: 'livelihood', domain: 'livelihood', credibility: '存疑', credibility_auto: '存疑',
      credibility_manual: false, title: 'HIDDEN_ITEM', title_zh: 'HIDDEN_ITEM', display_title: 'HIDDEN_ITEM',
      summary: 's3', display_summary: 's3', sources: ['某源'], url: 'https://a.example.com/3',
      source_lang: 'zh', translation_status: '未翻译', hotspot_status: '未生成', hotspot_score: null,
      user_score: null, user_note: '', published_at: '2026-10-10T03:00:00', published_unknown: false,
      module_shown_at: '2026-10-10T03:00:00', oral_prompt: 'p1', hotspot_prompt: 'p2',
      translation_prompt: 'p3', highlight: '', conflict_note: '',
      user_vote: 'down', hidden: true, hidden_at: '2026-10-10T04:00:00', source_weights: { '某源': -1 } }
  ];
  const meta = {
    ok: true, modules: [{ key: 'comprehensive', label: { zh: '综合', en: 'All' } },
                        { key: 'ai', label: { zh: 'AI', en: 'AI' } }],
    module_labels: { ai: { zh: 'AI', en: 'AI' }, comprehensive: { zh: '综合', en: 'All' } },
    credibility_levels: ['已证实', '较可信', '单一来源', '存疑'],
    credibility_en: { '已证实': 'Verified' },
    source_tiers: { '量子位': { tier: 2 }, 'IT之家': { tier: 3 } },
    source_weights: { '量子位': 3, 'IT之家': -2 },
    display_limit: 10, history_page_size: 20, module_quota: { hot: 5, normal: 5, hot_threshold: 3 },
    comprehensive_quota: { ai: 3 }, windows: [24, 48, 72],
    vote_values: ['up', 'down', 'none'], weight_range: { min: -10, max: 10 }
  };
  const news = {
    ok: true, module: 'ai', module_label: { zh: 'AI', en: 'AI' }, lang: 'zh', sort: 'published',
    page: 1, pages: 1, page_size: 10, total: items.length, count: items.length, items,
    counts: { total: 2, hot: 1, normal: 0, unscored: 1, by_domain: { ai: 2 } },
    quota: { hot: 5, normal: 5, hot_threshold: 3 }, shortage: false, db_total: 2,
    credibility_levels: meta.credibility_levels, credibility_en: meta.credibility_en,
    domain_labels: meta.module_labels, last_refresh: { ok: true, message: '新增 2 条，跳过重复 0 条' },
    refresh_state: { status: 'idle' }
  };
  const history = Object.assign({}, news, { readonly: true, page: 1, pages: 2, total: 21 });

  const calls = { meta: 0, news: 0, history: 0, other: 0, lastHistoryUrl: '' };
  global.fetch = (url) => {
    let body = news;
    const u = String(url);
    if (u.indexOf('/api/meta') === 0) { body = meta; calls.meta += 1; }
    else if (u.indexOf('/api/history') === 0) {
      body = history; calls.history += 1; calls.lastHistoryUrl = u;
    }
    else if (u.indexOf('/api/news') === 0) { body = news; calls.news += 1; }
    else { calls.other += 1; }
    return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(body) });
  };

  files.forEach((f) => {
    const code = fs.readFileSync(path.join(dir, f), 'utf8');
    // 用 eval 在全局作用域执行，模拟浏览器加载顺序
    (0, eval)(code);
  });

  (handlers['DOMContentLoaded'] || []).forEach((fn) => fn());
  return { elements, calls };
}

function countCards(html) {
  // 注意：历史页的已隐藏卡片是 <article class="card is-hidden">，所以不加收尾引号
  return (String(html).match(/<article class="card/g) || []).length;
}

let failures = 0;
function check(name, cond, detail) {
  console.log((cond ? '  [OK]   ' : '  [FAIL] ') + name + (detail ? '  ' + detail : ''));
  if (!cond) { failures += 1; }
}

// ---- 场景 1：所有元素都取不到 ----
try {
  harness('null');
  check('元素全为 null 时初始化不抛错', true);
  const h = harness('null');
  check('null 场景下可重复执行而不抛错', true);
} catch (err) {
  check('元素全为 null 时初始化不抛错', false, err && err.message);
}

// 等上面几个实例的 Promise 全部结算完，再测渲染与请求次数（避免计数被上一轮污染）
setTimeout(runRenderChecks, 80);

function runRenderChecks() {
  try {
    const { elements, calls } = harness('full');
    setTimeout(() => {
      const cards = elements['cards'];
      const hcards = elements['hcards'];
      const todayHtml = cards ? String(cards.innerHTML) : '';
      check('今日视图只渲染未隐藏的条目（3 条数据 → 2 张卡）',
        cards && countCards(todayHtml) === 2, cards ? countCards(todayHtml) + ' 张' : '无容器');
      check('渲染前容器被清空（无叠加）',
        cards && todayHtml.split('<article').length === 3);
      check('已隐藏条目不出现在今日页',
        cards && todayHtml.indexOf('HIDDEN_ITEM') < 0);
      check('卡片含 5 星评分控件',
        cards && (todayHtml.match(/data-role="star"/g) || []).length === 10);
      check('卡片含 👍 / 👎 按钮（每卡各一个）',
        cards && (todayHtml.match(/data-role="vote-up"/g) || []).length === 2 &&
        (todayHtml.match(/data-role="vote-down"/g) || []).length === 2);
      check('已点赞的卡片按钮高亮',
        cards && todayHtml.indexOf('class="vote on" type="button" data-role="vote-up"') >= 0);
      check('来源权重默认不直出（只在 title/hover 里）',
        cards && todayHtml.indexOf('class="wtag"') >= 0 && todayHtml.indexOf('权重') >= 0);
      check('来源展开里带 Tier 标记', cards && (todayHtml.match(/class="tag t[0-9]"/g) || []).length >= 1);
      check('卡片展示可信度判定依据',
        cards && todayHtml.indexOf('cred-basis') >= 0 && todayHtml.indexOf('Tier ') >= 0);
      check('模块标签各自带刷新按钮',
        elements['modtabs'] && (String(elements['modtabs'].innerHTML).match(/data-refresh-module/g) || []).length >= 2);
      check('启动只请求一次 /api/news', calls.news === 1, 'news=' + calls.news);
      check('启动只请求一次 /api/meta', calls.meta === 1, 'meta=' + calls.meta);

      // 切到历史页：应当只发一次历史请求
      if (global.NewsApp && global.NewsApp.showView) { global.NewsApp.showView('history'); }
      setTimeout(() => {
        const histHtml = hcards ? String(hcards.innerHTML) : '';
        check('切到历史页只请求一次 /api/history', calls.history === 1, 'history=' + calls.history);
        check('历史卡片也带判定依据', hcards && histHtml.indexOf('cred-basis') >= 0);
        check('历史页显示全部条目（含已隐藏）',
          hcards && countCards(histHtml) === 3, hcards ? countCards(histHtml) + ' 张' : '无容器');
        check('历史页已隐藏条目带「已隐藏」标记',
          hcards && histHtml.indexOf('hidden-tag') >= 0 && histHtml.indexOf('已隐藏') >= 0);
        check('历史页已隐藏条目带「取消隐藏」入口',
          hcards && (histHtml.match(/data-role="unhide"/g) || []).length === 1);
        check('历史页默认不带已隐藏筛选',
          calls.lastHistoryUrl.indexOf('hidden=') < 0, calls.lastHistoryUrl);
        // 切到「只看已隐藏」：应发出带 hidden=only 的第二次请求
        const hiddenSel = elements['h-hidden'];
        if (hiddenSel && hiddenSel._h && hiddenSel._h.change) {
          hiddenSel.value = 'only';
          hiddenSel._h.change();
        }
        setTimeout(() => {
          check('历史页可切「只看已隐藏」（带 hidden=only 重新请求）',
            calls.history === 2 && calls.lastHistoryUrl.indexOf('hidden=only') >= 0,
            'history=' + calls.history + ' ' + calls.lastHistoryUrl);
          process.exit(failures ? 1 : 0);
        }, 40);
      }, 40);
    }, 60);
  } catch (err) {
    check('元素存在时渲染不抛错', false, err && err.message);
    process.exit(1);
  }
}
