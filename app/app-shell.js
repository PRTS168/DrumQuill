/* 应用外壳：侧栏导航 / 页面切换 / 谱面库计数 */
(function () {
  'use strict';
  var $ = function (id) { return document.getElementById(id); };
  var pages = { new: 'pageNew', library: 'pageLibrary' };
  var crumb = {
    new: ['新建扒谱', '把歌变成鼓谱'],
    library: ['谱面库', '已经扒过的曲子，点开就是可播放的谱面']
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
    var t = $('crumbTitle'), sub = $('crumbSub');
    if (t) {
      var c = crumb[name] || crumb.new;
      t.firstChild.textContent = c[0];
      if (sub) sub.textContent = c[1];
    }
    var m = document.querySelector('main');
    if (m) m.scrollTop = 0; else { try { window.scrollTo(0, 0); } catch (e) {} }
  }

  Array.prototype.forEach.call(document.querySelectorAll('.navitem[data-page]'), function (b) {
    b.addEventListener('click', function () { show(b.getAttribute('data-page')); });
  });

  var s1 = $('sideSettings');
  if (s1) s1.addEventListener('click', function () {
    var b = $('settingsBtn');
    if (b) b.click();
  });
  var s2 = $('sideDocs');
  if (s2) s2.addEventListener('click', function () {
    var a = $('footOpenDocs');
    if (a) a.click();
  });

  /* 谱面库条目数挂在侧栏上 */
  function refreshBadge() {
    fetch('/api/history').then(function (r) { return r.json(); }).then(function (list) {
      var n = (list && list.length) || 0;
      var b = $('navLib');
      if (b) b.textContent = n;
    }).catch(function () {});
  }
  setTimeout(refreshBadge, 1500);
  setInterval(refreshBadge, 15000);

  /* 任务完成 / 打开历史谱面时，确保用户停在「新建扒谱」这一页，否则会“打开但看不见” */
  function ensureNewPage() {
    var lib = pageLibrary;
    if (lib && !lib.hidden) show('new');
  }
  var done = viewDone;
  if (done && window.MutationObserver) {
    new MutationObserver(function () { if (!done.hidden) ensureNewPage(); })
      .observe(done, { attributes: true, attributeFilter: ['hidden'] });
  }

  window.dqShellShow = show;
  window.dqEnsureNewPage = ensureNewPage;
})();
