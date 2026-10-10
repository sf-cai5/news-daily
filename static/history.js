/* 历史视图（history.js）— 只操作 #history-view 里的 DOM，只读
   搜索/清空按钮在右上角工具栏（#history-tools），不在模块标签栏里。 */
(function () {
  'use strict';

  var A = window.NewsApp;
  if (!A) { return; }
  var t = A.t, esc = A.esc, el = A.el;

  var data = null;          // /api/history 的原始数据
  var meta = null;
  var eventsBound = false;
  var loaded = false;
  var pending = null;       // 同一时刻只发一个请求

  // 只有「模块」这一个维度：综合 / AI / 民生 / 社会国内 / 社会国外
  var flt = { date: '', module: '', cred: '', minScore: '', q: '', hidden: '', page: 1 };

  function domainLabel(key) {
    var map = (meta && meta.module_labels) || null;
    if (map && map[key]) { return map[key]; }
    return { zh: key || '', en: key || '' };
  }

  function tierOf(name) {
    var map = (meta && meta.source_tiers) || {};
    return (map[name] && map[name].tier) || 4;
  }

  /* 来源当前权重：默认不显示，hover 才看到（spec v2.3 第 5 节） */
  function weightOf(item, name) {
    var map = (item && item.source_weights) || (meta && meta.source_weights) || {};
    return Number(map[name] || 0);
  }

  function basisOf(item) {
    if (item.credibility_basis) { return item.credibility_basis; }
    var names = item.sources || [];
    if (!names.length) { return '—'; }
    return names.map(function (s) { return s + ' / Tier ' + tierOf(s); }).join('，');
  }

  /* ---------------- 筛选项 ---------------- */

  function renderFilters() {
    var moduleSel = el('h-module');
    if (moduleSel) {
      var mods = ((meta && meta.modules) || []).map(function (m) {
        return '<option value="' + esc(m.key) + '"' + (flt.module === m.key ? ' selected' : '') + '>' +
          esc(m.label.zh + ' / ' + m.label.en) + '</option>';
      });
      moduleSel.innerHTML = ['<option value="">' + esc(A.isEn() ? 'Module: all' : '模块：全部') + '</option>']
        .concat(mods).join('');
    }
    var credSel = el('h-cred');
    if (credSel) {
      var levels = (meta && meta.credibility_levels) || ['已证实', '较可信', '单一来源', '存疑'];
      credSel.innerHTML = ['<option value="">' + esc(t().allCred) + '</option>'].concat(
        levels.map(function (lv) {
          return '<option value="' + esc(lv) + '"' + (flt.cred === lv ? ' selected' : '') + '>' +
            esc(lv) + '</option>';
        })).join('');
    }
    var scoreSel = el('h-score');
    if (scoreSel) {
      scoreSel.innerHTML = ['<option value="">' + esc(t().allScore) + '</option>'].concat(
        [5, 4, 3, 2, 1].map(function (n) {
          return '<option value="' + n + '"' + (String(flt.minScore) === String(n) ? ' selected' : '') + '>' +
            esc(t().scoreAtLeast(n)) + '</option>';
        })).join('');
    }
    var dateEl = el('h-date');
    if (dateEl && !dateEl.value && flt.date) { dateEl.value = flt.date; }
    // spec v2.3：已隐藏筛选（全部 / 只看已隐藏 / 隐藏的不看）
    var hiddenSel = el('h-hidden');
    if (hiddenSel) {
      hiddenSel.innerHTML = [['', t().allHidden], ['only', t().onlyHidden], ['visible', t().hideHidden]]
        .map(function (row) {
          return '<option value="' + row[0] + '"' + (flt.hidden === row[0] ? ' selected' : '') + '>' +
            esc(row[1]) + '</option>';
        }).join('');
    }
  }

  /* ---------------- 卡片（只读版，与今日视图的卡片分开维护） ---------------- */

  function starsHtml(item) {
    var score = item.user_score || 0;
    return '<div class="stars" title="' + esc(t().myScore) + '">' +
      [1, 2, 3, 4, 5].map(function (n) {
        return '<span class="star' + (n <= score ? ' on' : '') + '">★</span>';
      }).join('') +
      (item.user_note ? '<span class="bi">' + esc(item.user_note) + '</span>' : '') + '</div>';
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
        return '<li class="srow" title="' + esc(t().weight + ' ' + (weight > 0 ? '+' + weight : String(weight))) +
          '"><span class="tag t' + tier + '">T' + tier + '</span> ' + esc(s) +
          '<span class="wtag">' + esc(weight > 0 ? '+' + weight : String(weight)) + '</span></li>';
      }).join('') + '</ul></details>';
  }

  function historyCardHtml(item, index) {
    var dom = domainLabel(item.domain);
    var cred = item.credibility || '单一来源';
    var manual = item.credibility_manual ? '<span class="tag manual">' + esc(t().manual) + '</span>' : '';
    var hotspot = (item.hotspot_status === '已生成' && item.hotspot_score)
      ? '<span class="tag ' + (item.hotspot_score >= 3 ? 'hot' : 'muted') + '">' + esc(t().hotspot + ' ' + item.hotspot_score + '/5') + '</span>'
      : '<span class="tag hot-none">' + esc(t().unscored) + '</span>';
    var moduleTag = '<span class="tag muted">' + esc((domainLabel(item.module).zh) || item.module) + '</span>';
    // spec v2.3：被点踩隐藏的新闻在历史页照样查得到，并显示「已隐藏」标记
    var hiddenTag = item.hidden
      ? '<span class="tag hidden-tag" title="' + esc(item.hidden_basis || t().voteDownHint) + '">' +
        esc(t().hiddenTag) + '</span>'
      : '';
    var basis = basisOf(item);
    return '' +
      '<article class="card' + (item.hidden ? ' is-hidden' : '') + '" data-id="' + item.id + '">' +
        '<div class="card-top">' +
          moduleTag + hiddenTag +
          '<span class="tag domain">' + A.bi(dom.zh, dom.en) + '</span>' +
          '<span class="cred cred-' + esc(cred) + '">' + esc(cred) + '</span>' + manual +
          hotspot + '<span class="tag muted">' + esc(t().readonly) + '</span>' +
        '</div>' +
        '<div class="cred-basis">' + '<b>' + esc(cred) + '</b>（' + esc(basis) + '）</div>' +
        '<h2><a href="' + esc(item.url) + '" target="_blank" rel="noopener">' +
          (index + 1) + '. ' + esc(item.display_title || item.title || '') + '</a></h2>' +
        '<p class="summary">' + esc(item.display_summary || item.summary || '') + '</p>' +
        (item.highlight ? '<p class="highlight">' + esc(t().highlight + '：' + item.highlight) + '</p>' : '') +
        '<div class="meta-line">' +
          (item.published_unknown
            ? '<span class="unknown">' + esc(t().publishedUnknown) + '</span>'
            : '<span>' + esc(t().published + '：' + String(item.published_at).replace('T', ' ')) + '</span>') +
          '<span>' + esc(t().shown + '：' + String(item.module_shown_at || '').replace('T', ' ')) + '</span>' +
        '</div>' +
        sourcesHtml(item) +
        '<div class="card-foot">' +
          starsHtml(item) +
          '<div class="card-actions">' +
            '<button class="btn tiny" type="button" data-role="copy-oral">' + esc(t().copyOral) + '</button>' +
            (item.hidden
              ? '<button class="btn tiny" type="button" data-role="unhide">' + esc(t().unhide) + '</button>'
              : '') +
            '<button class="btn tiny" type="button" data-role="detail">' + esc(t().detail) + '</button>' +
            '<a class="link" href="' + esc(item.url) + '" target="_blank" rel="noopener">' + esc(t().link) + '</a>' +
          '</div>' +
        '</div>' +
      '</article>';
  }

  function render() {
    var container = el('hcards');
    if (!container) { return; }
    var items = (data && data.items) || [];
    container.innerHTML = '';                 // 渲染前清空，避免重复堆叠
    if (!items.length) {
      container.innerHTML = '<p class="empty">' + esc(t().historyEmpty) + '</p>';
    } else {
      container.innerHTML = items.map(historyCardHtml).join('');
    }
    var page = (data && data.page) || 1;
    var pages = (data && data.pages) || 1;
    A.setText('h-pageinfo', page + ' / ' + pages);
    A.setText('h-info', t().total + ' ' + ((data && data.total) || 0) + ' ' + t().items +
      '　·　' + esc(t().search) + '：' + (flt.q ? esc(flt.q) : '—'));
  }

  /* ---------------- 数据 ---------------- */

  function historyUrl() {
    var params = ['page=' + flt.page, 'lang=' + A.state.lang, 'dedup=1',
      'page_size=' + ((meta && meta.history_page_size) || 20)];
    if (flt.date) { params.push('date=' + encodeURIComponent(flt.date)); }
    if (flt.module) { params.push('module=' + encodeURIComponent(flt.module)); }
    if (flt.cred) { params.push('credibility=' + encodeURIComponent(flt.cred)); }
    if (flt.minScore) { params.push('min_user_score=' + encodeURIComponent(flt.minScore)); }
    if (flt.q) { params.push('q=' + encodeURIComponent(flt.q)); }
    if (flt.hidden) { params.push('hidden=' + encodeURIComponent(flt.hidden)); }
    return '/api/history?' + params.join('&');
  }

  function load() {
    if (pending) { return pending; }        // 同一时刻只发一个请求
    loaded = true;
    pending = A.api(historyUrl()).then(function (res) {
      if (!res || !res.ok) { A.banner('h-banner', 'err', esc((res && res.error) || 'ERR')); return; }
      data = res;
      render();
      A.banner('h-banner', '', '');
    }).catch(function (e) { A.banner('h-banner', 'err', esc(String(e))); })
      .then(function () { pending = null; });
    return pending;
  }

  function readInputs() {
    var dateEl = el('h-date'), qEl = el('h-q');
    flt.date = dateEl ? dateEl.value : flt.date;
    flt.q = qEl ? qEl.value.trim() : flt.q;
    var moduleSel = el('h-module'), credSel = el('h-cred'), scoreSel = el('h-score');
    var hiddenSel = el('h-hidden');
    if (moduleSel) { flt.module = moduleSel.value; }
    if (credSel) { flt.cred = credSel.value; }
    if (scoreSel) { flt.minScore = scoreSel.value; }
    if (hiddenSel) { flt.hidden = hiddenSel.value; }
  }

  /* ---------------- 详情弹层 ---------------- */

  function detailModal(item) {
    var box = el('modal-box'), wrap = el('modal');
    if (!box || !wrap) { return; }
    var rows = [
      ['模块', esc(domainLabel(item.module).zh) + ' / ' + esc(item.module || '')],
      ['领域', esc(domainLabel(item.domain).zh)],
      [t().transTitle, esc(item.display_title || item.title || '')],
      [t().transSummary, esc(item.display_summary || item.summary || '')],
      [t().sourceLang, item.source_lang === 'en' ? 'EN' : '中文'],
      [t().cred, esc(item.credibility || '') + '（' + esc(t().auto + '：' + (item.credibility_auto || '')) + '）'],
      [t().published, item.published_unknown
        ? esc(t().publishedUnknown)
        : esc(String(item.published_at).replace('T', ' ')) + '（' + esc(item.published_at_source || '') + '）'],
      [t().shown, esc(String(item.module_shown_at || '').replace('T', ' '))],
      [t().hotspot, (item.hotspot_score ? item.hotspot_score + '/5' : esc(t().unscored)) +
        (item.highlight ? '　' + esc(t().highlight + '：' + item.highlight) : '')],
      [t().myScore, (item.user_score ? '★'.repeat(item.user_score) : '—') +
        (item.user_note ? '　' + esc(item.user_note) : '')],
      [t().sources, (item.sources || []).map(function (s) {
        var tier = tierOf(s);
        return '<span class="tag t' + tier + '">T' + tier + '</span> ' + esc(s);
      }).join('　')],
      [t().conflict, esc(item.conflict_note || '—')],
      ['URL', '<a class="link" href="' + esc(item.url) + '" target="_blank" rel="noopener">' + esc(item.url) + '</a>']
    ];
    box.innerHTML = '<h3>' + esc(t().detail) + '</h3>' +
      '<dl class="kv">' + rows.map(function (r) {
        return '<dt>' + esc(r[0]) + '</dt><dd>' + r[1] + '</dd>';
      }).join('') + '</dl>' +
      '<label class="field">' + esc(t().copyOral) + '</label><pre>' + esc(item.oral_prompt || '') + '</pre>' +
      '<div class="modal-actions">' +
        '<button class="btn" type="button" data-role="copy-modal">' + esc(t().copyOral) + '</button>' +
        '<button class="btn primary" type="button" data-role="close-modal">' + esc(t().cancel) + '</button>' +
      '</div>';
    wrap.classList.remove('hidden');
    var copyBtn = box.querySelector('[data-role="copy-modal"]');
    if (copyBtn) { copyBtn.addEventListener('click', function () { A.copyText(item.oral_prompt || ''); }); }
    var closeBtn = box.querySelector('[data-role="close-modal"]');
    if (closeBtn) {
      closeBtn.addEventListener('click', function () {
        wrap.classList.add('hidden');
        box.innerHTML = '';
      });
    }
  }

  function findItem(card) {
    if (!card || !data) { return null; }
    var id = card.getAttribute('data-id');
    var items = data.items || [];
    for (var i = 0; i < items.length; i++) {
      if (String(items[i].id) === String(id)) { return items[i]; }
    }
    return null;
  }

  /* ---------------- 事件 ---------------- */

  function bindEvents() {
    if (eventsBound) { return; }
    eventsBound = true;

    // 搜索 / 清空放在右上角工具栏里
    var searchBtn = el('h-search');
    if (searchBtn) {
      searchBtn.addEventListener('click', function () { readInputs(); flt.page = 1; load(); });
    }
    var clearBtn = el('h-clear');
    if (clearBtn) {
      clearBtn.addEventListener('click', function () {
        flt = { date: '', module: '', cred: '', minScore: '', q: '', hidden: '', page: 1 };
        var dateEl = el('h-date'), qEl = el('h-q');
        if (dateEl) { dateEl.value = ''; }
        if (qEl) { qEl.value = ''; }
        var hiddenSel = el('h-hidden');
        if (hiddenSel) { hiddenSel.value = ''; }
        renderFilters();
        load();
      });
    }
    var todayBtn = el('h-today');
    if (todayBtn) {
      todayBtn.addEventListener('click', function () {
        var d = new Date();
        var iso = d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' +
          String(d.getDate()).padStart(2, '0');
        var dateEl = el('h-date');
        if (dateEl) { dateEl.value = iso; }
        flt.date = iso;
        flt.page = 1;
        load();
      });
    }

    [['h-date', 'date'], ['h-module', 'module'],
     ['h-cred', 'cred'], ['h-score', 'minScore'], ['h-hidden', 'hidden']].forEach(function (pair) {
      var node = el(pair[0]);
      if (!node) { return; }
      node.addEventListener('change', function () {
        readInputs();
        flt.page = 1;
        load();
      });
    });

    var qEl = el('h-q');
    if (qEl) {
      qEl.addEventListener('keydown', function (ev) {
        if (ev.key === 'Enter') { readInputs(); flt.page = 1; load(); }
      });
    }

    var prev = el('h-prev');
    if (prev) { prev.addEventListener('click', function () { if (flt.page > 1) { flt.page -= 1; load(); } }); }
    var next = el('h-next');
    if (next) {
      next.addEventListener('click', function () {
        var pages = (data && data.pages) || 1;
        if (flt.page < pages) { flt.page += 1; load(); }
      });
    }

    var cards = el('hcards');
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
        if (role === 'copy-oral') { A.copyText(item.oral_prompt || ''); }
        else if (role === 'unhide') { unhide(item); }
        else if (role === 'detail') { detailModal(item); }
      });
    }
  }

  /* spec v2.3：取消隐藏（恢复展示，同时回退那次点踩的权重） */
  function unhide(item) {
    return A.post('/api/news/' + item.id + '/unhide', {}).then(function (res) {
      if (!res || !res.ok) { A.toast((res && res.error) || t().failed); return; }
      item.hidden = false;
      item.user_vote = res.user_vote || 'none';
      A.toast(res.message || t().unhiddenToast);
      load();
    }).catch(function () { A.toast(t().failed); });
  }

  /* ---------------- 对外 ---------------- */

  A.registerView('history', {
    onShow: function () {
      if (!loaded) { load(); }        // 切标签不重新抓取，只在首次进入时读一次
    }
  });

  A.onLangChange(function () {
    renderFilters();
    if (data) { render(); }
  });

  document.addEventListener('DOMContentLoaded', function () {
    bindEvents();
    A.loadMeta().then(function (res) {          // 与今日页共用一份 /api/meta
      if (res && res.ok !== false) { meta = res; }
      renderFilters();
    }).catch(function () { renderFilters(); });
  });
})();
