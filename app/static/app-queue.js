/* ===== DrumQuill 增强模块：队列 / ETA / 失败提示 / 诊断包（独立于主逻辑） ===== */
(function () {
  var st = null;
  function $(sel) { return document.querySelector(sel); }
  function el(tag, cls, text) {
    var d = document.createElement(tag);
    if (cls) d.className = cls;
    if (text != null) d.textContent = text;
    return d;
  }
  function toast(msg, bad) {
    var t = document.getElementById('toast');
    if (!t) { if (window.console) console.log('[queue]', msg); return; }
    t.textContent = msg;
    t.className = 'toast show' + (bad ? ' err' : '');
    clearTimeout(t.__t);
    t.__t = setTimeout(function () { t.className = 'toast'; }, 2600);
  }
  function fmtETA(sec) {
    if (sec == null) return '';
    var m = Math.round(sec / 60);
    if (m < 1) return '不到 1 分钟';
    return '约 ' + m + ' 分钟';
  }
  function esc(t) {
    return String(t == null ? '' : t).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }
  function api(url, body) {
    return fetch(url, body ? {method: 'POST', headers: {'Content-Type': 'application/json'},
                              body: JSON.stringify(body)} : {}).then(function (r) { return r.json(); });
  }

  /* 与 /api/start 同源的 mode/bpm：模式取当前选中的产出按钮，BPM 取输入框。
     队列推进（run_job）不再清洗 bpm，脏值会直进管线，必须在入库前按
     /api/start 的规则（int 后 20–400，越界/非法不传 → 自动检测）清掉；无选中时 mode=full 向后兼容。 */
  function pickModeBpm() {
    var out = { mode: 'full' };
    var mb = document.querySelector('#modeSwitch .jobmode.on');
    if (mb && mb.dataset.mode) out.mode = mb.dataset.mode;
    var raw = ((document.getElementById('bpm') || {}).value || '').trim();
    if (raw) {
      var v = Math.floor(Number(raw));
      if (isFinite(v) && v >= 20 && v <= 400) out.bpm = v;
    }
    return out;
  }

  /* 新建页的「加入队列」 */
  function bindAddFromPick() {
    var b = document.getElementById('addQueueBtn');
    if (!b || b.dataset.b) return;
    b.dataset.b = '1';
    b.onclick = function () {
      var n = (document.getElementById('pickedName') || {}).textContent || '';
      if (!n) { toast('先选一首歌', true); return; }
      var opt = pickModeBpm();
      api('/api/queue', {action: 'add', songs: [n], mode: opt.mode, bpm: opt.bpm}).then(function (j) {
        if (!j || !j.ok) { toast('加入队列失败：' + ((j && j.error) || '请求失败'), true); return; }
        toast(j.added > 0 ? ('已加入队列：' + n) : '未加入（文件不存在或已在队列中）', j.added > 0 ? false : true);
        refresh();
        if (window.dqShellShow) window.dqShellShow('queue');
      }).catch(function (e) { toast('加入队列请求失败：' + e, true); });
    };
  }

  /* ---------- 队列卡片 ---------- */
  function queueCard() {
    bindAddFromPick();
    var box = document.getElementById('dqQueue');
    if (!box) {
      var anchor = document.getElementById('pickedBlock');
      if (!anchor || !anchor.parentNode) return;
      box = el('div', 'note info');
      box.id = 'dqQueue';
      box.style.marginTop = '14px';
      anchor.parentNode.insertBefore(box, anchor.nextSibling);
    }
    var q = (st && st.queue) || {items: [], total: 0, done: 0, paused: false};
    var html = '<div class="cardhead" style="margin-bottom:var(--s3)">' +
      '<div><h1 style="font-size:15px">' + (q.paused ? '已暂停' : '进行中') + '</h1>' +
      '<div class="metaline"><span class="k">进度</span><span class="v">' + q.done + ' / ' + q.total +
      '</span></div></div>' +
      '<div class="right" style="display:flex;gap:8px;align-items:center">' +
      '<button class="btn ghost sm" id="dqAdd">把当前选中加入队列</button>' +
      '<button class="btn ghost sm" id="dqPause">' + (q.paused ? '继续队列' : '暂停队列') + '</button>' +
      '<button class="btn ghost sm" id="dqClear">清空</button></div></div>';
    if (q.items.length) {
      html += '<div class="qlist">' + q.items.map(function (it) {
        var cls = it.state === 'done' ? 'ok' : it.state === 'running' ? 'req'
                : (it.state === 'failed' || it.state === 'missing') ? 'miss' : 'opt';
        var tag = it.state === 'done' ? '完成' : it.state === 'running' ? '进行中'
                : it.state === 'failed' ? '失败'
                : it.state === 'missing' ? '找不到文件' : '等待';
        var dur = '';
        if (it.duration != null && (it.state === 'done' || it.state === 'failed')) {
          var dm = Math.floor(it.duration / 60), ds = it.duration % 60;
          dur = '<span class="qmeta" style="color:#64748b;font-size:12px;margin-left:auto;padding-right:10px">' +
                (dm ? dm + ' 分 ' : '') + ds + ' 秒</span>';
        }
        return '<div class="qrow">' +
          '<span class="qname">' + esc(it.song) + '</span>' +
          '<span class="badge ' + cls + '">' + tag + '</span>' + dur +
          '<button class="btn ghost sm" data-dqrm="' + esc(it.song) + '">移除</button></div>';
      }).join('') + '</div>';
    } else {
      html += '<div class="empty">队列为空<br><span class="hint">在「新建扒谱」里选好歌，点「加入队列」就会排到这里</span></div>';
    }
    var key = JSON.stringify([q.done, q.total, q.paused].concat((q.items || []).map(function (it) {
      return it.song + ':' + it.state;
    })));
    if (box.__key !== key) { box.innerHTML = html; box.__key = key; }
    var add = document.getElementById('dqAdd');
    if (add) add.onclick = function () {
      var n = (document.getElementById('pickedName') || {}).textContent || '';
      if (!n) { toast('先到「新建扒谱」选一首歌', true); return; }
      var opt = pickModeBpm();
      api('/api/queue', {action: 'add', songs: [n], mode: opt.mode, bpm: opt.bpm}).then(function (j) {
        if (!j || !j.ok) { toast('加入队列失败：' + ((j && j.error) || '请求失败'), true); return; }
        toast(j.added > 0 ? ('已加入队列：' + n) : '未加入（文件不存在或已在队列中）', j.added > 0 ? false : true);
        refresh();
        if (window.dqShellShow) window.dqShellShow('queue');
      }).catch(function (e) { toast('加入队列请求失败：' + e, true); });
    };
    var pa = document.getElementById('dqPause');
    if (pa) pa.onclick = function () {
      api('/api/queue', {action: q.paused ? 'resume' : 'pause'}).then(function (j) {
        if (!j || !j.ok) { toast((q.paused ? '继续队列' : '暂停队列') + '失败：' + ((j && j.error) || ''), true); return; }
        refresh();
      }).catch(function () {});
    };
    var cl = document.getElementById('dqClear');
    if (cl) cl.onclick = function () {
      api('/api/queue', {action: 'clear'}).then(function (j) {
        if (!j || !j.ok) { toast('清空队列失败：' + ((j && j.error) || ''), true); return; }
        refresh();
      }).catch(function () {});
    };
    Array.prototype.forEach.call(box.querySelectorAll('[data-dqrm]'), function (b) {
      b.onclick = function () {
        api('/api/queue', {action: 'remove', song: b.getAttribute('data-dqrm')}).then(function (j) {
          if (!j || !j.ok) { toast('移除失败：' + ((j && j.error) || ''), true); return; }
          refresh();
        }).catch(function () {});
      };
    });
  }

  /* ---------- ETA ---------- */
  function renderETA() {
    var meta = document.getElementById('runMeta');
    if (!meta) return;
    var old = document.getElementById('dqEta');
    if (old) old.remove();
    if (st && st.running && st.eta_sec != null) {
      var s = el('span', 'k');
      s.id = 'dqEta';
      s.style.marginLeft = '10px';
      s.textContent = '预计剩余 ' + fmtETA(st.eta_sec);
      meta.appendChild(s);
    }
  }

  /* ---------- 失败提示 ---------- */
  function renderHints() {
    var msg = document.getElementById('failMsg');
    if (!msg || !msg.parentNode) return;
    var box = document.getElementById('dqHints');
    if (!box) {
      box = el('div');
      box.id = 'dqHints';
      msg.parentNode.insertBefore(box, msg.nextSibling);
    }
    var hints = (st && st.hints) || [];
    box.innerHTML = '';
    hints.forEach(function (h) {
      var card = el('div', 'note err');
      card.style.marginTop = '10px';
      card.appendChild(el('div', null, h.text));
      var row = el('div');
      row.style.marginTop = '8px';
      (h.actions || []).forEach(function (a) {
        var b = el('button', 'btn ghost sm', a.label);
        b.style.marginRight = '8px';
        b.onclick = function () {
          if (a.kind === 'settings') {
            if (window.dqOpenSettings) window.dqOpenSettings();
            else { var s = document.getElementById('settingsBtn'); if (s) s.click(); }
          } else if (a.kind === 'rerun') {
            var body = a.body || {};
            body.song = st && st.song;
            api('/api/start', body).then(function (j) {
              if (!j || !j.ok) { toast('重试启动失败：' + ((j && j.error) || ''), true); return; }
              refresh();
            }).catch(function () {});
          }
        };
        row.appendChild(b);
      });
      card.appendChild(row);
      box.appendChild(card);
    });
  }

  /* ---------- 诊断包 ---------- */
  function addDiagButtons() {
    var retry = document.getElementById('retryBtn');
    if (retry && !document.getElementById('dqDiagFail')) {
      var b = el('button', 'btn ghost', '导出诊断包');
      b.id = 'dqDiagFail';
      b.style.marginLeft = '10px';
      b.onclick = function () { window.location.href = '/api/diagnostics'; };
      retry.parentNode.appendChild(b);
    }
    var foot = document.querySelector('#setModal .actions');
    if (foot && !document.getElementById('dqDiagSet')) {
      var b2 = el('button', 'btn ghost sm', '导出诊断包');
      b2.id = 'dqDiagSet';
      b2.onclick = function () { window.location.href = '/api/diagnostics'; };
      foot.appendChild(b2);
    }
  }

  function refresh() {
    fetch('/api/status').then(function (r) { return r.json(); }).then(function (j) {
      st = j;
      queueCard();
      renderETA();
      renderHints();
      addDiagButtons();
    }).catch(function () {});
  }
  /* ---------- 把 BPM 收进"更多选项"，减少开始前的决策 ---------- */
  function collapseBpm() {
    var bpm = document.getElementById('bpm');
    if (!bpm || document.getElementById('dqMore')) return;
    /* 新版界面已经把 BPM 放进「高级选项」折叠里，别再套一层 */
    if (bpm.closest('#advOpts')) return;
    var row = bpm.closest('.field') || bpm.parentNode;
    if (!row || !row.parentNode) return;
    var det = document.createElement('details');
    det.id = 'dqMore';
    det.style.marginTop = '10px';
    det.innerHTML = '<summary style="cursor:pointer;color:#64748b;font-size:13px">更多选项（速度 BPM，通常留空自动检测）</summary>';
    row.parentNode.insertBefore(det, row);
    det.appendChild(row);
  }

  setInterval(refresh, 2500);
  setTimeout(refresh, 1200);
  setTimeout(collapseBpm, 800);
})();
