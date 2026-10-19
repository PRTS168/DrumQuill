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
  function fmtETA(sec) {
    if (sec == null) return '';
    var m = Math.round(sec / 60);
    if (m < 1) return '不到 1 分钟';
    return '约 ' + m + ' 分钟';
  }
  function api(url, body) {
    return fetch(url, body ? {method: 'POST', headers: {'Content-Type': 'application/json'},
                              body: JSON.stringify(body)} : {}).then(function (r) { return r.json(); });
  }

  /* ---------- 队列卡片 ---------- */
  function queueCard() {
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
    var html = '<div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">' +
      '<b>批量队列</b><span class="d">' + q.done + ' / ' + q.total + ' 已完成</span>' +
      '<span style="flex:1"></span>' +
      '<button class="btn ghost sm" id="dqAdd">把当前选中加入队列</button>' +
      '<button class="btn ghost sm" id="dqPause">' + (q.paused ? '继续队列' : '暂停队列') + '</button>' +
      '<button class="btn ghost sm" id="dqClear">清空</button></div>';
    if (q.items.length) {
      html += '<div style="margin-top:8px">' + q.items.map(function (it) {
        var tag = it.state === 'done' ? '✓ 完成' : it.state === 'running' ? '▶ 进行中'
                : it.state === 'failed' ? '✕ 失败' : '… 等待';
        return '<div style="display:flex;gap:8px;align-items:center;padding:2px 0">' +
          '<span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">' + it.song + '</span>' +
          '<span class="d">' + tag + '</span>' +
          '<button class="btn ghost sm" data-dqrm="' + it.song + '">移除</button></div>';
      }).join('') + '</div>';
    } else {
      html += '<div class="d" style="margin-top:6px">队列为空。把歌加进来，跑完一首会自动接下一首。</div>';
    }
    box.innerHTML = html;
    var add = document.getElementById('dqAdd');
    if (add) add.onclick = function () {
      var n = (document.getElementById('pickedName') || {}).textContent || '';
      if (!n) { toast('先在下面选一首歌', true); return; }
      api('/api/queue', {action: 'add', songs: [n]}).then(function () { refresh(); });
    };
    var pa = document.getElementById('dqPause');
    if (pa) pa.onclick = function () { api('/api/queue', {action: q.paused ? 'resume' : 'pause'}).then(refresh); };
    var cl = document.getElementById('dqClear');
    if (cl) cl.onclick = function () { api('/api/queue', {action: 'clear'}).then(refresh); };
    Array.prototype.forEach.call(box.querySelectorAll('[data-dqrm]'), function (b) {
      b.onclick = function () { api('/api/queue', {action: 'remove', song: b.getAttribute('data-dqrm')}).then(refresh); };
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
            var s = document.getElementById('settingsBtn');
            if (s) s.click();
          } else if (a.kind === 'rerun') {
            var body = a.body || {};
            body.song = st && st.song;
            api('/api/start', body).then(refresh);
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
    var foot = document.querySelector('#setModal .modal-foot');
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
