/* 应用外壳：侧栏导航 / 页面切换 / 谱面库计数 */
(function () {
  'use strict';
  if (window.__dqShell) return;
  window.__dqShell = 1;
  var $ = function (id) { return document.getElementById(id); };
  var pages = {
    new: 'pageNew',
    queue: 'pageQueue',
    library: 'pageLibrary',
    settings: 'setModal',
    setup: 'setupModal'
  };
  var crumb = {
    new: ['新建扒谱', '把歌变成鼓谱'],
    queue: ['任务队列', '批量排队，跑完一首自动接下一首'],
    library: ['谱面库', '点开就是可播放的谱面'],
    settings: ['设置', '组件 · 模型 · 路径'],
    setup: ['安装向导', '选组件 → 安装 → 完成']
  };

  function show(name) {
    if (!pages[name]) return;
    Object.keys(pages).forEach(function (k) {
      var el = $(pages[k]);
      if (el) el.hidden = (k !== name);
    });
    Array.prototype.forEach.call(document.querySelectorAll('.navitem[data-page]'), function (b) {
      var on = b.getAttribute('data-page') === name;
      b.classList.toggle('on', on);
      if (on) b.setAttribute('aria-current', 'page'); else b.removeAttribute('aria-current');
    });
    var nm = $('crumbName'), sub = $('crumbSub');
    var c = crumb[name] || crumb.new;
    if (nm) nm.textContent = c[0];
    if (sub) sub.textContent = c[1];
    var m = document.querySelector('main');
    if (m) m.scrollTop = 0; else { try { window.scrollTo(0, 0); } catch (e) {} }
    if (name === 'library') refreshLib();          /* 每次进库都重新读一次产物目录 */
    if (name === 'settings') {
      /* 别用「点按钮」驱动（会和 openSettings 互相递归）；直接刷新这一页要的数据 */
      if (window.dqLoadSetup) window.dqLoadSetup();
      if (window.dqLoadModels) window.dqLoadModels();
    }
    if (name === 'queue') refreshQueueBadge();
  }

  /* 刷新谱面库（列表 + 侧栏计数） */
  function refreshLib() {
    if (window.dqLoadHistory) window.dqLoadHistory();
    refreshBadge();
  }

  Array.prototype.forEach.call(document.querySelectorAll('.navitem[data-page]'), function (b) {
    b.addEventListener('click', function () { show(b.getAttribute('data-page')); });
  });

  var s1 = $('sideSettings');   /* 已改成 data-page="settings" 的导航项，这里只做兜底 */
  if (s1 && !s1.getAttribute('data-page')) {
    s1.addEventListener('click', function () { show('settings'); });
  }
  var sc1 = $('setClose');
  if (sc1) sc1.addEventListener('click', function () { show('new'); });
  var sc2 = $('setupClose');
  if (sc2) sc2.addEventListener('click', function () { show('new'); });
  var sd = $('setupDoneBtn');
  if (sd) sd.addEventListener('click', function () { show('new'); });
  var s2 = $('sideDocs');
  if (s2) s2.addEventListener('click', function () {
    var a = $('footOpenDocs');
    if (a) a.click();
  });

  /* 队列待办数挂在侧栏上 */
  function refreshQueueBadge() {
    if (document.hidden) return;
    fetch('/api/status', { cache: 'no-store' }).then(function (r) { return r.json(); }).then(function (st) {
      var q = (st && st.queue) || {};
      var items = (q && q.items) || [];
      var left = items.filter(function (it) { return it.state === 'pending' || it.state === 'running'; }).length;
      var b = $('navQueue');
      if (b) b.textContent = left;
      var e = $('queueEta');
      if (e) {
        var eta = st && st.eta_sec;
        e.textContent = eta ? ('预计还要 ' + Math.max(1, Math.round(eta / 60)) + ' 分钟') : '';
      }
    }).catch(function () {});
  }

  /* 谱面库条目数挂在侧栏上 */
  function refreshBadge() {
    if (document.hidden) return;
    fetch('/api/history?t=' + Date.now(), { cache: 'no-store' })
      .then(function (r) { return r.json(); }).then(function (list) {
      var n = (list && list.length) || 0;
      var b = $('navLib');
      if (b) b.textContent = n;
    }).catch(function () {});
  }
  setTimeout(refreshBadge, 1500);
  setTimeout(refreshQueueBadge, 1800);
  setInterval(function () {
    if (document.hidden) return;
    refreshBadge();
    refreshQueueBadge();
  }, 10000);

  /* 任务完成 / 打开历史谱面时，确保用户停在「新建扒谱」这一页，否则会“打开但看不见” */
  function ensureNewPage() {
    var lib = $('pageLibrary');
    if (lib && !lib.hidden) show('new');
  }
  var done = $('viewDone');
  if (done && window.MutationObserver) {
    new MutationObserver(function () { if (!done.hidden) ensureNewPage(); })
      .observe(done, { attributes: true, attributeFilter: ['hidden'] });
  }

  var rb = $('libRefresh');
  if (rb) rb.addEventListener('click', function () { refreshLib(); });

  document.addEventListener('keydown', function (ev) {
    if (ev.key !== 'r' && ev.key !== 'R') return;
    if (ev.ctrlKey || ev.metaKey || ev.altKey) return;
    var t = ev.target;
    if (t && t.closest && t.closest('input,textarea,select,[contenteditable]')) return;
    var lib = $('pageLibrary');
    if (lib && !lib.hidden) { ev.preventDefault(); refreshLib(); }
  });


  /* ============================ 命令面板（Ctrl/⌘ + K）============================ */
  var cmds = [
    { k: 'new',     t: '新建扒谱',   s: '选歌、设参数、开始',      run: function () { show('new'); } },
    { k: 'queue',   t: '任务队列',   s: '批量排队与进度',          run: function () { show('queue'); } },
    { k: 'library', t: '谱面库',     s: '已扒过的曲子',            run: function () { show('library'); } },
    { k: 'settings',t: '设置',       s: '组件 / 模型 / 路径',      run: function () { show('settings'); } },
    { k: 'setup',   t: '安装向导',   s: '补装缺失组件',            run: function () { show('setup'); } },
    { k: 'refresh', t: '刷新谱面库', s: '重新读取产物目录',        run: function () { refreshLib(); } },
    { k: 'focus',   t: '切换聚焦模式', s: '隐藏侧栏只看谱面',      run: function () { var b = $('dqFocus'); if (b) b.click(); } },
    { k: 'pdf',     t: '打开鼓谱 PDF', s: '用系统阅读器打开',      run: function () { var b = $('openPdfBtn'); if (b) b.click(); } }
  ];
  var ck = { open: false, sel: 0, list: [] };

  function cmdkEls() {
    return { box: $('cmdk'), input: $('cmdkInput'), list: $('cmdkList'), prev: ck.prevFocus };
  }

  function cmdkRender() {
    var e = cmdkEls();
    if (!e.list) return;
    if (!ck.list.length) {
      e.list.innerHTML = '<div class="cmdk-empty" role="presentation">没有匹配的命令</div>';
      if (e.input) e.input.removeAttribute('aria-activedescendant');
      return;
    }
    e.list.innerHTML = ck.list.map(function (c, i) {
      return '<div class="cmdk-item' + (i === ck.sel ? ' on' : '') + '" role="option" data-i="' + i + '"' +
        ' id="cmdkOpt' + i + '" aria-selected="' + (i === ck.sel ? 'true' : 'false') + '">' +
        '<span class="ci-t">' + c.t + '</span><span class="ci-s">' + (c.s || '') + '</span></div>';
    }).join('');
    if (e.input) e.input.setAttribute('aria-activedescendant', 'cmdkOpt' + ck.sel);
    Array.prototype.forEach.call(e.list.querySelectorAll('.cmdk-item'), function (node) {
      node.addEventListener('mousedown', function (ev) {
        ev.preventDefault();
        ck.sel = +node.getAttribute('data-i');
        cmdkRun();
      });
    });
  }

  function cmdkFilter(q) {
    var s2 = (q || '').trim().toLowerCase();
    ck.list = cmds.filter(function (c) {
      if (!s2) return true;
      return (c.t + ' ' + (c.s || '') + ' ' + c.k).toLowerCase().indexOf(s2) >= 0;
    });
    ck.sel = 0;
    cmdkRender();
  }

  function cmdkOpen() {
    var e = cmdkEls();
    if (!e.box) return;
    ck.prevFocus = document.activeElement;
    ck.open = true;
    window.__dqCmdkOpen = true;
    e.box.hidden = false;
    if (e.input) { e.input.setAttribute('aria-expanded', 'true'); }
    if (e.input) { e.input.value = ''; e.input.focus(); }
    cmdkFilter('');
  }

  function cmdkClose() {
    var e = cmdkEls();
    if (!e.box) return;
    ck.open = false;
    window.__dqCmdkOpen = false;
    e.box.hidden = true;
    if (e.input) { e.input.setAttribute('aria-expanded', 'false'); }
    if (ck.prevFocus && ck.prevFocus.focus) { try { ck.prevFocus.focus(); } catch (x) {} }
  }

  function cmdkRun() {
    var c = ck.list[ck.sel];
    if (!c) {
      var f = document.querySelector('.cmdk-foot');
      if (f) { f.textContent = '没有匹配的命令'; setTimeout(function(){ f.textContent = '↑↓ 选择 · Enter 执行 · Esc 关闭 · Ctrl/⌘ + K 唤起'; }, 1400); }
      return;
    }
    cmdkClose();
    setTimeout(function () { c.run(); }, 0);
  }

  function bindCmdk() {
    var e = cmdkEls();
    if (!e.box || e.box.dataset.b) return;
    e.box.dataset.b = '1';
    e.box.addEventListener('mousedown', function (ev) { if (ev.target === e.box) cmdkClose(); });
    if (e.input) {
      e.input.addEventListener('input', function () { cmdkFilter(e.input.value); });
      e.input.setAttribute('role', 'combobox');
      e.input.setAttribute('aria-expanded', 'false');
      e.input.setAttribute('aria-controls', 'cmdkList');
      e.input.setAttribute('aria-autocomplete', 'list');
      e.input.addEventListener('keydown', function (ev) {
        if (ev.key === 'ArrowDown') { ev.preventDefault(); ev.stopPropagation(); if (ck.list.length) { ck.sel = Math.min(ck.list.length - 1, ck.sel + 1); cmdkRender(); } }
        else if (ev.key === 'ArrowUp') { ev.preventDefault(); ev.stopPropagation(); if (ck.list.length) { ck.sel = Math.max(0, ck.sel - 1); cmdkRender(); } }
        else if (ev.key === 'Enter') { ev.preventDefault(); ev.stopPropagation(); cmdkRun(); }
        else if (ev.key === 'Escape') { ev.preventDefault(); ev.stopPropagation(); cmdkClose(); }
      });
      /* 面板任意位置点一下都把焦点送回输入框，避免空格穿透到播放器 */
      var boxEl = $('cmdk');
      if (boxEl) boxEl.addEventListener('mousedown', function (ev) {
        if (ev.target === boxEl || (ev.target.classList && ev.target.classList.contains('cmdk-box'))) {
          ev.preventDefault();
          e.input.focus();
        }
      });
    }
  }

  document.addEventListener('keydown', function (ev) {
    var key = (ev.key || '').toLowerCase();
    if ((ev.ctrlKey || ev.metaKey) && key === 'k') {
      ev.preventDefault();
      if (ck.open) cmdkClose(); else cmdkOpen();
      bindCmdk();
      return;
    }
    if (ev.key === 'Escape' && ck.open) { ev.preventDefault(); ev.stopPropagation(); cmdkClose(); }
  });

  setTimeout(bindCmdk, 800);
  window.dqCmdk = cmdkOpen;

  window.dqShellShow = show;
  window.dqOpenSettings = function () { show('settings'); };
  window.dqOpenSetup = function () { show('setup'); };
  window.dqEnsureNewPage = ensureNewPage;
  window.dqRefreshLib = refreshLib;
})();
