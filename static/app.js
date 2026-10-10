/* 每日新闻聚合器 — 共享引导层（纯工具，不操作任何视图的 DOM 容器）
   职责：i18n 文案、接口请求、剪贴板、toast、视图切换、语言切换。
   今日视图在 today.js，历史视图在 history.js，两边各自管自己的 DOM。 */
window.NewsApp = (function () {
  'use strict';

  var TEXT = {
    zh: {
      loading: '加载中…', refresh: '刷新', refreshing: '抓取中…', queueing: '排队中…',
      fetching: '正在抓取新闻源、去重、验证…',
      empty: '这里还没有内容，点「刷新」抓一批。',
      emptyFiltered: '当前筛选条件下没有结果。',
      allCred: '全部可信度', allScore: '全部评分', scoreAtLeast: function (n) { return n + ' 星及以上'; },
      sortPublished: '发布时间倒序', sortHotspot: '热点评分优先', sortShown: '展示时间倒序', sortUserScore: '我的评分优先',
      onlyHot: '只看高热点',
      quota: '配额', current: '当前',
      published: '发布', publishedUnknown: '发布时间未知', shown: '展示',
      sources: '来源', sourceCount: function (n) { return n + ' 个来源'; },
      hotspot: '热点', unscored: '未评价', highlight: '看点', conflict: '冲突点',
      copyOral: '复制口播提示词', copyTranslate: '复制翻译提示词', copyHotspot: '复制评价提示词',
      fillTranslate: '填入译文', fillHotspot: '填入评价', detail: '详情', link: '原始链接',
      copied: '已复制到剪贴板', copyFail: '复制失败，请手动选择',
      notTranslated: '未翻译', partial: '部分翻译', translated: '已翻译',
      langZh: '中文', langEn: '英文', sourceLang: '原文',
      transTitle: '标题', transSummary: '摘要', score: '热点评分（1–5）', highlightLabel: '一句话看点（≤30 字）',
      save: '保存', cancel: '取消', saved: '已保存', savedRating: '评分已保存', failed: '操作失败',
      promptFirst: '第一步：复制提示词到大模型', pasteBack: '第二步：把结果粘贴回来',
      prevPage: '上一页', nextPage: '下一页', total: '共', items: '条',
      search: '搜索', clear: '清空筛选', keyword: '关键词（只搜标题与摘要）', date: '日期',
      historyEmpty: '没有匹配的历史新闻。', readonly: '历史只读',
      failedSources: '失败源 ', refreshDone: '刷新完成',
      manual: '人工', auto: '自动判定', tier: '来源分级', myScore: '我的评分',
      note: '备注（可选）', rateHint: '点击星星评分',
      voteUp: '👍 点赞', voteDown: '👎 点踩', voteUpHint: '点赞：该来源权重 +1',
      voteDownHint: '点踩：该来源权重 -1，本条从今日页隐藏',
      hiddenToast: '已隐藏，可在历史页查看', voteUpToast: '已点赞，来源权重 +1',
      unhide: '取消隐藏', unhiddenToast: '已取消隐藏',
      hiddenTag: '已隐藏', allHidden: '全部（含已隐藏）', onlyHidden: '只看已隐藏',
      hideHidden: '隐藏的不看', weight: '权重', weightHint: '来源权重（点👍/👎累积，范围 -10 ~ +10）'
    },
    en: {
      loading: 'Loading…', refresh: 'Refresh', refreshing: 'Fetching…', queueing: 'Queued…',
      fetching: 'Fetching sources, dedup, verifying…',
      empty: 'Nothing here yet — hit Refresh.',
      emptyFiltered: 'No items match the current filters.',
      allCred: 'All credibility', allScore: 'All ratings', scoreAtLeast: function (n) { return n + '+ stars'; },
      sortPublished: 'Newest published first', sortHotspot: 'Hotspot first', sortShown: 'Newest shown first', sortUserScore: 'My rating first',
      onlyHot: 'Hotspots only',
      quota: 'Quota', current: 'Now',
      published: 'Published', publishedUnknown: 'Publish time unknown', shown: 'Shown',
      sources: 'Sources', sourceCount: function (n) { return n + ' sources'; },
      hotspot: 'Hotspot', unscored: 'Not rated', highlight: 'Why it matters', conflict: 'Conflict',
      copyOral: 'Copy narration prompt', copyTranslate: 'Copy translation prompt', copyHotspot: 'Copy rating prompt',
      fillTranslate: 'Enter translation', fillHotspot: 'Enter rating', detail: 'Details', link: 'Original',
      copied: 'Copied to clipboard', copyFail: 'Copy failed, please select manually',
      notTranslated: 'Not translated', partial: 'Partial translation', translated: 'Translated',
      langZh: 'Chinese', langEn: 'English', sourceLang: 'Source',
      transTitle: 'Title', transSummary: 'Summary', score: 'Hotspot score (1–5)', highlightLabel: 'One-line highlight (≤30 chars)',
      save: 'Save', cancel: 'Cancel', saved: 'Saved', savedRating: 'Rating saved', failed: 'Failed',
      promptFirst: 'Step 1: copy the prompt into your LLM', pasteBack: 'Step 2: paste the result back',
      prevPage: 'Prev', nextPage: 'Next', total: 'Total', items: 'items',
      search: 'Search', clear: 'Clear filters', keyword: 'Keyword (title & summary only)', date: 'Date',
      historyEmpty: 'No history matches.', readonly: 'history is read-only',
      failedSources: 'Failed sources: ', refreshDone: 'Refresh finished',
      manual: 'manual', auto: 'auto', tier: 'Source tier', myScore: 'My rating',
      note: 'Note (optional)', rateHint: 'Click stars to rate',
      voteUp: '👍 Up', voteDown: '👎 Down', voteUpHint: 'Thumbs up: this source +1 weight',
      voteDownHint: 'Thumbs down: this source -1 weight and hide this item today',
      hiddenToast: 'Hidden — still available in History', voteUpToast: 'Upvoted: source weight +1',
      unhide: 'Unhide', unhiddenToast: 'Unhidden',
      hiddenTag: 'Hidden', allHidden: 'All (incl. hidden)', onlyHidden: 'Hidden only',
      hideHidden: 'Hide hidden', weight: 'Weight', weightHint: 'Source weight (👍/👎, range -10 to +10)'
    }
  };

  var state = { lang: 'zh', view: 'today' };
  try { state.lang = localStorage.getItem('news_lang') === 'en' ? 'en' : 'zh'; } catch (e) { /* 隐私模式 */ }

  function t() { return TEXT[state.lang] || TEXT.zh; }
  function isEn() { return state.lang === 'en'; }

  function el(id) { return document.getElementById(id); }

  /* 所有 DOM 取值都走这里，元素不存在时返回 null 而不是抛错 */
  function setText(id, value) {
    var node = el(id);
    if (node) { node.textContent = value; }
  }

  function setHtml(id, value) {
    var node = el(id);
    if (node) { node.innerHTML = value; }
  }

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  /* 主语言在前，另一种语言用 .bi 弱化 */
  function bi(zh, en, sep) {
    sep = sep || ' / ';
    if (isEn()) { return esc(en) + (zh && zh !== en ? '<span class="bi">' + sep + esc(zh) + '</span>' : ''); }
    return esc(zh) + (en && en !== zh ? '<span class="bi">' + sep + esc(en) + '</span>' : '');
  }

  var toastTimer = null;
  function toast(msg) {
    var node = el('toast');
    if (!node) { return; }
    node.textContent = msg;
    node.classList.remove('hidden');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { node.classList.add('hidden'); }, 2000);
  }

  function banner(id, cls, html) {
    var node = el(id);
    if (!node) { return; }
    if (!html) { node.className = 'banner hidden'; node.innerHTML = ''; return; }
    node.className = 'banner ' + cls;
    node.innerHTML = html;
  }

  function api(path, opts) {
    return fetch(path, opts).then(function (r) {
      return r.json().catch(function () { return { ok: false, error: 'HTTP ' + r.status }; })
        .then(function (data) {
          if (!r.ok && !data.error) { data.error = 'HTTP ' + r.status; }
          return data;
        });
    });
  }

  /* /api/meta 只请求一次：今日页与历史页共用同一个 Promise */
  var metaCache = null, metaPending = null;
  function loadMeta() {
    if (metaCache) { return Promise.resolve(metaCache); }
    if (metaPending) { return metaPending; }
    metaPending = api('/api/meta').then(function (data) {
      metaCache = data || {};
      metaPending = null;
      return metaCache;
    }).catch(function () {
      metaPending = null;
      return {};
    });
    return metaPending;
  }

  function post(path, body) {
    return api(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {})
    });
  }

  function copyText(text, onDone) {
    var done = function () { toast(t().copied); if (onDone) { onDone(); } };
    var fallback = function () {
      var ta = document.createElement('textarea');
      ta.value = text;
      ta.style.position = 'fixed';
      ta.style.opacity = '0';
      document.body.appendChild(ta);
      ta.select();
      try { document.execCommand('copy'); done(); } catch (e) { toast(t().copyFail); }
      document.body.removeChild(ta);
    };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(done).catch(fallback);
    } else { fallback(); }
  }

  function labelOf(map, key) {
    return (map && map[key]) || { zh: key || '', en: key || '' };
  }

  /* 视图切换：只切显示，不重新抓取；各视图自己决定要不要重新加载数据 */
  var views = { today: null, history: null };
  function registerView(name, hooks) { views[name] = hooks || {}; }

  function showView(name) {
    if (name !== 'today' && name !== 'history') { return; }
    state.view = name;
    var todayView = el('today-view'), historyView = el('history-view');
    if (todayView) { todayView.classList.toggle('hidden', name !== 'today'); }
    if (historyView) { historyView.classList.toggle('hidden', name !== 'history'); }
    var tools = el('history-tools');
    if (tools) { tools.classList.toggle('hidden', name !== 'history'); }
    Array.prototype.forEach.call(document.querySelectorAll('.vtab'), function (b) {
      b.classList.toggle('active', b.getAttribute('data-view') === name);
    });
    var hook = views[name];
    if (hook && typeof hook.onShow === 'function') { hook.onShow(); }
  }

  var langHooks = [];
  function onLangChange(fn) { if (typeof fn === 'function') { langHooks.push(fn); } }

  function setLang(lang) {
    state.lang = lang === 'en' ? 'en' : 'zh';
    try { localStorage.setItem('news_lang', state.lang); } catch (e) { /* 忽略 */ }
    document.documentElement.lang = isEn() ? 'en' : 'zh-CN';
    setText('lang', isEn() ? '中' : 'EN');
    setText('hot-only-label', t().onlyHot);
    setText('h-search', t().search);
    setText('h-clear', t().clear);
    setText('prev', t().prevPage);
    setText('next', t().nextPage);
    setText('h-prev', t().prevPage);
    setText('h-next', t().nextPage);
    setText('h-today', isEn() ? 'Yest.' : '今天');
    var hq = el('h-q');
    if (hq) { hq.placeholder = t().keyword; }
    langHooks.forEach(function (fn) { try { fn(); } catch (e) { /* 单个视图出错不影响另一个 */ } });
  }

  /* 启动：全部初始化都在 DOM 就绪之后 */
  function boot() {
    setLang(state.lang);
    Array.prototype.forEach.call(document.querySelectorAll('.vtab'), function (b) {
      b.addEventListener('click', function () { showView(b.getAttribute('data-view')); });
    });
    var langBtn = el('lang');
    if (langBtn) {
      langBtn.addEventListener('click', function () { setLang(isEn() ? 'zh' : 'en'); });
    }
    showView('today');
  }

  return {
    TEXT: TEXT, t: t, isEn: isEn, state: state,
    el: el, setText: setText, setHtml: setHtml, esc: esc, bi: bi,
    toast: toast, banner: banner, api: api, post: post, copyText: copyText,
    labelOf: labelOf, registerView: registerView, showView: showView,
    onLangChange: onLangChange, setLang: setLang, boot: boot, loadMeta: loadMeta
  };
})();

document.addEventListener('DOMContentLoaded', function () {
  window.NewsApp.boot();
});
