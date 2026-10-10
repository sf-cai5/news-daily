/* 今日视图（today.js）— 只操作 #today-view 里的 DOM，与历史视图互不干扰 */
(function () {
  'use strict';

  var A = window.NewsApp;
  if (!A) { return; }
  var t = A.t, esc = A.esc, el = A.el;

  var LEVELS = ['已证实', '较可信', '单一来源', '存疑'];
  var news = null;          // /api/news 的原始数据
  var meta = null;          // /api/meta
  var eventsBound = false;  // 事件只绑定一次
  var busy = false;
  var pending = null;       // 请求进行中就不再发第二次
  var firstLoadDone = false;

  var ui = {
    module: 'comprehensive', page: 1, hotOnly: false, cred: '', minScore: '', sort: 'published'
  };
  try {
    var saved = localStorage.getItem('news_module');
    if (saved) { ui.module = saved; }
  } catch (e) { /* 忽略 */ }

  function domainLabel(key) {
    var map = (meta && meta.module_labels) || null;
    if (map && map[key]) { return map[key]; }
    return { zh: key || '', en: key || '' };
  }

  function tierOf(name) {
    var map = (meta && meta.source_tiers) || {};
    return (map[name] && map[name].tier) || 4;
  }

  /* 来源当前权重：默认不显示，鼠标悬停时才在 title 里看到（spec v2.3 第 5 节） */
  function weightOf(item, name) {
    var map = (item && item.source_weights) || (meta && meta.source_weights) || {};
    return Number(map[name] || 0);
  }

  function weightText(weight) {
    return t().weight + ' ' + (weight > 0 ? '+' + weight : String(weight));
  }

  function credLabel(cred) {
    var en = (meta && meta.credibility_en && meta.credibility_en[cred]) || '';
    return esc(cred + (en ? ' / ' + en : ''));
  }

  /* ---------------- 渲染：模块标签、筛选项、配额 ---------------- */

  function renderModuleTabs() {
    var container = el('modtabs');
    if (!container) { return; }
    var mods = (meta && meta.modules) || [{ key: 'comprehensive', label: { zh: '综合', en: 'All' } }];
    // 模块标签栏里只有模块：标签 + 该模块自己的刷新按钮
    container.innerHTML = mods.map(function (m) {
      var count = '';
      if (m.key === ui.module && news && news.counts) {
        count = '<span class="cnt">' + (news.counts.total || 0) + '</span>';
      }
      var spinning = (busy && ui.module === m.key) ? ' spin' : '';
      return '<span class="mtab' + (m.key === ui.module ? ' active' : '') + '">' +
        '<button class="mtab-name" type="button" data-module="' + esc(m.key) + '">' +
        A.bi(m.label.zh, m.label.en) + '</button>' + count +
        '<button class="mtab-refresh' + spinning + '" type="button" data-refresh-module="' + esc(m.key) +
        '" title="刷新该模块 / Refresh this module">⟳</button></span>';
    }).join('');
  }

  function renderFilters() {
    var credSel = el('cred-filter');
    if (credSel) {
      var levels = (meta && meta.credibility_levels) || LEVELS;
      credSel.innerHTML = ['<option value="">' + esc(t().allCred) + '</option>'].concat(
        levels.map(function (lv) {
          var en = (meta && meta.credibility_en && meta.credibility_en[lv]) || '';
          return '<option value="' + esc(lv) + '"' + (ui.cred === lv ? ' selected' : '') + '>' +
            esc(lv + (en ? ' / ' + en : '')) + '</option>';
        })).join('');
    }
    var scoreSel = el('score-filter');
    if (scoreSel) {
      scoreSel.innerHTML = ['<option value="">' + esc(t().allScore) + '</option>'].concat(
        [5, 4, 3, 2, 1].map(function (n) {
          return '<option value="' + n + '"' + (String(ui.minScore) === String(n) ? ' selected' : '') + '>' +
            esc(t().scoreAtLeast(n)) + '</option>';
        })).join('');
    }
    var sortSel = el('sort');
    if (sortSel) {
      sortSel.innerHTML = [
        ['published', t().sortPublished], ['hotspot', t().sortHotspot],
        ['user_score', t().sortUserScore], ['shown', t().sortShown]
      ].map(function (row) {
        return '<option value="' + row[0] + '"' + (ui.sort === row[0] ? ' selected' : '') + '>' +
          esc(row[1]) + '</option>';
      }).join('');
    }
  }

  function renderQuota() {
    var node = el('quota');
    if (!node || !news) { return; }
    var counts = news.counts || {};
    var parts = [];
    if (ui.module === 'comprehensive') {
      var quota = news.quota || {};
      parts.push(t().quota + '：' + Object.keys(quota).map(function (k) {
        return domainLabel(k).zh + ' ' + quota[k];
      }).join(' · '));
      parts.push(t().current + '：' + Object.keys(quota).map(function (k) {
        return domainLabel(k).zh + ' ' + ((counts.by_domain || {})[k] || 0);
      }).join(' · '));
    } else {
      var q = news.quota || { hot: 5, normal: 5 };
      parts.push(t().quota + '：' + t().hotspot + ' ' + q.hot + ' + ' + (A.isEn() ? 'normal' : '普通') + ' ' + q.normal);
      parts.push(t().current + '：' + t().hotspot + ' ' + (counts.hot || 0) + ' · ' +
        (A.isEn() ? 'normal' : '普通') + ' ' + (counts.normal || 0) + ' · ' + t().unscored + ' ' + (counts.unscored || 0));
    }
    node.textContent = parts.join('　|　');
  }

  /* ---------------- 渲染：卡片 ---------------- */

  function credControl(item) {
    var cred = item.credibility || '单一来源';
    var manual = item.credibility_manual
      ? '<span class="tag manual" title="' + esc(t().auto + '：' + (item.credibility_auto || '')) + '">' + esc(t().manual) + '</span>'
      : '';
    var options = ((meta && meta.credibility_levels) || LEVELS).map(function (lv) {
      var en = (meta && meta.credibility_en && meta.credibility_en[lv]) || '';
      return '<option value="' + esc(lv) + '"' + (lv === cred ? ' selected' : '') + '>' +
        esc(lv + (en ? ' / ' + en : '')) + '</option>';
    }).join('');
    var title = t().auto + '：' + (item.credibility_auto || cred) +
      '　' + t().tier + '：Tier 1–4';
    return '<span class="cred cred-' + esc(cred) + '" data-role="cred" title="' + esc(title) + '">' +
      credLabel(cred) + '</span>' + manual +
      '<select data-role="set-cred" class="hidden" title="人工覆盖可信度">' + options + '</select>';
  }

  function hotspotTag(item) {
    if (item.hotspot_status !== '已生成' || !item.hotspot_score) {
      return '<span class="tag hot-none">' + esc(t().unscored) + '</span>';
    }
    return '<span class="tag ' + (item.hotspot_score >= 3 ? 'hot' : 'muted') + '">' +
      esc(t().hotspot + ' ' + item.hotspot_score + '/5') + '</span>';
  }

  function starsHtml(item) {
    var score = item.user_score || 0;
    var stars = [1, 2, 3, 4, 5].map(function (n) {
      return '<button class="star' + (n <= score ? ' on' : '') + '" type="button" data-role="star" data-v="' + n +
        '" title="' + esc(t().scoreAtLeast(n)) + '">★</button>';
    }).join('');
    return '<div class="stars" data-role="stars" title="' + esc(t().rateHint) + '">' + stars +
      '<input class="note" data-role="note" type="text" maxlength="120" placeholder="' + esc(t().note) +
      '" value="' + esc(item.user_note || '') + '"></div>';
  }

  function sourcesHtml(item) {
    var list = item.sources || [];
    if (!list.length) { return ''; }
    var head = list[0] + (list.length > 1 ? ' +' + (list.length - 1) : '');
    return '<details class="sources"><summary>' + esc(t().sources + '：' + head +
      (list.length > 1 ? '（' + esc(t().sourceCount(list.length)) + '）' : '')) + '</summary><ul>' +
      list.map(function (s) {
        var tier = tierOf(s);
        var weight = weightOf(item, s);
        // 权重默认不显示，只有鼠标悬停在来源上才看到（spec v2.3 第 5 节）
        return '<li class="srow" title="' + esc(weightText(weight)) + '">' +
          '<span class="tag t' + tier + '">T' + tier + '</span> ' + esc(s) +
          '<span class="wtag">' + esc(weight > 0 ? '+' + weight : String(weight)) + '</span></li>';
      }).join('') + '</ul></details>';
  }

  /* spec v2.3：👍 / 👎 */
  function votesHtml(item) {
    var vote = item.user_vote || 'none';
    return '<span class="votes">' +
      '<button class="vote' + (vote === 'up' ? ' on' : '') + '" type="button" data-role="vote-up" title="' +
        esc(t().voteUpHint) + '">👍</button>' +
      '<button class="vote' + (vote === 'down' ? ' on' : '') + '" type="button" data-role="vote-down" title="' +
        esc(t().voteDownHint) + '">👎</button></span>';
  }

  function publishedHtml(item) {
    if (item.published_unknown) { return '<span class="unknown">' + esc(t().publishedUnknown) + '</span>'; }
    return esc(t().published + '：' + String(item.published_at).replace('T', ' '));
  }

  function transBadge(item) {
    var status = item.translation_status || '未翻译';
    if (status === '已翻译') { return '<span class="tag muted">' + esc(t().translated) + '</span>'; }
    if (status === '部分') { return '<span class="tag muted">' + esc(t().partial) + '</span>'; }
    return '<span class="tag muted">' + esc(t().notTranslated) + '</span>';
  }

  function cardHtml(item, index) {
    var dom = domainLabel(item.domain);
    var title = item.display_title || item.title || '';
    var summary = item.display_summary || item.summary || '';
    var highlight = item.highlight || '';
    var conflict = item.conflict_note || '';
    var cred = item.credibility || '单一来源';
    var basis = basisOf(item);
    return '' +
      '<article class="card" data-id="' + item.id + '">' +
        '<div class="card-top">' +
          '<span class="tag domain">' + A.bi(dom.zh, dom.en) + '</span>' +
          credControl(item) + hotspotTag(item) + transBadge(item) + votesHtml(item) +
        '</div>' +
        // 可信度必须带上判定依据：`单一来源（中国新闻网 / Tier 3）`
        '<div class="cred-basis" title="' + esc(cred + '（' + basis + '）') + '">' +
          '<b>' + credLabel(cred) + '</b>（' + esc(basis) + '）</div>' +
        '<h2><a href="' + esc(item.url) + '" target="_blank" rel="noopener">' +
          (index + 1) + '. ' + esc(title) + '</a></h2>' +
        '<p class="summary">' + esc(summary) + '</p>' +
        (highlight ? '<p class="highlight">' + esc(t().highlight + '：' + highlight) + '</p>' : '') +
        (conflict ? '<p class="conflict">' + esc(t().conflict + '：' + conflict) + '</p>' : '') +
        '<div class="meta-line">' + publishedHtml(item) +
          '<span>' + esc(t().shown + '：' + String(item.module_shown_at || '').replace('T', ' ')) + '</span>' +
        '</div>' +
        sourcesHtml(item) +
        '<div class="card-foot">' +
          starsHtml(item) +
          '<div class="card-actions">' +
            '<button class="btn tiny" type="button" data-role="copy-oral">' + esc(t().copyOral) + '</button>' +
            '<button class="btn tiny" type="button" data-role="copy-trans">' + esc(t().copyTranslate) + '</button>' +
            '<button class="btn tiny" type="button" data-role="fill-trans">' + esc(t().fillTranslate) + '</button>' +
            '<button class="btn tiny" type="button" data-role="copy-hot">' + esc(t().copyHotspot) + '</button>' +
            '<button class="btn tiny" type="button" data-role="fill-hot">' + esc(t().fillHotspot) + '</button>' +
            '<a class="link" href="' + esc(item.url) + '" target="_blank" rel="noopener">' + esc(t().link) + '</a>' +
          '</div>' +
        '</div>' +
      '</article>';
  }

  /** 判定依据：优先用后端算好的，老数据就按来源分级现拼。 */
  function basisOf(item) {
    if (item.credibility_basis) { return item.credibility_basis; }
    var names = item.sources || [];
    if (!names.length) { return '—'; }
    return names.map(function (s) { return s + ' / Tier ' + tierOf(s); }).join('，');
  }

  function render() {
    var container = el('cards');
    if (!container || !news) { return; }
    // 点踩过的新闻后端已经不下发，这里再兜一道，保证卡片立刻消失（spec v2.3 第 5 节）
    var items = (news.items || []).filter(function (i) { return !i.hidden; });
    container.innerHTML = '';                      // 渲染前先清空，避免叠卡片
    if (!items.length) {
      container.innerHTML = '<p class="empty">' +
        esc((ui.cred || ui.hotOnly || ui.minScore) ? t().emptyFiltered : t().empty) + '</p>';
    } else {
      container.innerHTML = items.map(cardHtml).join('');
    }

    A.setText('pageinfo', (news.page || 1) + ' / ' + (news.pages || 1));
    A.setText('meta', t().total + ' ' + (news.total || 0) + ' ' + t().items + ' · ' + (news.db_total || 0) + ' in DB');
    renderQuota();
    renderModuleTabs();

    var last = news.last_refresh;
    if (last && last.message) {
      var cls = last.ok === false ? 'err' : ((last.flags && last.flags.shortage) ? 'warn' : 'ok');
      A.banner('banner', cls, esc(t().refreshDone) + '：' + esc(last.message));
    } else if (news.shortage) {
      A.banner('banner', 'warn', esc(t().shortage || ''));
    } else {
      A.banner('banner', '', '');
    }
    var errors = (last && last.errors) || [];
    A.setText('errs', errors.length ? t().failedSources + errors.length : '');
  }

  /* ---------------- 数据 ---------------- */

  function newsUrl() {
    var params = ['module=' + encodeURIComponent(ui.module), 'lang=' + A.state.lang,
      'sort=' + encodeURIComponent(ui.sort), 'page=' + ui.page,
      'page_size=' + ((meta && meta.display_limit) || 10)];
    if (ui.cred) { params.push('credibility=' + encodeURIComponent(ui.cred)); }
    if (ui.minScore) { params.push('min_user_score=' + encodeURIComponent(ui.minScore)); }
    if (ui.hotOnly) {
      params.push('min_hotspot=' + ((meta && meta.module_quota && meta.module_quota.hot_threshold) || 3));
    }
    return '/api/news?' + params.join('&');
  }

  function load() {
    if (pending) { return pending; }        // 同一时刻只发一个请求，避免重复渲染
    pending = A.api(newsUrl()).then(function (data) {
      if (!data || !data.ok) { A.banner('banner', 'err', esc((data && data.error) || 'ERR')); return; }
      news = data;
      firstLoadDone = true;
      render();
    }).catch(function (e) { A.banner('banner', 'err', esc(String(e))); })
      .then(function () { pending = null; });
    return pending;
  }

  function findItem(card) {
    if (!card || !news) { return null; }
    var id = card.getAttribute('data-id');
    var items = news.items || [];
    for (var i = 0; i < items.length; i++) {
      if (String(items[i].id) === String(id)) { return items[i]; }
    }
    return null;
  }

  /* ---------------- 弹层（译文 / 评价） ---------------- */

  function modal(html) {
    var box = el('modal-box'), wrap = el('modal');
    if (!box || !wrap) { return null; }
    box.innerHTML = html;
    wrap.classList.remove('hidden');
    return box;
  }

  function closeModal() {
    var wrap = el('modal'), box = el('modal-box');
    if (wrap) { wrap.classList.add('hidden'); }
    if (box) { box.innerHTML = ''; }
  }

  function fillTranslationModal(item) {
    var target = (item.source_lang || 'zh') === 'zh' ? t().langEn : t().langZh;
    var box = modal(
      '<h3>' + esc(t().fillTranslate) + ' → ' + esc(target) + '</h3>' +
      '<p class="hint">' + esc(t().promptFirst) + '：<b>' + esc(t().copyTranslate) + '</b>　' + esc(t().pasteBack) + '</p>' +
      '<pre>' + esc(item.translation_prompt || '') + '</pre>' +
      '<label class="field">' + esc(t().transTitle) + '</label>' +
      '<input type="text" id="m-title" style="width:100%">' +
      '<label class="field">' + esc(t().transSummary) + '</label>' +
      '<textarea id="m-summary"></textarea>' +
      '<div class="modal-actions">' +
        '<button class="btn" type="button" data-role="copy-modal">' + esc(t().copyTranslate) + '</button>' +
        '<button class="btn" type="button" data-role="close-modal">' + esc(t().cancel) + '</button>' +
        '<button class="btn primary" type="button" data-role="save-trans">' + esc(t().save) + '</button>' +
      '</div>');
    if (!box) { return; }
    var copyBtn = box.querySelector('[data-role="copy-modal"]');
    if (copyBtn) { copyBtn.addEventListener('click', function () { A.copyText(item.translation_prompt || ''); }); }
    var closeBtn = box.querySelector('[data-role="close-modal"]');
    if (closeBtn) { closeBtn.addEventListener('click', closeModal); }
    var saveBtn = box.querySelector('[data-role="save-trans"]');
    if (saveBtn) {
      saveBtn.addEventListener('click', function () {
        var titleEl = el('m-title'), summaryEl = el('m-summary');
        A.post('/api/news/' + item.id + '/translation', {
          title: titleEl ? titleEl.value : '', summary: summaryEl ? summaryEl.value : ''
        }).then(function (res) {
          if (!res || !res.ok) { A.toast((res && res.error) || t().failed); return; }
          A.toast(t().saved); closeModal(); load();
        }).catch(function () { A.toast(t().failed); });
      });
    }
  }

  function fillHotspotModal(item) {
    var box = modal(
      '<h3>' + esc(t().fillHotspot) + '</h3>' +
      '<p class="hint">' + esc(t().promptFirst) + '：<b>' + esc(t().copyHotspot) + '</b>　' + esc(t().pasteBack) + '</p>' +
      '<pre>' + esc(item.hotspot_prompt || '') + '</pre>' +
      '<label class="field">' + esc(t().score) + '</label>' +
      '<select id="m-score">' + [1, 2, 3, 4, 5].map(function (n) {
        return '<option value="' + n + '"' + (item.hotspot_score === n ? ' selected' : '') + '>' + n + '</option>';
      }).join('') + '</select>' +
      '<label class="field">' + esc(t().highlightLabel) + '</label>' +
      '<input type="text" id="m-highlight" style="width:100%" maxlength="60" value="' + esc(item.highlight || '') + '">' +
      '<div class="modal-actions">' +
        '<button class="btn" type="button" data-role="copy-modal">' + esc(t().copyHotspot) + '</button>' +
        '<button class="btn" type="button" data-role="close-modal">' + esc(t().cancel) + '</button>' +
        '<button class="btn primary" type="button" data-role="save-hot">' + esc(t().save) + '</button>' +
      '</div>');
    if (!box) { return; }
    var copyBtn = box.querySelector('[data-role="copy-modal"]');
    if (copyBtn) { copyBtn.addEventListener('click', function () { A.copyText(item.hotspot_prompt || ''); }); }
    var closeBtn = box.querySelector('[data-role="close-modal"]');
    if (closeBtn) { closeBtn.addEventListener('click', closeModal); }
    var saveBtn = box.querySelector('[data-role="save-hot"]');
    if (saveBtn) {
      saveBtn.addEventListener('click', function () {
        var scoreEl = el('m-score'), hlEl = el('m-highlight');
        A.post('/api/news/' + item.id + '/evaluation', {
          hotspot_score: scoreEl ? scoreEl.value : null,
          highlight: hlEl ? hlEl.value : ''
        }).then(function (res) {
          if (!res || !res.ok) { A.toast((res && res.error) || t().failed); return; }
          A.toast(t().saved); closeModal(); load();
        }).catch(function () { A.toast(t().failed); });
      });
    }
  }

  /* ---------------- 评分与操作 ---------------- */

  function paintStars(card, score) {
    if (!card) { return; }
    var stars = card.querySelectorAll('[data-role="star"]');
    Array.prototype.forEach.call(stars, function (s) {
      s.classList.toggle('on', Number(s.getAttribute('data-v')) <= score);
    });
  }

  function saveScore(item, card, score, noteEl) {
    var payload = { score: score };
    if (noteEl) { payload.note = noteEl.value || ''; }
    return A.post('/api/news/' + item.id + '/user-score', payload).then(function (res) {
      if (!res || !res.ok) { A.toast((res && res.error) || t().failed); return; }
      item.user_score = res.user_score;
      if (typeof res.user_note === 'string') { item.user_note = res.user_note; }
      paintStars(card, res.user_score || 0);
      A.toast(t().savedRating);
    }).catch(function () { A.toast(t().failed); });
  }

  /* spec v2.3：👍 权重 +1；👎 权重 -1 且本条从今日页消失（历史页仍可查、可取消隐藏） */
  function paintVote(card, vote) {
    if (!card) { return; }
    var up = card.querySelector('[data-role="vote-up"]');
    var down = card.querySelector('[data-role="vote-down"]');
    if (up) { up.classList.toggle('on', vote === 'up'); }
    if (down) { down.classList.toggle('on', vote === 'down'); }
  }

  function sendVote(item, card, action) {
    return A.post('/api/news/' + item.id + '/vote', { action: action }).then(function (res) {
      if (!res || !res.ok) { A.toast((res && res.error) || t().failed); return; }
      item.user_vote = res.user_vote;
      item.hidden = !!res.hidden;
      if (action === 'down') {
        if (card && card.parentNode) { card.parentNode.removeChild(card); }
        A.toast(t().hiddenToast);
        load();                                   // 重新取一次列表，分页与配额对齐
        return;
      }
      paintVote(card, res.user_vote);
      A.toast(res.message || t().voteUpToast);
      load();
    }).catch(function () { A.toast(t().failed); });
  }

  function bindEvents() {
    if (eventsBound) { return; }          // 事件只绑一次
    eventsBound = true;

    var tabs = el('modtabs');
    if (tabs) {
      tabs.addEventListener('click', function (ev) {
        var target = ev.target;
        if (!target || !target.closest) { return; }
        // 模块标签上的 ⟳：先切到该模块，再刷新该模块
        var refreshTab = target.closest('[data-refresh-module]');
        if (refreshTab) {
          switchModule(refreshTab.getAttribute('data-refresh-module'));
          doRefresh();
          return;
        }
        var nameBtn = target.closest('.mtab-name');
        if (nameBtn) { switchModule(nameBtn.getAttribute('data-module')); }
      });
    }

    var refreshBtn = el('refresh');
    if (refreshBtn) { refreshBtn.addEventListener('click', doRefresh); }

    var prev = el('prev');
    if (prev) { prev.addEventListener('click', function () { if (ui.page > 1) { ui.page -= 1; load(); } }); }
    var next = el('next');
    if (next) {
      next.addEventListener('click', function () {
        var pages = (news && news.pages) || 1;
        if (ui.page < pages) { ui.page += 1; load(); }
      });
    }
    var hotOnly = el('hot-only');
    if (hotOnly) {
      hotOnly.addEventListener('change', function () { ui.hotOnly = !!hotOnly.checked; ui.page = 1; load(); });
    }
    var credSel = el('cred-filter');
    if (credSel) { credSel.addEventListener('change', function () { ui.cred = credSel.value; ui.page = 1; load(); }); }
    var scoreSel = el('score-filter');
    if (scoreSel) { scoreSel.addEventListener('change', function () { ui.minScore = scoreSel.value; ui.page = 1; load(); }); }
    var sortSel = el('sort');
    if (sortSel) { sortSel.addEventListener('change', function () { ui.sort = sortSel.value; ui.page = 1; load(); }); }

    var cards = el('cards');
    if (cards) {
      cards.addEventListener('click', function (ev) {
        var target = ev.target;
        if (!target || !target.closest) { return; }
        var card = target.closest('.card');
        if (!card) { return; }
        var item = findItem(card);
        if (!item) { return; }
        var roleEl = target.closest('[data-role]');
        var role = roleEl ? roleEl.getAttribute('data-role') : '';
        if (role === 'star') {
          saveScore(item, card, Number(roleEl.getAttribute('data-v')), card.querySelector('[data-role="note"]'));
        } else if (role === 'vote-up') { sendVote(item, card, 'up'); }
        else if (role === 'vote-down') { sendVote(item, card, 'down'); }
        else if (role === 'copy-oral') { A.copyText(item.oral_prompt || ''); }
        else if (role === 'copy-trans') { A.copyText(item.translation_prompt || ''); }
        else if (role === 'copy-hot') { A.copyText(item.hotspot_prompt || ''); }
        else if (role === 'fill-trans') { fillTranslationModal(item); }
        else if (role === 'fill-hot') { fillHotspotModal(item); }
        else if (role === 'cred') {
          var sel = card.querySelector('[data-role="set-cred"]');
          if (sel) { sel.classList.toggle('hidden'); sel.focus(); }
        }
      });

      cards.addEventListener('change', function (ev) {
        var target = ev.target;
        if (!target || !target.closest) { return; }
        var card = target.closest('.card');
        if (!card) { return; }
        var item = findItem(card);
        if (!item) { return; }
        if (target.matches('[data-role="set-cred"]')) {
          A.post('/api/news/' + item.id + '/credibility', { credibility: target.value }).then(function (res) {
            if (!res || !res.ok) { A.toast((res && res.error) || t().failed); return; }
            item.credibility = target.value;
            item.credibility_manual = item.credibility_auto !== target.value;
            A.toast(t().saved);
            load();
          }).catch(function () { A.toast(t().failed); });
        }
      });

      cards.addEventListener('blur', function (ev) {
        var target = ev.target;
        if (!target || !target.closest || !target.matches || !target.matches('[data-role="note"]')) { return; }
        var card = target.closest('.card');
        var item = findItem(card);
        if (!item) { return; }
        if ((item.user_note || '') === target.value) { return; }
        saveScore(item, card, item.user_score, target);
      }, true);

      cards.addEventListener('keydown', function (ev) {
        var target = ev.target;
        if (!target || !target.matches || !target.matches('[data-role="note"]')) { return; }
        if (ev.key === 'Enter') { target.blur(); }
      });
    }

    var wrap = el('modal');
    if (wrap) {
      wrap.addEventListener('click', function (ev) { if (ev.target === wrap) { closeModal(); } });
    }
  }

  /* ---------------- 刷新（异步 + 轮询） ---------------- */

  function pollRefresh(module, tries) {
    tries = tries || 0;
    A.api('/api/refresh/status').then(function (data) {
      if (!data) { return; }
      var st = (data.modules || {})[module] || {};
      if (data.running) {
        A.setText('progress', st.progress || t().refreshing);
        if (tries < 400) { setTimeout(function () { pollRefresh(module, tries + 1); }, 1500); }
        return;
      }
      A.setText('progress', '');
      var btn = el('refresh');
      if (btn) { btn.disabled = false; btn.textContent = t().refresh; }
      busy = false;
      load().then(function () {
        var result = (st.result && st.result.message) || '';
        if (result) { A.toast(t().refreshDone + '：' + result); }
      });
    }).catch(function () {
      A.setText('progress', '');
      var btn = el('refresh');
      if (btn) { btn.disabled = false; btn.textContent = t().refresh; }
      busy = false;
    });
  }

  function doRefresh() {
    if (busy) { return; }
    busy = true;
    var btn = el('refresh');
    if (btn) { btn.disabled = true; btn.textContent = t().refreshing; }
    A.setText('progress', t().queueing);
    A.api('/api/refresh?module=' + encodeURIComponent(ui.module), { method: 'POST' }).then(function (res) {
      if (res && res.busy) {
        A.toast(res.message || t().fetching);
        busy = false;
        if (btn) { btn.disabled = false; btn.textContent = t().refresh; }
        A.setText('progress', '');
        return;
      }
      if (!res || !res.ok) {
        A.banner('banner', 'err', esc((res && res.error) || 'ERR'));
        busy = false;
        if (btn) { btn.disabled = false; btn.textContent = t().refresh; }
        A.setText('progress', '');
        return;
      }
      pollRefresh(ui.module, 0);
    }).catch(function () {
      busy = false;
      if (btn) { btn.disabled = false; btn.textContent = t().refresh; }
      A.setText('progress', '');
    });
  }

  function switchModule(module) {
    if (!module || module === ui.module) { return; }
    ui.module = module;
    ui.page = 1;
    try { localStorage.setItem('news_module', module); } catch (e) { /* 忽略 */ }
    renderModuleTabs();
    load();
  }

  /* ---------------- 对外 ---------------- */

  function bootToday() {
    bindEvents();
    A.loadMeta().then(function (data) {          // 与历史页共用一份 /api/meta
      if (data && data.ok !== false && data.modules) {
        meta = data;
        var keys = (meta.modules || []).map(function (m) { return m.key; });
        if (keys.length && keys.indexOf(ui.module) < 0) { ui.module = 'comprehensive'; }
        renderModuleTabs();
        renderFilters();
      }
      load();
    }).catch(function () { load(); });
  }

  A.registerView('today', {
    onShow: function () {
      if (firstLoadDone) { load(); }   // 首次由 bootToday 负责，避免启动时连发两次请求
    }
  });
  A.onLangChange(function () {
    if (news) { render(); }        // 已加载过就重渲染，不重新请求
    renderFilters();
    var btn = el('refresh');
    if (btn && !busy) { btn.textContent = t().refresh; }
  });

  document.addEventListener('DOMContentLoaded', function () { bootToday(); });
})();
