/* ===== 谱面工作台：就是导出那张 PDF，绿色播放头跟着音乐走 ===== */
(function () {
  'use strict';
  var el = function (id) { return document.getElementById(id); };
  var st = {
    song: '', bars: [], layout: null, page: 1, zoom: 'fit', src: 'mix',
    key: '', playing: false, explicit: false, cols: [], pageHold: 0, failAt: 0
  };
  var DPI = { fit: 150, page: 200, hd: 220, big: 320 };

  /* ---------- 工具 ---------- */
  function curAudio() { return el(st.src === 'drum' ? 'resAudio' : 'dqAudio'); }
  function time() { var a = curAudio(); return a ? (a.currentTime || 0) : 0; }
  function mmss(t) {
    t = Math.max(0, t || 0);
    var m = Math.floor(t / 60), s = Math.floor(t - m * 60);
    return m + ':' + (s < 10 ? '0' : '') + s;
  }
  function totalDur() {
    var a = curAudio();
    if (a && a.duration && isFinite(a.duration)) return a.duration;
    var b = st.bars[st.bars.length - 1];
    return b ? (b.end ? b.end : b.start + 2) : 0;
  }
  function barAt(t) {
    var n = st.bars.length;
    if (!n) return null;
    for (var i = 0; i < n; i++) {
      var b = st.bars[i], s = b.start, e = b.end ? b.end : (s + 2);
      if (t >= s && t < e) return { i: i, frac: Math.min(0.999, (t - s) / Math.max(0.15, e - s)) };
    }
    if (t < st.bars[0].start) return { i: 0, frac: 0 };
    var last = st.bars[n - 1];
    if (t >= (last.end ? last.end : last.start + 2) - 0.02) return { i: n - 1, frac: 0.999 };
    for (var j = n - 1; j >= 0; j--) {
      if (t >= st.bars[j].start) return { i: j, frac: 0.999 };
    }
    return { i: 0, frac: 0 };
  }
  function barGeo(idx) {
    if (!st.layout) return null;
    var n = 0;
    for (var p = 0; p < st.layout.pages.length; p++) {
      var pg = st.layout.pages[p];
      for (var si = 0; si < pg.systems.length; si++) {
        var sy = pg.systems[si], bn = sy.bounds.length - 1;
        if (idx >= n && idx < n + bn) {
          var k = idx - n;
          return { page: p + 1, top: sy.top, bottom: sy.bottom, x0: sy.bounds[k], x1: sy.bounds[k + 1] };
        }
        n += bn;
      }
    }
    return null;
  }

  /* ---------- 谱面坐标 ---------- */
  /* MuseScore 按音符时值排版，同一小节里各拍宽度并不相等；
     所以不能按时间线性铺满小节，必须用谱面上音符列的真实 x。 */
  function buildCols() {
    st.cols = [];
    st.colsBad = 0;
    st.colsApprox = 0;
    st.colsMerged = 0;
    if (!st.layout) return;
    var bar = 0;
    for (var p = 0; p < st.layout.pages.length; p++) {
      var pg = st.layout.pages[p];
      for (var si = 0; si < pg.systems.length; si++) {
        var sy = pg.systems[si], per = sy.bars_nx || [], perX = sy.bars_x || [];
        for (var k = 0; k < sy.bounds.length - 1; k++) {
          if (bar >= st.bars.length) break;
          var seen = {}, notes = st.bars[bar].notes || [];
          for (var n = 0; n < notes.length; n++) seen[Math.max(0, Math.min(15, notes[n].p | 0))] = 1;
          var ps = Object.keys(seen).map(Number).sort(function (a, b) { return a - b; });
          /* 优先用原始符头 x：数量多于拍位数时，反复合并最近的一对，直到正好对上 */
          var rawX = (perX[k] || []).slice();
          var seg = rawX.length ? rawX : (per[k] || []).slice();
          if (seg.length > ps.length && ps.length) {
            seg = seg.slice();
            while (seg.length > ps.length && seg.length > 1) {
              var bi2 = 1, bd2 = Infinity;
              for (var t2 = 1; t2 < seg.length; t2++) {
                var g2 = seg[t2] - seg[t2 - 1];
                if (g2 < bd2) { bd2 = g2; bi2 = t2; }
              }
              seg[bi2 - 1] = (seg[bi2 - 1] + seg[bi2]) / 2;
              seg.splice(bi2, 1);
              st.colsMerged++;
            }
          }
          var good = (seg.length === ps.length);
          /* 拍位个数和谱上音符列数对不上时，按比例挑锚点（保持单调），不要整小节退回线性 */
          if (!good && seg.length && ps.length) {
            var mapped = [];
            for (var q2 = 0; q2 < ps.length; q2++) {
              var j2 = ps.length > 1 ? Math.round(q2 * (seg.length - 1) / (ps.length - 1)) : 0;
              mapped.push(seg[Math.min(seg.length - 1, j2)]);
            }
            seg = mapped;
            st.colsApprox++;
          }
          for (var q = 1; q < seg.length; q++) {
            if (seg[q] <= seg[q - 1]) seg[q] = seg[q - 1] + 0.5;      /* 强制单调 */
          }
          st.cols[bar] = (ps.length && seg.length) ? { ps: ps, xs: seg } : null;
          if (!good) st.colsBad++;
          bar++;
        }
      }
    }
  }

  /* 小节内第 p 个十六分位（0..16，可带小数）对应的谱面 x */
  function xAtP(bi, p) {
    var g = barGeo(bi), c = st.cols && st.cols[bi];
    if (!g) return null;
    if (c && c.ps.length && c.xs && c.xs.length) {
      var ps = c.ps, xs = c.xs, j;
      if (p <= ps[0]) {
        if (ps[0] <= 0.0001) return xs[0];
        var f0 = p / ps[0];
        return g.x0 + (xs[0] - g.x0) * Math.max(0, Math.min(1, f0));
      }
      for (j = 0; j < ps.length - 1; j++) {
        if (p < ps[j + 1]) {
          var u = (p - ps[j]) / Math.max(0.001, ps[j + 1] - ps[j]);
          return xs[j] + (xs[j + 1] - xs[j]) * u;
        }
      }
      if (p >= ps[ps.length - 1]) {
        var f1 = (p - ps[ps.length - 1]) / Math.max(2, 16 - ps[ps.length - 1]);
        return xs[xs.length - 1] + (g.x1 - xs[xs.length - 1]) * Math.max(0, Math.min(1, f1));
      }
    }
    return g.x0 + (p / 16) * (g.x1 - g.x0);
  }

  /* ---------- 打印版 PDF ---------- */
  function pageUrl(page) {
    return '/api/pdf/page?song=' + encodeURIComponent(st.song) +
      '&page=' + page + '&dpi=' + DPI[st.zoom] + '&v=' + encodeURIComponent(st.zoom);
  }
  /* 先把图下到内存再换，翻页时不闪、也不会把光标画到旧页上。
     用令牌而不是比对 src：同一 src 反复设置时也要能把 loaded 补回来。 */
  function setPageImage(src, key) {
    var img = el('dqPageImg');
    if (img.dataset.srcKey === key && img.dataset.loaded === '1') return;
    img.dataset.srcKey = key;
    img.dataset.loaded = '';           /* 新页还没到位：这段时间不要拿旧图的尺寸算位置 */
    var pre = new Image();
    pre.onload = function () {
      if (img.dataset.srcKey !== key) return;      /* 期间又换了页，丢掉这次结果 */
      img.setAttribute('src', src);
      img.dataset.loaded = '1';
      img.dataset.loadPage = String(st.page);
      paintPdf();
    };
    pre.onerror = function () {
      if (img.dataset.srcKey !== key) return;
      img.setAttribute('src', src);
      img.dataset.loaded = '1';
      img.dataset.loadPage = String(st.page);
    };
    pre.src = src;
  }
  var preloaded = {};
  function preloadNext() {
    if (!st.layout) return;
    var n = st.page + 1;
    if (n > st.layout.pages.length) return;
    var u = pageUrl(n);
    if (preloaded[u]) return;
    preloaded[u] = 1;
    var im = new Image();
    im.src = u;
  }

  function renderPdf() {
    var pg = st.layout && st.layout.pages[st.page - 1];
    if (!pg) return;
    var ov = el('dqOverlay');
    setPageImage(pageUrl(st.page), st.song + '|' + st.page + '|' + st.zoom);
    ov.setAttribute('viewBox', '0 0 ' + pg.w + ' ' + pg.h);
    el('dqPageLabel').textContent = st.page + ' / ' + st.layout.pages.length;
    el('dqPrevPage').disabled = st.page <= 1;
    el('dqNextPage').disabled = st.page >= st.layout.pages.length;
    el('dqPaper').classList.toggle('zoomed', st.zoom === 'big');
    el('dqPaper').classList.toggle('pagefit', st.zoom === 'page');
    var img2 = el('dqPageImg');
    var big = st.zoom === 'big';
    img2.style.width = st.zoom === 'page' ? 'auto' : (big ? '190%' : '100%');
    img2.style.height = st.zoom === 'page' ? '100%' : '';
    ov.style.width = big ? '190%' : '100%';
    preloadNext();
  }

  /* 吸顶区实际高度（顶栏 + 播放条），窄屏播放条不吸顶所以只算顶栏 */
  function stickyOffset() {
    var h = document.querySelector('header.top');
    var t = document.querySelector('.transport');
    var hh = h ? h.getBoundingClientRect().height : 56;
    var th = 0;
    if (t && getComputedStyle(t).position === 'sticky') th = t.getBoundingClientRect().height;
    return Math.round(hh + th);
  }

  /* ---- 柔和跟随滚动 ----
     不用 CSS 的 smooth（它会「设定目标后一次性溜过去」，密集段落看起来一顿一顿），
     改成每帧朝目标挪一点点：缓动系数 + 每帧位移上限，接近时自然收住。 */
  var sc = { goal: null, raf: 0, busy: false, userHold: 0, chasing: false };

  /* 页面滚动容器：新外壳里是 <main>，不再是 window */
  function scroller() {
    return document.querySelector('main') || document.documentElement;
  }
  function scrollTopNow() { var m = scroller(); return m === document.documentElement ? scrollTopNow() : m.scrollTop; }
  function viewH() { var m = scroller(); return m === document.documentElement ? viewH() : m.clientHeight; }
  function scrollMaxH() { var m = scroller(); return Math.max(0, m.scrollHeight - viewH()); }
  var disp = { x: null, bar: -1, last: 0 };   /* 平滑后的光标位置 */
  function maxScroll() {
    var d = document.documentElement;
    return Math.max(0, (d.scrollHeight || 0) - viewH());
  }
  function stopScrollAnim() {
    if (sc.raf) { cancelAnimationFrame(sc.raf); sc.raf = 0; }
    sc.goal = null;
    sc.busy = false;
  }
  function easeScrollTo(goal, maxStep) {
    goal = Math.max(0, Math.min(goal, maxScroll()));
    sc.goal = goal;
    if (sc.raf) return;
    var cap = maxStep || 12;                 /* 每帧最多 12px ≈ 720px/s */
    var step = function () {
      if (sc.goal == null) { sc.raf = 0; sc.busy = false; return; }
      var d = sc.goal - scrollTopNow();
      if (Math.abs(d) < 0.7) { sc.goal = null; sc.raf = 0; sc.busy = false; return; }
      var delta = d * 0.13;
      if (Math.abs(delta) > cap) delta = (delta > 0 ? 1 : -1) * cap;
      /* 取整：浏览器 scrollY 是整数，亚像素步进会造成 ±1px 抖动 */
      var stepPx = Math.round(delta);
      if (stepPx === 0) stepPx = (d > 0 ? 1 : -1);
      sc.busy = true;
      scroller().scrollBy(0, stepPx);
      sc.raf = requestAnimationFrame(step);
    };
    sc.raf = requestAnimationFrame(step);
  }

  /* 当前系统行在文档里的位置与高度（CSS 像素） */
  function systemDocRect(g) {
    var img = el('dqPageImg'), pg = st.layout && st.layout.pages[st.page - 1];
    if (!g || !img || !pg) return null;
    var r = img.getBoundingClientRect();
    if (!r.height) return null;
    var k = r.height / pg.h;
    var top = r.top + scrollTopNow() + g.top * k;
    return { top: top, height: (g.bottom - g.top) * k, k: k };
  }

  /* 视口锚点：让当前小节的行中心停在这里（吸顶条下面的可视区中部） */
  function followAnchor() {
    var visTop = stickyOffset() + 12;
    var visH = Math.max(140, viewH() - visTop - 28);
    return { y: visTop + visH * 0.44, visH: visH, visTop: visTop };
  }

  /* 柔和跟随：目标每帧按「当前行中心 vs 锚点」重算，
     偏差小就完全不动（死区），偏差大就一点点追过去，不会冲过头再拉回来。 */
  function scrollToSystem(g, force) {
    if (!g) return;
    var img = el('dqPageImg');
    if (!img || img.dataset.loadPage !== String(st.page)) return;
    var dr = systemDocRect(g);
    if (!dr) return;
    var now = (window.performance && performance.now) ? performance.now() : Date.now();
    if (sc.userHold && now < sc.userHold) return;      /* 用户刚自己滚过，别抢 */
    var an = followAnchor();
    var centerInView = dr.top + dr.height / 2 - scrollTopNow();
    var delta = centerInView - an.y;                   /* >0 表示行中心偏下，要往下滚 */
    var dead = Math.max(16, Math.min(58, an.visH * 0.10));
    var small = 10;
    if (force) {
      sc.chasing = false;
    } else if (!sc.chasing) {
      if (Math.abs(delta) < dead) {                    /* 还在舒适区：不动 */
        if (sc.raf) stopScrollAnim();
        return;
      }
      sc.chasing = true;                               /* 越过死区：开始追到正中 */
    } else if (Math.abs(delta) < small) {
      sc.chasing = false;
      stopScrollAnim();
      return;
    }
    var cap = Math.min(20, Math.max(9, Math.abs(delta) * 0.22));
    easeScrollTo(scrollTopNow() + delta, force ? 26 : cap);
  }

  /* 用户主动滚动/滚轮/触屏时，立刻停掉我们的动画，并让出 1.2 秒 */
  function watchUserScroll() {
    if (window.__dqScrollWatch) return;
    window.__dqScrollWatch = 1;
    var yieldToUser = function () {
      if (sc.busy) return;                 /* 是我们自己在滚，不算用户 */
      sc.chasing = false;
      stopScrollAnim();
      sc.userHold = ((window.performance && performance.now) ? performance.now() : Date.now()) + 1200;
    };
    window.addEventListener('wheel', yieldToUser, { passive: true });
    window.addEventListener('touchmove', yieldToUser, { passive: true });
    window.addEventListener('keydown', function (ev) {
      if (/^(PageDown|PageUp|Home|End|ArrowUp|ArrowDown| )$/.test(ev.key)) yieldToUser();
    });
  }

  /* 光标平滑：只能往前走 + 限制瞬时速度（列间距最大差 4 倍，直插会忽快忽慢） */
  function smoothX(bi, target, g) {
    if (!isFinite(target)) { disp.x = null; return null; }
    var now = (window.performance && performance.now) ? performance.now() : Date.now();
    var dt = disp.last ? Math.min(0.12, (now - disp.last) / 1000) : 0.016;
    disp.last = now;
    var w = g.x1 - g.x0;
    if (!st.playing || disp.x == null || disp.bar !== bi || Math.abs(target - disp.x) > w * 0.5) {
      disp.bar = bi;
      disp.x = target;
      return target;
    }
    var bar = st.bars[bi];
    var barDur = Math.max(0.25, (bar.end || bar.start + 2) - bar.start);
    var vAvg = w / barDur;
    var need = target - disp.x;
    if (need > 0) {
      var step = Math.min(need, Math.max(vAvg * 2.4 * dt, need * Math.min(1, 8 * dt)));
      disp.x += step;
    } else if (need < -w * 0.5) {
      disp.x = target;
    }
    return disp.x;
  }

  function paintPdf() {
    var ov = el('dqOverlay');
    if (!ov || !st.layout) return;
    var hit = barAt(time());
    if (!hit) { ov.innerHTML = ''; return; }
    st.curBar = hit.i;
    var g = barGeo(hit.i);
    if (!g) { ov.innerHTML = ''; return; }
    if (g.page !== st.page) {
      /* 播放中自动翻页；但用户刚自己翻过页就让他多看一会儿，别抢 */
      var held = st.pageHold && ((window.performance ? performance.now() : Date.now()) < st.pageHold);
      if (!held) {
        st.page = g.page;
        st.pageTurn = true;      /* 翻页后直接落位，不要倒着滑回去 */
        renderPdf();
      }
      return;                     /* 等新页的图就绪后 setPageImage 会再画一次 */
    }
    var pageImg = el('dqPageImg');
    if (pageImg.dataset.loaded !== '1') return;
    if (pageImg.dataset.loadPage !== String(st.page)) return;   /* 图还是上一页，别拿它算坐标 */
    if (st.pageTurn) {
      st.pageTurn = false;
      sc.chasing = false;
      sc.row = hit.i;
      stopScrollAnim();
      snapFollow();                /* 换页：一次落位，避免倒着滑一屏 */
    } else if (st.playing) {
      scrollToSystem(g, false);    /* 播放中柔和跟随 */
    }
    var pg = st.layout.pages[st.page - 1];
    var b = st.bars[hit.i];
    if (b) {
      el('dqBarNo').textContent = hit.i + 1;
      el('dqBarTime').textContent = ' · ' + mmss(b.start) + '–' + mmss(b.end ? b.end : b.start + 2);
    }
    var pad = (g.bottom - g.top) * 0.24;
    var h = g.bottom - g.top + pad * 2, y = g.top - pad;
    /* 用谱上真实音符列定位（拍内按时间插值），这样光标不会随小节越走越偏 */
    var bi = Math.min(3, Math.floor(hit.frac * 4));
    var bx = xAtP(hit.i, bi * 4);
    var bx2 = xAtP(hit.i, bi * 4 + 4);
    var xTarget = xAtP(hit.i, hit.frac * 16);
    if (bx == null || xTarget == null || !isFinite(xTarget)) return;
    var x = smoothX(hit.i, xTarget, g);
    if (x == null || !isFinite(x)) return;
    if (bx2 == null) bx2 = bx + (g.x1 - g.x0) / 4;
    var beat = Math.max(8, bx2 - bx);
    ov.innerHTML =
      '<rect x="24" y="' + y + '" width="' + (pg.w - 48) + '" height="' + h + '" fill="rgba(34,197,94,0.05)"/>' +
      '<rect x="' + g.x0 + '" y="' + y + '" width="' + (g.x1 - g.x0) + '" height="' + h + '" fill="rgba(34,197,94,0.10)"/>' +
      '<rect x="' + bx.toFixed(1) + '" y="' + y + '" width="' + beat.toFixed(1) + '" height="' + h + '" fill="rgba(34,197,94,0.26)"/>' +
      '<rect x="' + (x - 0.8).toFixed(1) + '" y="' + y + '" width="1.6" height="' + h + '" fill="#16a34a"/>' +
      '<polygon points="' + x.toFixed(1) + ',' + y + ' ' + (x - 4.5).toFixed(1) + ',' + (y - 7) + ' ' +
      (x + 4.5).toFixed(1) + ',' + (y - 7) + '" fill="#16a34a"/>';
  }

  /* ---------- 播放 ---------- */
  var raf = 0;
  function loop() { raf = requestAnimationFrame(loop); paint(); }
  function startLoop() { if (!raf) loop(); }
  function stopLoop() { if (raf) { cancelAnimationFrame(raf); raf = 0; } }

  var ticking = 0;
  function paint() {
    var t = time(), dur = totalDur();
    var f = dur ? Math.min(1, t / dur) : 0;
    var fill = el('dqFill'), head = el('dqHeadPtr');
    if (fill) fill.style.width = (f * 100) + '%';
    if (head) head.style.left = (f * 100) + '%';
    el('dqCur').textContent = mmss(t);
    el('dqDur').textContent = mmss(dur);
    paintPdf();
  }
  function syncPlay() {
    var a = curAudio(), on = !!(a && !a.paused);
    st.playing = on;
    el('dqPlay').textContent = on ? '❚❚' : '▶';
    if (on) startLoop(); else { stopLoop(); paint(); }
  }
  function bindAudio(a) {
    if (!a || a.dataset.dq) return;
    a.dataset.dq = '1';
    a.addEventListener('play', syncPlay);
    a.addEventListener('pause', syncPlay);
    a.addEventListener('ended', syncPlay);
    a.addEventListener('timeupdate', function () { if (!st.playing) paint(); });
    a.addEventListener('loadedmetadata', paint);
    a.addEventListener('durationchange', paint);
    a.addEventListener('error', function () {
      if (a === curAudio() && el('dqWarn')) {
        el('dqWarn').textContent = '⚠ 音频没能加载，播放头不会动（谱面仍可点选定位）。可在设置里检查音频源。';
        el('dqWarn').hidden = false;
      }
    });
  }
  function togglePlay() {
    var a = curAudio();
    if (!a || !a.getAttribute('src')) return;
    if (!a.paused) { a.pause(); return; }
    var p = a.play();
    if (p && p.catch) p.catch(function () {});
  }
  function seek(t) {
    var a = curAudio();
    if (!a) return;
    try { a.currentTime = Math.max(0, t); } catch (e) {}
    disp.x = null;
    disp.last = 0;
    paint();
    snapFollow();
  }

  /* 跳跃式定位：直接落到锚点（平滑只用于自动跟随） */
  function snapFollow() {
    var hit = barAt(time());
    if (!hit) return;
    var g = barGeo(hit.i);
    if (!g || g.page !== st.page) return;
    var img = el('dqPageImg');
    if (!img || img.dataset.loadPage !== String(st.page)) return;
    var dr = systemDocRect(g);
    if (!dr) return;
    var an = followAnchor();
    var goal = Math.max(0, Math.min(dr.top + dr.height / 2 - an.y, maxScroll()));
    stopScrollAnim();
    var m2 = scroller();
    if (m2 === document.documentElement) window.scrollTo(0, goal); else m2.scrollTop = goal;
  }

  /* ---------- 播放 ---------- */
  var raf = 0;
  function loop() { raf = requestAnimationFrame(loop); paint(); }
  function startLoop() { if (!raf) loop(); }
  function stopLoop() { if (raf) { cancelAnimationFrame(raf); raf = 0; } }

  var ticking = 0;
  function paint() {
    var t = time(), dur = totalDur();
    var f = dur ? Math.min(1, t / dur) : 0;
    var fill = el('dqFill'), head = el('dqHeadPtr');
    if (fill) fill.style.width = (f * 100) + '%';
    if (head) head.style.left = (f * 100) + '%';
    el('dqCur').textContent = mmss(t);
    el('dqDur').textContent = mmss(dur);
    paintPdf();
  }
  function syncPlay() {
    var a = curAudio(), on = !!(a && !a.paused);
    st.playing = on;
    el('dqPlay').textContent = on ? '❚❚' : '▶';
    if (on) startLoop(); else { stopLoop(); paint(); }
  }
  function bindAudio(a) {
    if (!a || a.dataset.dq) return;
    a.dataset.dq = '1';
    a.addEventListener('play', syncPlay);
    a.addEventListener('pause', syncPlay);
    a.addEventListener('ended', syncPlay);
    a.addEventListener('timeupdate', function () { if (!st.playing) paint(); });
    a.addEventListener('loadedmetadata', paint);
    a.addEventListener('durationchange', paint);
    a.addEventListener('error', function () {
      if (a === curAudio() && el('dqWarn')) {
        el('dqWarn').textContent = '⚠ 音频没能加载，播放头不会动（谱面仍可点选定位）。可在设置里检查音频源。';
        el('dqWarn').hidden = false;
      }
    });
  }
  function togglePlay() {
    var a = curAudio();
    if (!a || !a.getAttribute('src')) return;
    if (!a.paused) { a.pause(); return; }
    var p = a.play();
    if (p && p.catch) p.catch(function () {});
  }
  function seek(t) {
    var a = curAudio();
    if (!a) return;
    try { a.currentTime = Math.max(0, t); } catch (e) {}
    disp.x = null;
    disp.last = 0;
    paint();
    snapFollow();
  }

  /* 跳跃式定位：直接落到目标位置（平滑只用于自动跟随） */
  function snapFollow() {
    var hit = barAt(time());
    if (!hit) return;
    var g = barGeo(hit.i);
    if (!g || g.page !== st.page) return;
    var img = el('dqPageImg');
    if (!img || img.dataset.loadPage !== String(st.page)) return;
    var dr = systemDocRect(g);
    if (!dr) return;
    var safeTop = stickyOffset() + 22;
    var safeBottom = viewH() - 96;
    var y = dr.top - scrollTopNow();
    if (y < safeTop || y + dr.height > safeBottom) {
      stopScrollAnim();
      var goal = Math.max(0, Math.min(dr.top - safeTop - 6, maxScroll()));
      window.scrollTo(0, goal);
    }
  }
  /* 点小节：落到最近的音符列，而不是小节开头（更符合「我想听这里」的直觉） */
  function seekInBar(idx, px) {
    var bar = st.bars[idx];
    if (!bar) return;
    var s = bar.start, e = bar.end ? bar.end : s + 2;
    var t = s;
    var c = st.cols && st.cols[idx];
    var g0 = barGeo(idx);
    if (c && c.xs && c.xs.length && px != null) {
      if (px <= c.xs[0] || px >= c.xs[c.xs.length - 1]) {
        /* 点在最后一列之后（小节尾部留白）或第一列之前：按小节内比例算 */
        var fr = g0 ? Math.max(0, Math.min(1, (px - g0.x0) / Math.max(1e-3, g0.x1 - g0.x0))) : 0;
        t = s + fr * (e - s);
      } else {
        var best = 0, bd = 1e9;
        for (var i = 0; i < c.xs.length; i++) {
          var d = Math.abs(c.xs[i] - px);
          if (d < bd) { bd = d; best = i; }
        }
        t = s + (c.ps[best] / 16) * (e - s);
      }
    } else if (g0 && px != null) {
      var fr2 = Math.max(0, Math.min(1, (px - g0.x0) / Math.max(1e-3, g0.x1 - g0.x0)));
      t = s + fr2 * (e - s);
    }
    var playing = st.playing;
    seek(Math.max(0, t));
    var g = barGeo(idx);
    if (g) scrollToSystem(g, true);
    if (playing) { var a = curAudio(); if (a && a.paused) { var pr = a.play(); if (pr && pr.catch) pr.catch(function () {}); } }
  }

  function goBar(idx) {
    if (idx < 0 || idx >= st.bars.length) return;
    var playing = st.playing;
    seek(st.bars[idx].start + 0.02);
    var gg = barGeo(idx);
    if (gg) scrollToSystem(gg, true);
    if (playing) { var a = curAudio(); if (a && a.paused) { var p = a.play(); if (p && p.catch) p.catch(function () {}); } }
  }
  function switchSrc(which) {
    if (which === st.src) return;
    if (which === 'drum' && !el('resAudio').getAttribute('src')) return;
    var from = curAudio(), t = from ? (from.currentTime || 0) : 0;
    var wasPlaying = !!(from && !from.paused);
    if (from) from.pause();
    st.src = which;
    Array.prototype.forEach.call(document.querySelectorAll('#dqSrc button'), function (b) {
      b.classList.toggle('on', b.dataset.src === which);
    });
    var to = curAudio();
    bindAudio(to);
    var apply = function () {
      try { to.currentTime = Math.min(t, to.duration || t); } catch (e) {}
      if (wasPlaying) { var p = to.play(); if (p && p.catch) p.catch(function () {}); }
      paint();
    };
    if (to.readyState >= 1) apply();
    else to.addEventListener('loadedmetadata', apply, { once: true });
    paint();
  }

  /* ---------- 交互 ---------- */
  function holdPage() {
    st.pageHold = ((window.performance && performance.now) ? performance.now() : Date.now()) + 4000;
  }

  function bindChrome() {
    var ov = el('dqOverlay');
    if (ov && !ov.dataset.b) {
      ov.dataset.b = '1';
      ov.addEventListener('click', function (ev) {
        var img = el('dqPageImg'), pg = st.layout && st.layout.pages[st.page - 1];
        if (!img || !pg) return;
        var r = img.getBoundingClientRect();
        var px = (ev.clientX - r.left) / r.width * pg.w;
        var py = (ev.clientY - r.top) / r.height * pg.h;
        /* 本页第一小节的全局序号：之前每页的小节数都要算上，否则第 2 页起点哪都跳错 */
        var n = 0;
        for (var pp = 0; pp < st.page - 1; pp++) {
          var prev = st.layout.pages[pp];
          for (var sj = 0; sj < prev.systems.length; sj++) n += prev.systems[sj].bounds.length - 1;
        }
        for (var si = 0; si < pg.systems.length; si++) {
          var sy = pg.systems[si];
          if (py >= sy.top - 14 && py <= sy.bottom + 14) {
            for (var b = 0; b < sy.bounds.length - 1; b++) {
              if (px >= sy.bounds[b] && px <= sy.bounds[b + 1]) {
                var idx = n + b;
                if (st.bars[idx]) seekInBar(idx, px);
                return;
              }
            }
            return;
          }
          n += sy.bounds.length - 1;
        }
      });
    }
    var play = el('dqPlay');
    if (play && !play.dataset.b) { play.dataset.b = '1'; play.onclick = togglePlay; }
    var back = el('dqHistBack');
    if (back) back.onclick = function () { backToJob(); };
    watchUserScroll();

    el('dqPrevPage').onclick = function () {
      if (st.page > 1) { st.page--; holdPage(); renderPdf(); }
    };
    el('dqNextPage').onclick = function () {
      if (st.layout && st.page < st.layout.pages.length) { st.page++; holdPage(); renderPdf(); }
    };
    el('dqZoom').onchange = function () {
      st.zoom = this.value;
      el('dqPageImg').removeAttribute('data-loaded');
      renderPdf();
    };
    el('dqPrevBar').onclick = function () { var h = barAt(time()); goBar(h ? h.i - 1 : 0); };
    el('dqNextBar').onclick = function () { var h = barAt(time()); goBar(h ? h.i + 1 : 0); };
    Array.prototype.forEach.call(document.querySelectorAll('#dqSrc button'), function (b) {
      b.onclick = function () { switchSrc(b.dataset.src); };
    });

    var scb = el('dqScrub'), dragging = false;
    if (scb && !scb.dataset.b) {
      scb.dataset.b = '1';
      var fromEv = function (ev) {
        var r = scb.getBoundingClientRect();
        var f = Math.min(1, Math.max(0, (ev.clientX - r.left) / Math.max(1, r.width)));
        seek(f * totalDur());
      };
      scb.addEventListener('pointerdown', function (ev) {
        dragging = true;
        try { scb.setPointerCapture(ev.pointerId); } catch (e) {}
        fromEv(ev);
      });
      scb.addEventListener('pointermove', function (ev) { if (dragging) fromEv(ev); });
      scb.addEventListener('pointerup', function (ev) {
        dragging = false;
        try { scb.releasePointerCapture(ev.pointerId); } catch (e) {}
      });
      scb.addEventListener('pointercancel', function () { dragging = false; });
    }

    if (!document.body.dataset.dqKeys) {
      document.body.dataset.dqKeys = '1';
      document.addEventListener('keydown', function (ev) {
        var tag = (ev.target && ev.target.tagName) || '';
        if (/^(INPUT|SELECT|TEXTAREA)$/.test(tag)) return;
        if (el('dqScore').hidden) return;
        if (ev.code === 'Space') { ev.preventDefault(); togglePlay(); }
        else if (ev.key === 'ArrowLeft') {
          ev.preventDefault();
          if (st.page > 1) { st.page--; holdPage(); renderPdf(); }
        } else if (ev.key === 'ArrowRight') {
          ev.preventDefault();
          if (st.layout && st.page < st.layout.pages.length) { st.page++; holdPage(); renderPdf(); }
        }
      });
    }

    if (!window.__dqResize) {
      window.__dqResize = 1;
      var rt = 0;
      window.addEventListener('resize', function () {
        clearTimeout(rt);
        rt = setTimeout(function () {
          if (st.layout) { renderPdf(); paintPdf(); }
        }, 220);
      });
    }
  }

  /* ---------- 数据 ---------- */
  function fetchScore(name) {
    return Promise.all([
      fetch('/api/scoremap?song=' + encodeURIComponent(name)).then(function (r) { return r.ok ? r.json() : null; }),
      fetch('/api/scorelayout?song=' + encodeURIComponent(name)).then(function (r) { return r.ok ? r.json() : null; })
    ]);
  }

  function applyScore(sm, lo, name) {
    if (!sm || !sm.ok || !(sm.bars || []).length) return false;
    if (name === st.jobSong) window.__dqLastJobSong = name;
    st.song = name;
    st.bars = sm.bars || [];
    st.layout = (lo && lo.ok) ? lo : null;
    st.page = 1;
    var mix = el('dqAudio');
    var want = '/api/source?song=' + encodeURIComponent(name);
    if (mix.getAttribute('src') !== want) mix.setAttribute('src', want);
    /* 音源选回原曲；鼓声轨属于「本次任务」，看历史时清掉，免得播上一首的鼓声 */
    st.src = 'mix';
    Array.prototype.forEach.call(document.querySelectorAll('#dqSrc button'), function (b) {
      b.classList.toggle('on', b.dataset.src === 'mix');
    });
    if (name !== (window.__dqLastJobSong || '')) el('resAudio').removeAttribute('src');
    disp.x = null; disp.bar = -1; disp.last = 0;
    bindAudio(mix);
    bindAudio(el('resAudio'));
    bindChrome();
    el('dqBarTotal').textContent = st.bars.length;
    el('dqStat').textContent = '共 ' + st.bars.length + ' 小节 · 全长 ' + mmss(totalDur()) +
      ' · ♩=' + (Math.round(st.bars[0].bpm || 0) || '—');
    el('dqWarn').hidden = true;
    el('dqScore').dataset.ready = '1';
    var pimg = el('dqPageImg');
    pimg.dataset.srcKey = '';
    pimg.dataset.loaded = '';
    pimg.removeAttribute('data-load-page');
    preloaded = {};
    st.pageTurn = true;          /* 首次上屏直接落到居中位置 */
    sc.chasing = false;
    buildCols();
    if (st.layout && st.colsBad > Math.max(3, st.bars.length * 0.35)) {
      el('dqWarn').textContent = '⚠ 这份 PDF 的音符列与谱面数据不完全一致，播放头在这首里只能近似对齐' +
        '（其他歌不受影响）。重新扒一次这首歌通常就好了。';
      el('dqWarn').hidden = false;
    }
    if (st.layout) { renderPdf(); paintPdf(); } else {
      el('dqOverlay').innerHTML = '';
    }
    return true;
  }

  function setHist(on, label) {
    var box = el('dqHist');
    if (!box) return;
    box.hidden = !on;
    if (on) el('dqHistName').textContent = label;
    el('dqScore').dataset.explicit = on ? '1' : '';
  }

  function load() {
    if (st.explicit) return;             /* 用户正从「最近产物」看历史，别被当前任务顶掉 */
    fetch('/api/status').then(function (r) { return r.json(); }).then(function (s) {
      if (!s || s.running || !s.song) return;
      var hasPdf = (s.results || []).some(function (r) { return r.kind === 'pdf'; });
      if (!hasPdf) return;
      var k = s.song + '|' + (s.finished || '');
      if (k === st.key) return;
      /* 失败了就别每 2.5 秒重跑一次 PDF 解析（每首 0.1~0.3 秒 CPU），退避 30 秒 */
      var nowMs = (window.performance && performance.now) ? performance.now() : Date.now();
      if (st.failAt && nowMs - st.failAt < 30000) return;
      st.key = k;
      st.jobSong = s.song;
      var card = el('dqScore');
      if (card) card.removeAttribute('data-ready');
      fetchScore(s.song).then(function (res) {
        if (st.explicit) return;                 /* 用户已打开别的歌，别覆盖 */
        if (!applyScore(res[0], res[1], s.song)) { st.key = ''; st.failAt = nowMs; return; }
        var done = el('viewDone');
        card.hidden = !(done && !done.hidden);
        setHist(false, '');
      }).catch(function (e) {
        st.key = ''; st.failAt = nowMs;
        if (window.console) console.error('[dq] 载入谱面失败：', e && (e.stack || e.message || e));
      });
    }).catch(function () {});
  }

  /* 从「最近产物」打开某首歌的动态谱 */
  function openSong(name) {
    var card = el('dqScore');
    if (!card || !name) return;
    st.explicit = true;
    st.key = '';
    fetchScore(name).then(function (res) {
      if (!applyScore(res[0], res[1], name)) {
        st.explicit = false;
        el('dqWarn').textContent = '这首歌没有找到可用的谱面数据（可能产物被删了）。';
        el('dqWarn').hidden = false;
        return;
      }
      setHist(true, name);
      card.hidden = false;
      var mm = scroller();
      var want = Math.max(0, card.offsetTop - 70);
      if (mm === document.documentElement) window.scrollTo(0, want); else mm.scrollTop = want;
    }).catch(function (e) {
      st.explicit = false;
      if (window.console) console.error('[dq] 打开谱面失败：', e && (e.stack || e.message || e));
      el('dqWarn').textContent = '打开这首的谱面失败：' + ((e && e.message) || e);
      el('dqWarn').hidden = false;
    });
  }

  function backToJob() {
    st.explicit = false;
    st.key = '';
    setHist(false, '');
    load();
  }

  window.dqOpen = openSong;
  window.__dq = { st: st, sc: sc, disp: disp, sys: systemDocRect, sticky: stickyOffset };

  setInterval(load, 2500);
  setTimeout(load, 500);
})();
