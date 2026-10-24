(function(){
  'use strict';
  var $ = function(s){ return document.querySelector(s); };

  /* ================= 常量 ================= */
  /* 必须与后端 app.py STAGES 的 1..6 一一对应 */
  var STAGES = [
    { name:'读取歌曲信息 · 检测速度', hint:'分析音频、自动测速' },
    { name:'分离鼓轨（Demucs）',      hint:'最慢的一步：把鼓声从伴奏里分离出来' },
    { name:'转录 MIDI（ADTOF）',      hint:'逐帧识别底鼓 / 军鼓 / 通鼓 / 镲片' },
    { name:'校正小节线 · 生成乐谱',    hint:'对齐首拍并量化到 16 分网格' },
    { name:'渲染 PDF（MuseScore）',   hint:'排成五线谱并导出 PDF' },
    { name:'合成鼓声音频',            hint:'用音色库渲染鼓声，可直接试听' }
  ];
  var SET_FIELDS = [
    ['dataDir','数据目录（songs / midis / output 默认在其下）',true,'text','common'],
    ['museScorePath','MuseScore 路径（留空 = 自动查找）',false,'text','common'],
    ['ffmpegPath','ffmpeg 路径（留空 = 自动查找）',false,'text','common'],
    ['demucsJobs','CPU 并行分段数（留空 = 按核数自动）',false,'number','common'],
    ['gpuPython','GPU 运行时 python.exe（留空 = 自动探测 .venv-gpu）',true,'text','common'],
    ['bpmFallback','测速失败时的兜底 BPM',false,'number','common'],
    ['postDedupeMs','识别后处理：去抖去重阈值 ms（0=关闭，默认 50）',false,'number','common'],
    ['postMinLenMs','识别后处理：最短音符时值 ms（0=关闭，默认 30）',false,'number','common'],
    ['postSwing','摇摆量化量 0–0.6（0=直线量化）',false,'number','common'],
    ['adtofThresholds','转录阈值 底鼓,军鼓,嗵鼓,踩镲,吊镲（留空=按分离模型自动，越小越灵敏）',false,'text','common'],
    ['quantizeTol','量化吸附容差（占十六分音符比例 0–0.5，0=不量化，默认 0.25）',false,'number','common'],
    ['beatGrid','节拍网格：跟踪真实拍点与强拍（关闭则退回全曲固定 BPM）',false,'bool','common'],
    ['songsDir','歌曲目录 songsDir（留空 = dataDir\\songs）',false,'text','adv'],
    ['midisDir','MIDI 目录 midisDir（留空 = dataDir\\midis）',false,'text','adv'],
    ['outputDir','输出目录 outputDir（留空 = dataDir\\output）',false,'text','adv'],
    ['stemsDir','分离鼓轨目录（留空 = dataDir\\stems）',false,'text','adv'],
    ['venvDir','虚拟环境 venvDir（留空 = 根\\.venv）',false,'text','adv'],
    ['adtofDir','ADTOF 源码目录（留空 = 自动查找）',false,'text','adv'],
    ['soundDir','音色目录 soundDir（留空 = 根\\sound）',false,'text','adv'],
    ['modelsDir','模型目录 modelsDir（留空 = 根\\models）',false,'text','adv']
  ];
  var SETUP_PATHS = [
    ['dataDir','数据目录',true],
    ['venvDir','虚拟环境'],
    ['modelsDir','模型目录'],
    ['soundDir','音色目录'],
    ['museScorePath','MuseScore 路径'],
    ['ffmpegPath','ffmpeg 路径']
  ];

  /* ================= 状态 ================= */
  var cfg = null;              /* /api/config 结果 */
  var gpu = null;              /* GPU 探测快照 */
  var devPref = 'auto';        /* auto | cpu | cuda */
  var jobMode = 'full';        /* full | score | audio | stems */
  var curSong = null;          /* 当前选中的歌曲文件名 */
  var curDur = null;           /* 歌曲时长（秒） */
  var lastStatus = null;
  var sumKey = null;           /* 完成视图的渲染键，避免每 2 秒重绘播放器 */
  var stageSeen = {};          /* stage -> 首次出现时间戳(ms) */
  var stageDur = {};           /* stage -> 完成耗时(秒) */
  var curStage = 0;
  var setupPending = false;
  var setupLogSeq = 0, setupTimer = null, setupCompsData = [];
  var toastTimer = null;
  var dismissedKey = null;   /* 用户点了「再扒一首」后，不再弹回同一个已结束任务 */

  /* ================= 小工具 ================= */
  function esc(s){
    return String(s==null?'':s).replace(/[&<>"']/g, function(c){
      return ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'})[c];
    });
  }
  function fmtSize(b){
    b = Number(b)||0;
    if(b>=1048576) return (b/1048576).toFixed(1)+' MB';
    if(b>=1024) return Math.round(b/1024)+' KB';
    return b+' B';
  }
  function fmtDur(sec){
    sec = Math.max(0, Math.round(Number(sec)||0));
    var m = Math.floor(sec/60), s = sec%60;
    if(m<=0) return s+' 秒';
    return m+' 分 '+(s<10?'0'+s:s)+' 秒';
  }
  function stripExt(n){ return String(n==null?'':n).replace(/\.[^.\/\\]+$/,''); }
  function show(el){ if(el) el.hidden = false; }
  function hide(el){ if(el) el.hidden = true; }
  /* 选歌态：只在这两处切换「拖拽区 / 已选文件 / 三步说明」 */
  function syncPickUI(hasSong){
    var d = $('#drop'), pb = $('#pickedBlock'), qs = $('#quickSteps');
    if(d) d.hidden = !!hasSong;
    if(pb) pb.hidden = !hasSong;
    if(qs) qs.hidden = !!hasSong;
  }
  function toast(msg, isErr){
    var t = $('#toast');
    t.textContent = msg;
    t.className = 'toast show' + (isErr ? ' err' : '');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function(){ t.className = 'toast'; }, 3200);
  }
  window.dqToast = toast;      /* 其它模块（谱面 / 队列）复用同一个提示条 */
  function setMark(kind){  /* idle | busy | done | bad */
    var m = $('#mark');
    if(m) m.className = 'statusdot' + (kind === 'idle' ? '' : ' ' + kind);
  }

  /* ================= 视图切换 ================= */
  /* pick | run | done | fail */
  var _lastHistToast = 0;
  function showView(name, opts){
    var map = { pick:'#viewPick', run:'#viewRun', done:'#viewDone', fail:'#viewFail' };
    Object.keys(map).forEach(function(k){
      var el = $(map[k]);
      if(k === name) show(el); else hide(el);
    });
    /* 谱面卡显示与否交给 app-score 的唯一出口决定（练习态也在那里处理） */
    if(window.dqSyncCard) window.dqSyncCard();
  }
  window.dqShowView = showView;
  window.dqUndismiss = function(){ dismissedKey = null; sumKey = null; };   /* 「回到本次结果」用 */

  /* ================= 设备 ================= */
  function devLabel(pref){
    if(pref==='cpu') return 'CPU';
    if(pref==='cuda'||pref==='gpu') return 'GPU';
    return '自动';
  }

  function renderEngine(){
    var chip = $('#engineChip');
    var ready = gpu && gpu.status === 'ready';
    var avail = !!(gpu && gpu.available);
    var want = devPref === 'cuda' ? 'gpu' : devPref;

    if(!ready){
      chip.className = 'chip';
      chip.innerHTML = '<span class="dot2"></span>检测 GPU…';
    }else if(avail && want !== 'cpu'){
      chip.className = 'chip gpu';
      chip.innerHTML = '<span class="dot2"></span><b>'+esc(gpu.name.replace(/^NVIDIA\s+/,''))+'</b> · '+Math.round(gpu.vram_mb/1024)+'GB';
      chip.title = '分离使用 '+gpu.name+'（'+gpu.vram_mb+' MB 显存）';
    }else if(avail){
      chip.className = 'chip';
      chip.innerHTML = '<span class="dot2"></span><b>CPU 模式</b>（GPU 可用）';
      chip.title = 'GPU 可用但被手动关闭';
    }else{
      chip.className = 'chip';
      chip.innerHTML = '<span class="dot2"></span><b>CPU 模式</b>';
      chip.title = '未检测到可用 GPU' + ((gpu && gpu.reason) ? '：'+gpu.reason : '');
    }

    /* 分段控件 */
    var seg = $('#devSwitch');
    if(seg){
      Array.prototype.forEach.call(seg.children, function(b){
        b.classList.toggle('on', b.dataset.dev === want);
        if(b.dataset.dev === 'gpu') b.disabled = !avail;
      });
    }
    var hint = $('#devHint');
    if(hint){
      if(!ready) hint.textContent = '检测中…';
      else if(want === 'gpu') hint.textContent = '使用 ' + gpu.name + '，约 3 倍速';
      else if(want === 'cpu') hint.textContent = avail ? '已强制 CPU（GPU 可用）' : '未检测到可用 GPU，将用 CPU';
      else hint.textContent = avail ? ('自动：用 ' + gpu.name) : '未检测到可用 GPU，将用 CPU';
    }
    renderEta();
    renderSettingsEngineLine();
  }

  function lockDevice(locked){
    var seg = $('#devSwitch');
    if(seg){
      Array.prototype.forEach.call(seg.children, function(b){ b.disabled = locked ? true : (b.dataset.dev==='gpu' && !(gpu && gpu.available)); });
    }
    var hint = $('#devHint');
    if(hint && locked) hint.textContent = '任务进行中，已锁定';
    else renderEngine();
    lockMode(locked);
  }

  /* ---- 作业模式（完整 / 只要谱子 / 只要音频 / 只要鼓声） ---- */
  var MODE_HINT = {
    full:  '完整：鼓谱 PDF + MusicXML + MIDI + 鼓声音频',
    score: '只要谱子：出 PDF / MusicXML / MIDI，跳过音频合成（更快一点）',
    audio: '只要音频：仍会分离与转录，只输出鼓声音频（不出 PDF/谱面）',
    stems: '只要鼓声：只跑鼓轨分离，直接给纯鼓轨音频（最快，不做转录）'
  };
  function renderMode(){
    Array.prototype.forEach.call(document.querySelectorAll('#modeSwitch .jobmode'), function(b){
      b.classList.toggle('on', b.dataset.mode === jobMode);
    });
    var h = $('#modeHint');
    if(h) h.textContent = MODE_HINT[jobMode] || '';
    renderEta();
  }
  function lockMode(locked){
    Array.prototype.forEach.call(document.querySelectorAll('#modeSwitch .jobmode'), function(b){
      b.disabled = locked;
    });
  }

  function pollGpu(attempt){
    attempt = attempt || 0;
    fetch('/api/gpu').then(function(r){ return r.json(); }).then(function(j){
      var wasReady = !!(gpu && gpu.status === 'ready');
      gpu = j;
      renderEngine();
      if(j.status === 'ready' && !wasReady && typeof loadSetupStatus === 'function'){
        loadSetupStatus();   /* GPU 探测结果会影响"能力/组件"列表，刷新一次 */
      }
      if(j.status !== 'ready' && attempt < 15) setTimeout(function(){ pollGpu(attempt+1); }, 1500);
    }).catch(function(){
      if(attempt < 15) setTimeout(function(){ pollGpu(attempt+1); }, 2000);
    });
  }

  /* ================= 预计用时 ================= */
  function estimateTotal(){
    if(!curDur) return null;
    var factor = (devPref === 'cpu' || !(gpu && gpu.available)) ? 1.7 : 0.75;
    return Math.round(45 + curDur * factor);
  }
  function renderEta(){
    var el = $('#etaHint');
    if(!el) return;
    var total = estimateTotal();
    if(!total){ el.textContent = ''; return; }
    el.textContent = '这首歌 ' + fmtDur(curDur) + '，预计用时约 ' + fmtDur(total) + '（' +
      ((devPref==='cpu' || !(gpu && gpu.available)) ? 'CPU' : 'GPU') + '）';
  }

  /* 时长探测：用于「预计用时」，刷新后也能从后端补回来 */
  function probeDuration(song, cb){
    if(!song) return;
    fetch('/api/probe?song='+encodeURIComponent(song)).then(function(r){ return r.json(); })
      .then(function(p){
        if(p && p.duration_s){ curDur = p.duration_s; renderEta(); if(cb) cb(p); }
      }).catch(function(){});
  }

  /* 一个已结束任务的唯一键（用户关掉后不再自动弹回） */
  function statusKey(s){
    if(!s) return null;
    return [s.song||'', s.finished||'', s.error?'E':'K',
            (s.results||[]).map(function(r){ return r.kind+':'+r.name+':'+r.size; }).join('|')].join('#');
  }
  function dismissFinished(){
    if(lastStatus && lastStatus.finished) dismissedKey = statusKey(lastStatus);
  }

  /* ================= 轮询状态 ================= */
  /* 记录每一步的耗时：切到下一步时结算上一步 */
  function noteStage(st){
    st = Number(st)||0;
    if(!st || st === curStage) return;
    if(curStage && stageSeen[curStage]) stageDur[curStage] = Math.round((Date.now()-stageSeen[curStage])/1000);
    stageSeen[st] = stageSeen[st] || Date.now();
    curStage = st;
  }
  function stageNow(){
    if(!curStage || !stageSeen[curStage]) return 0;
    return Math.round((Date.now()-stageSeen[curStage])/1000);
  }

  function renderSteps(stage, finished){
    var box = $('#steps');
    var html = '';
    for(var i=0;i<STAGES.length;i++){
      var n = i+1, cls = '', icon = String(n), meta = '';
      if(finished || n < stage){ cls = 'done'; icon = '✓'; meta = stageDur[n] ? fmtDur(stageDur[n]) : '完成'; }
      else if(n === stage){ cls = 'on'; meta = '进行中'; }
      html += '<li class="'+cls+'">' +
        '<span class="sicon'+(cls==='on'?' spin':'')+'">'+((cls==='on')?'':icon)+'</span>' +
        '<div class="sbody"><div class="sname">'+esc(STAGES[i].name)+'</div>' +
        '<div class="shint">'+esc(STAGES[i].hint)+'</div></div>' +
        '<span class="smeta">'+esc(meta)+'</span></li>';
    }
    box.innerHTML = html;
    /* 当前步骤算"做到一半"，进度条不至于长时间停在 0% */
    var pct = finished ? 100 : Math.round(((Math.max(0, stage) - 0.5) / STAGES.length) * 100);
    $('#bar').style.width = Math.max(0, Math.min(100, pct)) + '%';
  }

  function elapsedText(){
    if(!lastStatus || !lastStatus.started_at) return '';
    var t0 = new Date(String(lastStatus.started_at).replace(/-/g,'/')).getTime();
    if(!t0) return '';
    var sec = Math.max(0, Math.round((Date.now()-t0)/1000));
    var total = estimateTotal();
    var left = total ? total - sec : null;
    var txt = '已用 <b>'+fmtDur(sec)+'</b>';
    if(left !== null){
      txt += left > 10 ? ' · 预计还需 <b>'+fmtDur(left)+'</b>' : ' · 快完成了';
    }
    return txt;
  }
  function tickClock(){
    if(!(lastStatus && lastStatus.running)) return;
    var c = $('#runClock');
    if(c) c.innerHTML = elapsedText();
    /* 当前步骤的秒数实时跳动（不重建整个列表） */
    var on = document.querySelector('#steps li.on .smeta');
    if(on) on.textContent = '已 ' + fmtDur(stageNow());
  }

  /* 运行卡片顶部的一行元信息：设备 / 速度 / 时长 */
  function renderRunMeta(s){
    if(!s) return;
    var meta = [];
    if(s.device) meta.push('<span class="k">设备</span><span class="v">'+esc(s.device)+'</span>');
    if(s.detected_bpm) meta.push('<span class="k">速度</span><span class="v">'+esc(s.detected_bpm)+' BPM</span>');
    if(curDur) meta.push('<span class="k">时长</span><span class="v">'+fmtDur(curDur)+'</span>');
    $('#runMeta').innerHTML = meta.join('');
  }

  function applyStatus(s){
    lastStatus = s;
    var finished = !!s.finished;
    var running = !!s.running;

    if(running){
      setMark('busy');
      showView('run');
      if(curSong !== s.song) curSong = s.song;
      $('#runSong').textContent = s.song || '';
      renderRunMeta(s);
      if(!curDur) probeDuration(s.song, function(){ renderRunMeta(lastStatus || s); });
      var st = Number(s.stage)||0;
      noteStage(st);
      renderSteps(st || 1, false);
      var tip = $('#runTip');
      var tips = [
        '正在读取歌曲信息并自动测速…',
        '正在把鼓声从整首歌里分离出来 —— 这是最慢的一步，进度条可能会停一会儿，属正常。',
        '正在逐帧识别每一个鼓点（底鼓 / 军鼓 / 通鼓 / 镲片）。',
        '正在对齐首拍、把音符量化到 16 分网格。',
        '正在排版五线谱并导出 PDF。',
        '正在合成鼓声，马上就能试听。'
      ];
      tip.innerHTML = '<span>⏳</span><div>' + esc(tips[Math.max(0, Math.min(tips.length-1, st-1))]) +
        '<br><span style="opacity:.8">任务在后台运行，可以关掉这个页面，稍后打开还在。</span></div>';
      $('#runClock').innerHTML = elapsedText();
      $('#log').textContent = s.log_tail || '';
      var lw = $('#logWrap');
      if(lw && lw.open){ $('#log').scrollTop = $('#log').scrollHeight; }
      lockDevice(true);
      startEnable(false);
      checkStall(s);
      sumKey = null;
      return;
    }

    hide($('#stallWarn'));
    lockDevice(false);

    if(!finished){
      /* 空闲 */
      setMark('idle');
      /* 用户正在看某首歌的成品页：别把界面切回"选歌" */
      if(!(window.dqIsExplicit && window.dqIsExplicit())) showView('pick');
      syncPickUI(!!curSong);
      startEnable(!setupPending);
      return;
    }

    /* 已结束 */
    var key = statusKey(s);

    /* 用户已经点过「再扒一首」-> 这个结果不再自动弹出 */
    if(key === dismissedKey){
      setMark('idle');
      if(!(window.dqIsExplicit && window.dqIsExplicit())) showView('pick');
      /* 不要把已经选好的文件冲掉 */
      syncPickUI(!!curSong);
      startEnable(!setupPending);
      return;
    }

    var first = (key !== sumKey);
    sumKey = key;

    if(s.error){
      setMark('bad');
      showView('fail');
      var isCancel = String(s.error).indexOf('取消') >= 0;
      $('#failIcon').textContent = isCancel ? '⏹' : '!';
      $('#failTitle').textContent = isCancel ? '任务已取消' : '任务未完成';
      $('#failMeta').innerHTML =
        '<span class="k">歌曲</span><span class="v">'+esc(s.song||'-')+'</span>' +
        '<span class="k">时间</span><span class="v">'+esc(s.finished||'-')+'</span>';
      $('#failMsg').textContent = s.error + (isCancel
        ? '。中间产物已保留，可以直接重试。'
        : '。可以直接重试一次；如果反复失败，把程序目录下 app\\job.log 末尾的几行发出来。');
      return;
    }

    if(s.results && s.results.length){
      setMark('done');
      showView('done');
      if(first){
        /* 用户正在看别的歌，就别把结果卡的内容换掉，只提醒一声 */
        if(window.dqIsExplicit && window.dqIsExplicit()){
          var nowMs2 = Date.now();
          if(nowMs2 - _lastHistToast > 5000){
            _lastHistToast = nowMs2;
            if(window.dqToast) window.dqToast('这次扒谱已经完成 · 点「回到本次结果」看新谱');
          }
        }else{
          renderDone(s);
          loadHistory();        /* 这次的成品要立刻出现在「谱面库」里 */
        }
      }
      return;
    }

    /* 结束了但没有产物 */
    setMark('bad');
    showView('fail');
    $('#failIcon').textContent = '?';
    $('#failTitle').textContent = '没找到产物文件';
    $('#failMeta').innerHTML = '<span class="k">歌曲</span><span class="v">'+esc(s.song||'-')+'</span>';
    $('#failMsg').textContent = 'output 目录里没有这次的结果（可能被移动或删除了）。可以重试一次。';
  }

  /* ================= 结果卡：PDF 预览 + 主要产物下载 ================= */
  var resPv = { base: null, page: 1, pages: 0, dpi: 150 };
  var PROD_ORDER = ['pdf', 'xml', 'audio', 'midi'];
  var PROD_LABEL = { xml: 'MusicXML 乐谱', audio: '鼓声音频', midi: 'MIDI', pdf: '鼓谱 PDF' };

  function resPvPaint(){
    var im = $('#donePvImg');
    if(!resPv.base || !im) return;
    im.src = '/api/pdf/page?song='+encodeURIComponent(resPv.base)+'&page='+resPv.page+'&dpi='+resPv.dpi;
    $('#donePvPage').textContent = resPv.page + ' / ' + resPv.pages;
    $('#donePvPrev').disabled = resPv.page <= 1;
    $('#donePvNext').disabled = resPv.page >= resPv.pages;
  }

  /* 本次结果与历史某首歌共用这一套渲染：产物一律从 /api/artifacts 现读 */
  function fillResult(name, metaHtml){
    var base = stripExt(name || '');
    if(!base) return;
    resPv.base = base; resPv.page = 1; resPv.pages = 0;
    $('#doneMeta').innerHTML = metaHtml || ('<span class="k">歌曲</span><span class="v">'+esc(base)+'</span>');

    /* 预览：没有 PDF 就整块收起（例如「只要音频」模式） */
    fetch('/api/pdf/info?song='+encodeURIComponent(base)).then(function(r){ return r.json(); }).then(function(j){
      if(!j || !j.ok){ $('#donePv').hidden = true; $('#donePvSys').hidden = true; return; }
      $('#donePv').hidden = false;
      $('#donePvSys').hidden = false;
      resPv.pages = j.pages || 1;
      resPvPaint();
    }).catch(function(){ $('#donePv').hidden = true; });

    /* 产物行 */
    fetch('/api/artifacts?song='+encodeURIComponent(base)).then(function(r){ return r.json(); }).then(function(j){
      var items = ((j && j.items) || []).filter(function(it){
        if(PROD_ORDER.indexOf(it.kind) < 0) return false;
        /* .beats.mid / .beats_al.mid 是中间产物，不算「主要产物」 */
        if(it.kind === 'midi' && /\.beats(_al)?\.mid$/i.test(it.name)) return false;
        return true;
      });
      items.sort(function(a,b){ return PROD_ORDER.indexOf(a.kind) - PROD_ORDER.indexOf(b.kind); });
      var html = items.map(function(it){
        var label = it.label || PROD_LABEL[it.kind] || it.kind;
        if(it.kind === 'midi') label = 'MIDI';       /* 服务端标签带了文件名，这里去掉重复 */
        var acts = '';
        if(it.kind === 'pdf') acts += '<button class="btn ghost sm" data-pv="1">预览</button>';
        if(it.kind === 'audio') acts += '<button class="btn ghost sm" data-try="'+encodeURIComponent('/api/download?name='+encodeURIComponent(it.name)+'&song='+encodeURIComponent(base))+'">试听</button>';
        acts += '<a class="btn sm" href="/api/download?name='+encodeURIComponent(it.name)+'&song='+encodeURIComponent(base)+'">下载</a>';
        return '<div class="prow"><div class="pinfo">' +
          '<div class="pn">'+esc(label)+'</div>' +
          '<div class="pm">'+esc(it.name)+' · '+fmtSize(it.size)+'</div>' +
          '</div><div class="pact">'+acts+'</div></div>';
      }).join('');
      $('#doneFiles').innerHTML = html ||
        '<div class="prow"><div class="pinfo"><div class="pm">这次没有成品文件（可能是「只要鼓声」模式）</div></div></div>';
    }).catch(function(){
      $('#doneFiles').innerHTML = '<div class="prow"><div class="pinfo"><div class="pm">读取产物失败</div></div></div>';
    });
  }
  window.dqResultFor = fillResult;

  function renderDone(s){
    var base = stripExt(s.song);
    var meta = [];
    meta.push('<span class="k">歌曲</span><span class="v">'+esc(s.song)+'</span>');
    if(s.detected_bpm) meta.push('<span class="k">速度</span><span class="v">'+esc(s.detected_bpm)+' BPM</span>');
    if(s.device) meta.push('<span class="k">设备</span><span class="v">'+esc(s.device)+'</span>');
    if(s.started_at && s.finished){
      var a = new Date(String(s.started_at).replace(/-/g,'/')).getTime();
      var b = new Date(String(s.finished).replace(/-/g,'/')).getTime();
      if(a && b) meta.push('<span class="k">用时</span><span class="v">'+fmtDur((b-a)/1000)+'</span>');
    }
    /* 没用上真实拍点：别让它悄悄降级（如果是用户自己关掉了节拍网格，就别再提示） */
    var warn = $('#doneWarn');
    if(warn){
      var bs = String(s.beat_source || '');
      var gridOff = !!(cfg && (cfg.beatGrid === false || cfg.beatGrid === 'false'));
      if(bs && bs.indexOf('beat_this') !== 0 && !gridOff){
        warn.innerHTML = '⚠ 这次没能用上真实拍点（Beat This 不可用，退回按全曲一个速度记谱），小节可能与音乐对不上。' +
          '装好「节拍跟踪模型 Beat This」后重跑这首歌就会好 —— ' +
          '<button class="btn ghost sm" id="doneWarnFix" style="margin-left:6px">去安装</button>';
        warn.hidden = false;
        var fix = $('#doneWarnFix');
        if(fix) fix.onclick = function(){ if(window.dqOpenSetup) window.dqOpenSetup(); };
      }else{
        warn.hidden = true;
      }
    }

    var hasAudio = (s.results||[]).some(function(r){ return r.kind==='audio'; });
    /* 音频是第二产物：鼓声轨喂给播放器的「鼓声」音源 */
    if(hasAudio){
      var src = '/api/download?kind=audio&song='+encodeURIComponent(s.song)+'&preview=1';
      var au = $('#resAudio');
      if(au.getAttribute('src') !== src) au.src = src;
      window.__dqJobAudio = src;          /* 去过别的谱再回来，鼓声轨还能接上 */
    }else{
      $('#resAudio').removeAttribute('src');
      window.__dqJobAudio = '';
    }
    if(window.dqSyncSrc) window.dqSyncSrc();     /* 「原曲 / 鼓声」按钮可用性跟着产物变 */
    if(window.dqResultHist) window.dqResultHist(false, '');
    fillResult(base, meta.join(''));
  }

  function checkStall(s){
    var el = $('#stallWarn');
    if(!s || !s.running || !s.log_mtime){ hide(el); return; }
    var longStage = (s.stage >= 2 && s.stage <= 3);
    var limit = longStage ? 480 : 180;
    var idle = Math.floor(Date.now()/1000 - s.log_mtime);
    if(idle > limit){
      el.innerHTML = '⚠ 已 '+Math.floor(idle/60)+' 分钟没有新输出。' +
        (longStage ? '分离/转录阶段模型在后台计算，属正常；' : '') +
        '若 CPU 占用也是 0，可以取消后重试。';
      show(el);
    }else{
      hide(el);
    }
  }

  function startEnable(on){ $('#startBtn').disabled = !on || setupPending; }

  /* ================= 选择文件 ================= */
  function pickFile(file){
    if(!file) return;
    var fd = new FormData();
    fd.append('file', file);
    fetch('/api/upload', { method:'POST', body:fd }).then(function(r){
      return r.json().then(function(j){ return { ok:r.ok, j:j }; });
    }).then(function(res){
      var j = res.j || {};
      if(!j.ok){ toast('上传失败：'+(j.error||''), true); return; }
      curSong = j.name;
      curDur = null;
      dismissFinished();   /* 选了新歌：上一次的结果卡片不再自动弹出 */
      $('#pickedName').textContent = j.name;
      $('#pickedMeta').textContent = fmtSize(j.size) + ' · 已就绪';
      syncPickUI(true);
      $('#bpm').value = '';
      showView('pick');
      startEnable(!setupPending);
      fetch('/api/probe?song='+encodeURIComponent(j.name)).then(function(r){ return r.json(); })
        .then(function(p){
          if(p && p.duration_s){
            curDur = p.duration_s;
            $('#pickedMeta').textContent = fmtSize(p.size) + ' · 时长 ' + fmtDur(curDur);
            renderEta();
          }
        }).catch(function(){});
      toast('已选择：'+j.name);
    }).catch(function(e){ toast('上传请求失败：'+e, true); });
  }

  function resetToPick(clearSong){
    dismissedKey = sumKey;          /* 记住被关掉的那次结果 */
    sumKey = null;
    if(clearSong){ curSong = null; curDur = null; }
    syncPickUI(false);
    if(window.dqHidePlayer) window.dqHidePlayer();   /* 「再扒一首」= 从头开始，播放器跟着收起 */
    $('#bpm').value = '';
    showView('pick');
    setMark('idle');
    startEnable(!setupPending);
  }

  /* ================= 启动 / 取消 / 重试 ================= */
  function doStart(){
    if(!curSong){ toast('请先选择一首歌', true); return; }
    var raw = ($('#bpm').value || '').trim();
    var bpm = 0;
    if(raw){
      var v = Number(raw);
      if(!(v >= 40 && v <= 300)){ toast('BPM 需在 40–300 之间，留空则自动检测', true); return; }
      bpm = v;
    }
    stageSeen = {};
    stageDur = {};
    curStage = 0;
    sumKey = null;
    startEnable(false);
    setMark('busy');
    showView('run');
    $('#runSong').textContent = curSong;
    $('#runMeta').innerHTML = '';
    $('#runClock').innerHTML = '正在启动…';
    $('#bar').style.width = '0%';
    $('#log').textContent = '';
    renderSteps(1, false);
    fetch('/api/start', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({ song: curSong, bpm: bpm, mode: jobMode })
    }).then(function(r){ return r.json().then(function(j){ return { s:r.status, j:j }; }); })
    .then(function(res){
      if(!res.j.ok){
        toast('启动失败：'+(res.j.error||('HTTP '+res.s)), true);
        startEnable(true);
        showView('pick');
        setMark('idle');
      }
    }).catch(function(e){
      toast('启动请求失败：'+e, true);
      startEnable(true);
      showView('pick');
      setMark('idle');
    });
  }

  function doCancel(){
    if(!confirm('确定取消当前任务？\n中间产物会保留，可以重试。')) return;
    $('#cancelBtn').disabled = true;
    fetch('/api/cancel', { method:'POST' }).then(function(r){ return r.json(); }).then(function(j){
      if(!j.ok) toast('取消失败：'+(j.error||''), true);
      else toast('已请求取消');
    }).catch(function(e){ toast('取消请求失败：'+e, true); })
    .finally(function(){ setTimeout(function(){ $('#cancelBtn').disabled = false; }, 2500); });
  }

  function doRetry(){
    if(!curSong){
      /* 刷新后内存里没有歌曲名，用后端记录的 */
      curSong = (lastStatus && lastStatus.song) || null;
    }
    if(!curSong){ resetToPick(true); return; }
    doStart();
  }

  /* ================= 轮询 ================= */
  function poll(){
    fetch('/api/status').then(function(r){ return r.json(); }).then(function(s){
      applyStatus(s);
    }).catch(function(){
      /* 服务不通时不要每次弹提示，仅置灰；骨架屏要换成可重试的提示 */
      setMark('bad');
      var box = $('#history');
      if(box && !box.querySelector('.hrow')){
        box.innerHTML = '<div class="empty">读取失败<br><span class="hint">点「刷新」重试</span></div>';
      }
    });
  }

  /* ================= 历史 + 产物管理 ================= */
  window.dqLoadHistory = loadHistory;

  function loadHistory(){
    var box0 = $('#history');
    if (box0 && !box0.querySelector('.hrow')) {
      box0.innerHTML = '<div class="skel-row"><div class="skel skel-thumb"></div>' +
        '<div style="flex:1"><div class="skel skel-line" style="width:38%"></div>' +
        '<div class="skel skel-line" style="width:22%;margin-top:6px"></div></div></div>' +
        '<div class="skel-row"><div class="skel skel-thumb"></div>' +
        '<div style="flex:1"><div class="skel skel-line" style="width:30%"></div>' +
        '<div class="skel skel-line" style="width:18%;margin-top:6px"></div></div></div>';
    }
    fetch('/api/history?t=' + Date.now(), { cache: 'no-store' })
      .then(function(r){ return r.json(); }).then(function(items){
      var box = $('#history');
      if(!items || !items.length){ box.innerHTML = '<div class="empty">还没有扒过任何曲子<br><span class="hint">去「新建扒谱」拖一首歌进来</span></div>'; return; }
      var html = '';
      items.forEach(function(it){
        var base = it.base || it.name.replace(/_鼓谱\.pdf$/, '');
        var sb = esc(base);
        var hasPdf = /_鼓谱\.pdf$/.test(it.name) || it.has_pdf;
        html += '<div class="hrow" data-base="'+sb+'">' +
          (hasPdf ? '<img class="hthumb" alt="" data-thumb="'+sb+'" title="点击查看鼓谱">' : '') +
          '<div class="hname"><div class="nm">'+sb+'</div>' +
          '<div class="hm">'+esc(it.time)+' · '+fmtSize(it.size)+'</div></div>' +
          '<div class="hops">' +
          (hasPdf ? '<button class="btn sm" data-dyn="'+sb+'">看谱</button>' +
                    '<button class="btn ghost sm" data-pdf="'+sb+'">PDF</button>' : '') +
          '<button class="btn ghost sm" data-play="'+sb+'">▶ 试听</button>' +
          '<button class="btn ghost sm" data-artifacts="'+sb+'">产物…</button>' +
          '<button class="btn ghost sm" data-delsong="'+sb+'" title="删除这首歌的全部产物（源音频保留）">删除</button>' +
          '</div></div>';
      });
      box.innerHTML = html;
      /* 缩略图：第一页 @40dpi（服务端缓存，取不到就隐藏） */
      Array.prototype.forEach.call(box.querySelectorAll('[data-thumb]'), function(img){
        var b = img.dataset.thumb;
        img.onerror = function(){ img.dataset.empty = '1'; };
        img.onclick = function(){
          if(window.dqOpen) window.dqOpen(b); else sysPdf(b);
        };
        img.src = '/api/pdf/page?song='+encodeURIComponent(b)+'&page=1&dpi=40';
      });
    }).catch(function(){});
  }

  /* 展开某首歌的全部产物（成品 / 中间产物 / 源音频），每行可定位、保存、删除 */
  function toggleArtifacts(base, btn){
    var row = btn.closest('.hrow');
    var exist = row.querySelector('.afiles');
    if(exist){ row.removeChild(exist); return; }
    fetch('/api/artifacts?song='+encodeURIComponent(base)).then(function(r){ return r.json(); }).then(function(j){
      if(!j.ok){ toast('读取产物失败：'+(j.error||''), true); return; }
      var items = j.items || [];
      if(!items.length){
        toast('这首歌已经没有产物文件了');
        if(row.parentNode) row.parentNode.removeChild(row);   /* 空行直接从历史里去掉 */
        return;
      }
      var groups = [
        ['成品', ['pdf','xml','audio']],
        ['中间产物', ['stem','midi']],
        ['源音频', ['source']]
      ];
      var body = '';
      groups.forEach(function(g){
        var list = items.filter(function(x){ return g[1].indexOf(x.kind) >= 0; });
        if(!list.length) return;
        body += '<div class="asec">'+g[0]+'（'+list.length+'）</div>';
        list.forEach(function(x){
          var viewBtn = (x.kind === 'pdf')
            ? '<button class="btn ghost sm" data-pdf="'+esc(base)+'">查看</button>' : '';
          var playBtn = (x.kind === 'audio')
            ? '<button class="btn ghost sm" data-playsrc="'+esc(x.path)+'">试听</button>' : '';
          body += '<div class="arow">' +
            '<span class="anm" title="'+esc(x.path)+'">'+esc(x.name)+'</span>' +
            '<span class="asz">'+fmtSize(x.size)+'</span>' +
            '<span class="atime">'+esc(x.time)+'</span>' +
            '<span class="aops">' + viewBtn + playBtn +
              // 源音频是用户自己拖入的文件、且后端 download 路由不含 songs_dir，
              // 渲染"保存"必 404；只保留"定位"（在目录中打开）。
              (x.kind === 'source' ? '' :
                '<a class="btn ghost sm" href="/api/download?name='+encodeURIComponent(x.name)+'&song='+encodeURIComponent(base)+'">保存</a>') +
              '<button class="btn ghost sm" data-reveal="'+esc(x.path)+'">定位</button>' +
              '<button class="btn ghost sm" data-delitem="'+esc(x.name)+'" data-delbase="'+esc(base)+'">删除</button>' +
            '</span></div>';
        });
      });
      var panel = document.createElement('div');
      panel.className = 'afiles';
      panel.innerHTML = '<div class="ahead"><b>'+esc(base)+'</b><span>（共 '+items.length+' 个文件）</span>' +
        '<span class="sp"></span>' +
        '<button class="btn ghost sm" data-reveal="'+esc(items[0].path.replace(/[\\/][^\\/]*$/, ''))+'">打开目录</button>' +
        '<button class="btn ghost sm" data-delsong="'+esc(base)+'">全部删除</button></div>' + body;
      row.appendChild(panel);
    }).catch(function(e){ toast('读取产物失败：'+e, true); });
  }

  function deleteSong(base){
    if(!confirm('删除「'+base+'」的全部产物？\n（成品 + 中间产物；源音频默认保留）')) return;
    fetch('/api/delete', { method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({ song: base, keep_source: true }) }).then(function(r){ return r.json(); })
      .then(function(j){
        if(!j.ok){ toast('删除失败：'+(j.error||''), true); return; }
        toast('已删除 '+(j.removed||[]).length+' 个文件');
        loadHistory();
      }).catch(function(e){ toast('删除失败：'+e, true); });
  }

  function deleteItem(base, name){
    if(!confirm('删除文件「'+name+'」？')) return;
    fetch('/api/delete', { method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({ song: base, name: name, keep_source: false }) }).then(function(r){ return r.json(); })
      .then(function(j){
        if(!j.ok){ toast('删除失败：'+(j.error||''), true); return; }
        toast('已删除 '+name);
        refreshArtifacts(base);   /* 只重画这个面板，不整体刷新历史（否则面板被冲掉） */
      }).catch(function(e){ toast('删除失败：'+e, true); });
  }

  /* 删除单个文件后重画面板（不重建整个历史列表） */
  function refreshArtifacts(base){
    var row = document.querySelector('.hrow[data-base="'+CSS.escape(base)+'"]');
    if(!row) return;
    var old = row.querySelector('.afiles');
    if(old) row.removeChild(old);
    var btn = row.querySelector('[data-artifacts]');
    if(btn) toggleArtifacts(base, btn);
  }

  function revealPath(p){
    fetch('/api/reveal', { method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({ path: p }) }).then(function(r){ return r.json(); }).then(function(j){
      if(!j.ok) toast('打不开：'+(j.error||''), true);
    }).catch(function(e){ toast('打不开：'+e, true); });
  }

  function playFromPath(path, btn){
    /* 中间产物试听：把路径转成下载地址直接播 */
    var a = document.querySelector('.afiles audio');
    if(a) a.parentNode.removeChild(a);
    var wrap = document.createElement('audio');
    wrap.controls = true;
    wrap.preload = 'metadata';
    wrap.src = '/api/download?name=' + encodeURIComponent(path.replace(/^.*[\\/]/, '')) + '&preview=1';
    btn.closest('.arow').appendChild(wrap);
    wrap.play();
  }

  function playHistory(base, btn){
    var row = btn.closest('.hrow');
    var bars = document.querySelectorAll('.hrow .audiorow');
    var existing = row.querySelector('.audiorow');
    if(existing){
      var a0 = existing.querySelector('audio');
      if(a0.paused) a0.play(); else a0.pause();
      return;
    }
    Array.prototype.forEach.call(bars, function(b){ b.parentNode.removeChild(b); });
    var wrap = document.createElement('div');
    wrap.className = 'audiorow';
    wrap.innerHTML = '<audio controls preload="metadata" src="/api/download?kind=audio&song=' +
      encodeURIComponent(base) + '&preview=1"></audio>';
    row.appendChild(wrap);
    var a = wrap.querySelector('audio');
    a.addEventListener('play', function(){ btn.textContent = '⏸ 暂停'; });
    a.addEventListener('pause', function(){ btn.textContent = '▶ 试听'; });
    a.addEventListener('ended', function(){ btn.textContent = '▶ 试听'; });
    a.play();
  }

  /* ================= 用系统阅读器打开 PDF（应用内不再有第二个谱面视图） ================= */
  function sysPdf(name){
    var base = stripExt(name);
    var out = (cfg && cfg.resolved && cfg.resolved.output) || '';
    var path = out ? (out.replace(/[\\\/]+$/, '') + '\\' + base + '_鼓谱.pdf') : '';
    var fallback = function(){
      var a = document.createElement('a');
      a.href = '/api/download?kind=pdf&song=' + encodeURIComponent(base);
      a.rel = 'noopener';
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
    };
    if(!path){ fallback(); return; }
    fetch('/api/open', { method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({ path: path }) })
      .then(function(r){ return r.json(); })
      .then(function(o){
        if(!o || !o.ok){
          toast('系统阅读器打不开，已改为下载：' + ((o && o.error) || ''), true);
          fallback();
        }
      })
      .catch(function(){ fallback(); });
  }
  window.dqOpenPdfFile = sysPdf;

  /* ================= 设置 ================= */
  function renderSettings(){
    if(!cfg) return;
    function fieldHtml(f){
      var key = f[0], label = f[1], full = f[2], type = f[3];
      var val = (cfg[key] != null) ? cfg[key] : '';
      var eff = (cfg.resolved && cfg.resolved[key]) ? ('生效：'+cfg.resolved[key]) : '';
      var ctrl;
      if(type === 'bool'){
        /* 布尔项：空值按"开启"处理（与后端默认一致） */
        var on = (val === '' || val === true || val === 'true' || val === 1 || val === '1');
        ctrl = '<select class="inp wide" id="cfg_'+key+'">' +
               '<option value="true"'+(on?' selected':'')+'>开启</option>' +
               '<option value="false"'+(!on?' selected':'')+'>关闭</option>' +
               '</select>';
      } else {
        ctrl = '<input class="inp wide" id="cfg_'+key+'" type="'+(type||'text')+'" value="'+esc(val)+'" placeholder="（默认）">';
      }
      return '<div class="field'+(full?' full':'')+'">' +
        '<label>'+esc(label)+'</label>' + ctrl +
        (eff ? '<span class="eff">'+esc(eff)+'</span>' : '') +
        '</div>';
    }
    $('#gridCommon').innerHTML = SET_FIELDS.filter(function(f){ return f[4]==='common'; }).map(fieldHtml).join('');
    $('#gridAdv').innerHTML = SET_FIELDS.filter(function(f){ return f[4]==='adv'; }).map(fieldHtml).join('');
    renderSettingsEngineLine();
  }

  /* ---------- 分离模型面板 ---------- */
  var modelsData = null;
  var modelSeq = 0;
  var modelTimer = null;

  function loadModels(cb){
    fetch('/api/models').then(function(r){ return r.json(); }).then(function(j){
      modelsData = j;
      renderModels();
      if(cb) cb(j);
    }).catch(function(){});
  }

  function stars(n){
    n = Math.max(1, Math.min(5, Number(n) || 3));
    return '<span class="stars" title="效果等级">' + '★'.repeat(n) + '☆'.repeat(5 - n) + '</span>';
  }

  function renderModels(){
    var box = $('#modelRows');
    if(!box) return;
    if(!modelsData){ box.innerHTML = '<div class="empty" style="padding:4px 0">读取中…</div>'; return; }

    var vram = (gpu && gpu.available) ? (gpu.vram_mb || 0) : 0;
    var onGpu = !!(gpu && gpu.available);
    /* 按显存分档：4–6GB 用默认（最快最稳）；6–12GB 可上 FT（均衡）；12GB+ 才推 MDX-Extra（镲片最准但慢 3 倍） */
    var recId = !onGpu ? 'htdemucs'
              : (vram >= 12288 ? 'mdx_extra'
              : (vram >= 6144 ? 'htdemucs_ft' : 'htdemucs'));
    var recLabel = '';
    var modelLabel = function(id){
      var hit = null;
      (modelsData.models || []).forEach(function(x){ if(x.id === id) hit = x; });
      return hit ? hit.label : id;
    };
    recLabel = modelLabel(recId);

    var html = '';
    modelsData.models.forEach(function(m){
      var meas = m.measured || {};
      var fits = !onGpu ? true : (!m.min_vram_mb || vram >= m.min_vram_mb);
      var st = m.installed
        ? '<span class="badge ok">已安装' + (m.size_mb ? ' · ' + m.size_mb + 'MB' : '') + '</span>'
        : '<span class="badge miss">未下载' + (m.total_mb ? ' · 约' + m.total_mb + 'MB' : '') + '</span>';
      var act = '';
      if(m.installed){
        act = (m.id === modelsData.current)
          ? '<span class="badge req">使用中</span>'
          : '<button class="btn sm" data-msel="' + esc(m.id) + '"' + (fits ? '' : ' disabled title="显存可能不够"') + '>切换到此</button>';
        if(m.id !== 'htdemucs'){
          act += ' <button class="btn ghost sm" data-mdel="' + esc(m.id) + '">删除</button>';
        }
      }else{
        act = '<button class="btn sm" data-mdl="' + esc(m.id) + '">下载</button>';
      }
      var meta = [];
      if(m.best_for) meta.push('适用：' + m.best_for);
      if(meas.f1) meta.push('实测 F1 ' + meas.f1 + '（逐类）');
      if(meas.f1_B) meta.push('验证段 ' + meas.f1_B);
      if(m.speed_x) meta.push('速度 ' + m.speed_x + '×');
      if(m.min_vram_mb) meta.push('显存 ≥' + Math.ceil(m.min_vram_mb / 1024) + 'GB');
      var warn = fits ? '' : '<div class="cl no"><span class="lb">注意</span><span>你的显卡显存约 ' +
        (vram / 1024).toFixed(1) + 'GB，跑它可能变慢或 OOM</span></div>';
      html += '<div class="ctlrow" style="' + (m.advanced ? 'opacity:.75' : '') + '">' +
        '<div class="ci2"><div class="n">' + esc(m.label) +
        ' ' + stars(m.stars) +
        (m.id === recId ? ' <span class="badge req">本机推荐</span>' : (m.advanced ? ' <span class="badge opt">实验</span>' : '')) +
        '</div><div class="d">' + esc(m.desc) + (meta.length ? ' · ' + meta.join(' · ') : '') + '</div>' + warn + '</div>' +
        st + ' ' + act + '</div>';
    });
    var tier = !onGpu
      ? '未检测到可用 NVIDIA 显卡：分离走 CPU，<b>HTDemucs</b> 最快最稳，其它模型在 CPU 上慢 3–4 倍。'
      : '你的显卡 <b>' + esc(gpu.name) + '</b> 显存 <b>' + (vram / 1024).toFixed(1) + 'GB</b>，本机推荐 <b>' + esc(recLabel) + '</b>。';
    var vramLine = '<div class="note info" style="margin:10px 0 0"><span>i</span><div>' + tier +
      '<br>按显存分档：<b>4–6GB</b> → HTDemucs（快，够用）· <b>6–12GB</b> → HTDemucs-FT（均衡）· ' +
      '<b>12GB+</b> → MDX-Extra（镲片最准）。<br>' +
      '效果等级见星级（按实测 F1 与镲片吻合度评定，差距多在 1% 以内，换模型救不了错谱，先调 ADTOF 与规则引擎）。<br>' +
      '转录阈值已按模型实测调优（底鼓/踩镲更灵敏、嗵鼓更严），可在「常用设置」里手动覆盖。</div></div>';
    box.innerHTML = html + vramLine;
  }

  function watchModelDownload(){
    modelSeq = 0;
    if(modelTimer) clearInterval(modelTimer);
    var lastLine = '';
    modelTimer = setInterval(function(){
      fetch('/api/setup/progress?last_seq=' + modelSeq).then(function(r){ return r.json(); }).then(function(j){
        if(j.log && j.log.length){
          j.log.forEach(function(l){
            lastLine = l.text;
            modelSeq = Math.max(modelSeq, l.seq);
          });
        }
        var msg = $('#modelMsg');
        if(msg) msg.textContent = (j.running ? '下载中… ' : '') + (lastLine || '');
        if(!j.running && j.done){
          clearInterval(modelTimer); modelTimer = null;
          if(msg) msg.textContent = j.ok ? '下载完成 ✓' : '下载失败，详情见 运行日志/安装日志';
          loadModels();
        }
      }).catch(function(){});
    }, 1500);
  }

  function modelAction(url, body, msgEl){
    if(msgEl) msgEl.textContent = '请稍等…';
    fetch(url, { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body) })
      .then(function(r){ return r.json().then(function(j){ return {s:r.status, j:j}; }); })
      .then(function(res){
        if(!res.j.ok){
          if(msgEl) msgEl.textContent = '失败：' + (res.j.error || ('HTTP ' + res.s));
          return;
        }
        if(url.indexOf('download') >= 0){
          if(msgEl) msgEl.textContent = '开始下载…';
          watchModelDownload();
        }else{
          if(msgEl) msgEl.textContent = '已完成';
          loadModels();
        }
      }).catch(function(e){ if(msgEl) msgEl.textContent = '请求失败：' + e; });
  }

  document.addEventListener('click', function(e){
    var dl = e.target.closest('[data-mdl]');
    if(dl){ modelAction('/api/models/download', { model: dl.dataset.mdl }, $('#modelMsg')); return; }
    var sel = e.target.closest('[data-msel]');
    if(sel){ modelAction('/api/models/select', { model: sel.dataset.msel }, $('#modelMsg')); return; }
    var del = e.target.closest('[data-mdel]');
    if(del){
      if(!confirm('删除这个模型的权重文件？以后想用要重新下载。')) return;
      modelAction('/api/models/delete', { model: del.dataset.mdel }, $('#modelMsg'));
    }
  });

  function renderSettingsEngineLine(){
    var el = $('#engineLine');
    if(!el) return;
    if(!gpu){ el.innerHTML = 'GPU 状态：检测中…'; return; }
    if(gpu.status !== 'ready'){ el.innerHTML = 'GPU 状态：检测中…'; return; }
    if(gpu.available){
      el.innerHTML = '<span>分离引擎：<b>'+esc(gpu.name)+'</b> · '+gpu.vram_mb+' MB 显存 · torch '+esc(gpu.torch||'')+
        (gpu.cuda?(' · CUDA '+esc(gpu.cuda)):'')+'</span>';
    }else{
      el.innerHTML = '<span>分离引擎：<b>仅 CPU</b> · ' + esc(gpu.reason || '未检测到可用 GPU') + '</span>';
    }
  }

  function saveSettings(){
    var body = {};
    SET_FIELDS.forEach(function(f){
      var el = document.getElementById('cfg_'+f[0]);
      if(!el) return;
      var v = el.value.trim();
      if(f[3] === 'bool'){ body[f[0]] = (v === 'true'); }
      else { body[f[0]] = (v === '') ? '' : (f[3]==='number' ? Number(v) : v); }
    });
    $('#cfgMsg').textContent = '保存中…';
    fetch('/api/config', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body) })
      .then(function(r){ return r.json(); })
      .then(function(j){
        if(!j.ok){ $('#cfgMsg').textContent = ''; toast('保存失败：'+(j.error||''), true); return; }
        $('#cfgMsg').textContent = '已保存';
        toast('设置已保存');
        loadConfig();
      }).catch(function(e){ $('#cfgMsg').textContent=''; toast('保存请求失败：'+e, true); });
  }

  function loadConfig(){
    return fetch('/api/config').then(function(r){ return r.json(); }).then(function(j){
      cfg = j;
      devPref = j.demucsDevice || 'auto';
      if(j.gpu) gpu = j.gpu;
      $('#footPaths').textContent = 'songs  ' + j.resolved.songs + '   ·   output  ' + j.resolved.output + '   ·   midis  ' + j.resolved.midis;
      $('#outPathHint').textContent = j.resolved.output;
      renderSettings();
      renderEngine();
    }).catch(function(){});
  }

  /* ================= 组件 / 能力 / 安装 ================= */
  var setupInfo = null;              /* /api/setup/status 全量结果 */
  var setupChosen = {};              /* 勾选中的组件 id -> true */
  var setupPresetName = 'recommended';

  function capOk(key){
    if(!setupInfo) return true;
    var c = (setupInfo.capabilities||[]).filter(function(x){ return x.key===key; })[0];
    return c ? !!c.ok : true;
  }

  /* 组件元信息 */
  function compById(id){
    return (setupInfo && setupInfo.components || []).filter(function(c){ return c.id===id; })[0] || null;
  }

  /* 能力清单（设置弹窗顶部 + 安装完成页） */
  function renderCapList(){
    var box = $('#capList');
    if(!box) return;
    if(!setupInfo){ box.innerHTML = '<span class="cap">读取中…</span>'; return; }
    box.innerHTML = (setupInfo.capabilities||[]).map(function(c){
      var cls = c.ok ? 'ok' : 'no';
      var txt = c.ok ? c.name : (c.name + ' → ' + (c.fallback || '不可用'));
      return '<span class="cap '+cls+'" title="'+esc(c.note||'')+'"><span class="ci">'+(c.ok?'✓':'✗')+'</span>'+esc(txt)+'</span>';
    }).join('');
  }

  /* 设置里的组件表：每行一个状态 + 单独安装/修复 */
  function renderCtlRows(){
    var box = $('#ctlRows');
    if(!box) return;
    if(!setupInfo){ box.innerHTML = ''; return; }
    box.innerHTML = (setupInfo.components||[]).map(function(c){
      var badge = c.ok
        ? '<span class="badge ok">已就绪</span>'
        : '<span class="badge miss">缺失</span>';
      var btn = c.ok
        ? '<button class="btn ghost sm" data-install="'+esc(c.id)+'">重装</button>'
        : '<button class="btn sm" data-install="'+esc(c.id)+'">'+(c.required?'安装':'补装')+'</button>';
      return '<div class="ctlrow">' +
        '<div class="ci2"><div class="n">'+esc(c.name)+(c.required?' <span class="badge req">必需</span>':'')+'</div>' +
        '<div class="d">'+esc(c.desc||'')+' · '+esc(c.size||'')+(c.ok?'':' → 不装：'+esc(c.without||''))+'</div></div>' +
        badge + btn + '</div>';
    }).join('');
  }

  /* 首次加载/安装后统一刷新（force=true 时绕过服务端 60 秒缓存） */
  function loadSetupStatus(cb, force){
    var url = '/api/setup/status' + (force ? '?refresh=1' : '');
    fetch(url).then(function(r){ return r.json(); }).then(function(j){
      setupInfo = j;
      setupCompsData = j.components || [];
      setupPending = !j.can_transcribe;          /* 只有"必需组件缺失"才算未就绪 */
      applyGating();
      renderCapList();
      renderCtlRows();
      if(cb) cb(j);
    }).catch(function(){ if(cb) cb(null); });
  }

  /* 主界面的能力门禁：必需缺失 -> 阻断；可选缺失 -> 一行提示 */
  function applyGating(){
    var blocker = $('#blocker');
    var hint = $('#optMissing');
    var missingReq = (setupInfo && setupInfo.missing_required) || [];
    var missingOpt = (setupInfo && setupInfo.missing_optional) || [];

    if(setupPending){
      var names = missingReq.map(function(id){ var c = compById(id); return c ? c.name : id; });
      blocker.innerHTML = '<span class="bi">⚠</span><div><b>还差 '+names.length+' 个必需组件，装好才能扒谱</b><br>' +
        esc(names.join('、')) + '<div style="margin-top:10px"><button class="btn primary sm" id="blockerInstall">立即安装</button></div></div>';
      show(blocker);
      hide(hint);
      $('#startBtn').disabled = true;
      $('#startBtn').title = '组件未就绪';
      var bi = $('#blockerInstall');
      if(bi) bi.onclick = function(){ openSetup(false, false); };
      show($('#setupBanner'));
    }else{
      hide(blocker);
      hide($('#setupBanner'));
      $('#startBtn').disabled = false;
      $('#startBtn').title = '';

      if(missingOpt.length){
        var fallbacks = (setupInfo.capabilities||[]).filter(function(c){ return !c.ok; })
          .map(function(c){ return c.name + ' → ' + (c.fallback||''); });
        hint.innerHTML = '⚠ 部分功能未启用：' + esc(fallbacks.join('；')) +
          '　<a href="#" id="optMissingInstall">去补装</a>';
        show(hint);
        var om = $('#optMissingInstall');
        if(om) om.onclick = function(e){ e.preventDefault(); openSetup(true, false); };
      }else{
        hide(hint);
      }
    }
  }

  /* ---------------- 安装向导 ---------------- */
  function openSetup(manual, resume){
    if(!setupInfo){
      if(window.dqOpenSetup) window.dqOpenSetup(); else show($('#setupModal'));
      $('#setupMsg').textContent = '正在读取组件状态…';
      loadSetupStatus(function(){ openSetup(manual, resume); });
      return;
    }
    $('#setupTitle').textContent = manual ? '安装 / 修复组件' : (setupPending ? '首次使用，先装组件' : '安装向导');
    $('#setupNote').innerHTML = manual
      ? '已就绪的组件默认不勾选；想强制重装就勾上。'
      : '想装什么就装什么 —— 每项都写清了"装了能得到什么 / 不装会怎样"。只有标「必需」的三项是真的不可省。';
    selectPreset(manual ? null : 'recommended');
    renderSetupPaths();
    show($('#setupStep1')); hide($('#setupStep2')); hide($('#setupStep3'));
    $('#setupStartBtn').hidden = false; $('#setupStartBtn').disabled = false;
    $('#setupDoneBtn').hidden = true; $('#setupBackBtn').hidden = true;
    $('#setupMsg').textContent = '';
    /* 走外壳路由：否则「设置页 + 向导页」会同时可见，侧栏高亮也会留在设置 */
    if(window.dqOpenSetup) window.dqOpenSetup();
    else { show($('#setupModal')); hide($('#setModal')); }
    if(resume) startInstall(null, true);
  }

  /* 预设：最小 / 推荐 / 全部 */
  function selectPreset(name){
    setupPresetName = name;
    var ids;
    if(name === 'minimal') ids = (setupInfo.presets||{}).minimal || ['venv','pip','adtof'];
    else if(name === 'all') ids = (setupInfo.components||[]).map(function(c){ return c.id; });
    else if(name === 'recommended') ids = (setupInfo.presets||{}).recommended || [];
    else ids = null;

    Array.prototype.forEach.call(document.querySelectorAll('#setupPresets .preset'), function(b){
      b.classList.toggle('on', name && b.dataset.preset === name);
    });

    setupChosen = {};
    (setupInfo.components||[]).forEach(function(c){
      if(c.ok) return;                       /* 已就绪默认不勾 */
      if(c.skip_by_default) return;           /* 历史遗留默认不勾 */
      if(ids) { if(ids.indexOf(c.id) >= 0) setupChosen[c.id] = true; }
      else setupChosen[c.id] = true;
    });
    renderSetupComps();
  }

  function renderSetupComps(){
    var box = $('#setupComps');
    box.innerHTML = (setupInfo.components||[]).map(function(c){
      var checked = !!setupChosen[c.id];
      var cls = 'comp' + (c.ok ? ' ready' : '') + (c.skip_by_default ? ' off' : '');
      var badges = '';
      if(c.ok) badges += '<span class="badge ok">已就绪</span>';
      if(c.required) badges += '<span class="badge req">必需</span>';
      if(c.manual) badges += '<span class="badge opt">需手动</span>';
      if(c.skip_by_default) badges += '<span class="badge opt">可不装</span>';
      return '<label class="'+cls+'">' +
        '<input type="checkbox" value="'+esc(c.id)+'"'+(checked?' checked':'')+(c.ok?'':'')+'>' +
        '<div class="cbody">' +
          '<div class="crow"><span class="cn">'+esc(c.name)+'</span>' +
            '<span class="csz">'+esc(c.size||'')+'</span>'+badges+'</div>' +
          '<div class="cd">'+esc(c.desc||'')+'</div>' +
          (c.ok ? '' :
            '<div class="cl yes"><span class="lb">装了</span><span>'+esc(c.feature||'')+'</span></div>' +
            '<div class="cl no"><span class="lb">不装</span><span>'+esc(c.without||'')+'</span></div>') +
        '</div></label>';
    }).join('');

    /* 勾选变化 -> 更新合计 */
    Array.prototype.forEach.call(box.querySelectorAll('input[type=checkbox]'), function(inp){
      inp.addEventListener('change', function(){
        if(inp.checked) setupChosen[inp.value] = true; else delete setupChosen[inp.value];
        Array.prototype.forEach.call(document.querySelectorAll('#setupPresets .preset'), function(b){ b.classList.remove('on'); });
        renderSetupTotal();
      });
    });
    renderSetupTotal();
  }

  function parseMB(s){
    if(!s) return 0;
    var m = String(s).match(/([\d.]+)\s*(GB|MB)/i);
    if(!m) return 0;
    var v = parseFloat(m[1]);
    return /gb/i.test(m[2]) ? v*1024 : v;
  }
  function renderSetupTotal(){
    var ids = Object.keys(setupChosen);
    var mb = ids.reduce(function(a,id){ var c = compById(id); return a + (c?parseMB(c.size):0); }, 0);
    var txt;
    if(!ids.length){
      txt = '没有选中任何组件 —— 什么都不装也能用已有的功能。';
    }else{
      var names = ids.map(function(id){ var c = compById(id); return c ? c.name : id; });
      txt = '将安装 ' + ids.length + ' 项' + (mb ? '，下载约 ' + (mb>=1024 ? (mb/1024).toFixed(1)+' GB' : Math.round(mb)+' MB') : '') +
        '：' + names.join('、');
    }
    $('#setupTotal').textContent = txt;
    var btn = $('#setupStartBtn');
    btn.disabled = ids.length === 0;
  }

  function renderSetupPaths(){
    $('#setupPaths').innerHTML = SETUP_PATHS.map(function(f){
      var val = (cfg && cfg[f[0]] != null) ? cfg[f[0]] : '';
      var eff = (cfg && cfg.resolved && cfg.resolved[f[0]]) ? ('生效：'+cfg.resolved[f[0]]) : '';
      return '<div class="field'+(f[2]?' full':'')+'"><label>'+esc(f[1])+'</label>' +
        '<input class="inp wide" id="setup_'+f[0]+'" type="text" value="'+esc(val)+'" placeholder="（默认）">' +
        (eff?'<span class="eff">'+esc(eff)+'</span>':'') + '</div>';
    }).join('');
  }

  function startInstall(ids, resume){
    var list = ids;
    if(!list){
      list = Object.keys(setupChosen);
      if(!list.length){ $('#setupMsg').textContent = '请至少勾选一项'; return; }
    }
    var paths = {};
    SETUP_PATHS.forEach(function(f){
      var el = document.getElementById('setup_'+f[0]);
      if(!el) return;
      var v = el.value.trim();
      if(v) paths[f[0]] = v;
    });
    $('#setupStartBtn').disabled = true;
    $('#setupMsg').textContent = '正在启动…';
    /* 关键：先记下本次安装的起点，并清掉"见过 running"的标记。
       否则第一轮轮询可能读到上一次安装残留的 done 状态，直接跳到完成页。 */
    setupSawRunning = false;
    setupInstallAt = Math.floor(Date.now() / 1000);
    var p = resume ? Promise.resolve({ok:true, components:list}) : fetch('/api/setup/start', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({ components: list, paths: paths })
    }).then(function(r){ return r.json(); });
    p.then(function(j){
      if(!j.ok){ $('#setupMsg').textContent = '启动失败：'+(j.error||''); $('#setupStartBtn').disabled = false; return; }
      setupInstallingIds = j.components || list;
      hide($('#setupStep1')); show($('#setupStep2')); hide($('#setupStep3'));
      $('#setupStartBtn').hidden = true; $('#setupDoneBtn').hidden = true; $('#setupBackBtn').hidden = true;
      $('#setupLog').textContent = '';
      $('#setupBar').style.width = '0%';
      setupLogSeq = 0;
      if(setupTimer) clearInterval(setupTimer);
      setupTimer = setInterval(pollSetup, 1200);
      pollSetup();
    }).catch(function(e){ $('#setupMsg').textContent = '启动失败：'+e; $('#setupStartBtn').disabled = false; });
  }
  var setupInstallingIds = [];
  var setupSawRunning = false;
  var setupInstallAt = 0;

  function pollSetup(){
    fetch('/api/setup/progress?last_seq='+setupLogSeq).then(function(r){ return r.json(); }).then(function(j){
      if(j.log && j.log.length){
        j.log.forEach(function(l){ $('#setupLog').textContent += l.text + '\n'; });
        setupLogSeq = Math.max.apply(null, j.log.map(function(l){ return l.seq; }));
        $('#setupLog').scrollTop = $('#setupLog').scrollHeight;
      }
      var total = (j.steps && j.steps.length) ? j.steps[0].total : 0;
      var idx = Math.min((Number(j.step_index)||0) + 1, total || 1);
      if(total){
        $('#setupBar').style.width = Math.round(idx/total*100) + '%';
        $('#setupStatus').innerHTML = '<span>第 '+idx+' / '+total+' 步：<b>'+esc(j.current||'')+'</b></span>';
      }else{
        $('#setupStatus').innerHTML = '<span>'+esc(j.current||'准备中…')+'</span>';
      }
      if(j.running) setupSawRunning = true;
      /* 只有当"本次安装确实跑过"或"服务端记录的起始时间不早于本次点击"时，才认这个完成状态 */
      var freshRun = j.started_at && (Number(j.started_at) >= setupInstallAt - 3);
      if(!j.running && (j.done || j.started_at) && (setupSawRunning || freshRun)){
        if(setupTimer){ clearInterval(setupTimer); setupTimer = null; }
        finishSetup(j);
      }
    }).catch(function(){});
  }

  function finishSetup(j){
    hide($('#setupStep2')); show($('#setupStep3'));
    $('#setupStartBtn').hidden = true;
    $('#setupDoneBtn').hidden = false;
    $('#setupBackBtn').hidden = false;
    $('#setupBar').style.width = '100%';
    $('#setupMsg').textContent = '';
    var failed = (j.failed||[]).map(nameOf);
    var manual = (j.manual||[]).map(nameOf);
    var head = j.ok
      ? '<div class="note ok" style="margin-top:0"><span>✓</span><div><b>安装完成</b></div></div>'
      : '<div class="note warn" style="margin-top:0"><span>!</span><div><b>有组件没装成</b>' +
        (failed.length ? '<br>失败：'+esc(failed.join('、')) : '') +
        (manual.length ? '<br>需手动处理：'+esc(manual.join('、')) : '') + '</div></div>';
    /* 先给个占位：重新检测要 spawn 一次 python（约 4-5 秒），不能白屏等 */
    $('#setupResult').innerHTML = head +
      '<div class="sect" style="margin-top:18px">现在能做什么</div>' +
      '<div class="caplist"><span class="cap">正在重新检测组件状态…</span></div>';
    /* 装完强制重查（绕过服务端缓存），把"现在能干什么"直接摆出来 */
    loadSetupStatus(function(fresh){
      var caps = (fresh && fresh.capabilities) || [];
      var lines = caps.map(function(c){
        return '<span class="cap '+(c.ok?'ok':'no')+'" title="'+esc(c.note||'')+'"><span class="ci">'+(c.ok?'✓':'!')+
          '</span>'+esc(c.ok ? c.name : (c.name + ' → ' + (c.fallback||'不可用')))+'</span>';
      }).join('') || '<span class="cap">检测失败，请稍后重试</span>';
      var extra = '';
      if(fresh && fresh.missing_required && fresh.missing_required.length){
        extra = '<div class="note warn" style="margin-top:14px"><span>⚠</span><div>还有必需组件没装好，暂时不能扒谱。稍后可在「设置 → 能力与组件」里继续补装。</div></div>';
      }else if(fresh && fresh.missing_optional && fresh.missing_optional.length){
        var names = fresh.missing_optional.map(function(id){ var c = compById(id); return c ? c.name : id; }).join('、');
        extra = '<div class="note info" style="margin-top:14px"><span>i</span><div>仍可正常使用；没装的是：'+esc(names)+
          '（对应功能会走降级方案，随时可在设置里补装）。</div></div>';
      }
      $('#setupResult').innerHTML = head +
        '<div class="sect" style="margin-top:18px">现在能做什么</div>' +
        '<div class="caplist">'+lines+'</div>' + extra;
    }, true);
  }

  function nameOf(id){
    var c = compById(id);
    return c ? c.name : id;
  }

  /* 设置里点单个组件的"安装/重装" */
  function installOne(id){
    var c = compById(id);
    if(!c) return;
    if(c.manual && !confirm(c.name + ' 通常需要手动安装（例如 MuseScore 要管理员权限）。\n仍要尝试自动安装吗？')) return;
    setupChosen = {};
    setupChosen[id] = true;
    if(window.dqOpenSetup) window.dqOpenSetup(); else { show($('#setupModal')); hide($('#setModal')); }
    $('#setupStep1').hidden = true; $('#setupStep2').hidden = false; $('#setupStep3').hidden = true;
    startInstall([id], false);
  }


  /* ================= 事件绑定 ================= */
  var dropEl = $('#drop'), fileInput = $('#file');

  dropEl.addEventListener('click', function(){ fileInput.click(); });

  /* 键盘可达：Enter / 空格 等同点击 */
  dropEl.addEventListener('keydown', function (ev) {
    if (ev.key === 'Enter' || ev.key === ' ' || ev.code === 'Space') {
      ev.preventDefault();
      dropEl.click();
    }
  });
  dropEl.addEventListener('dragover', function(e){ e.preventDefault(); dropEl.classList.add('over'); });
  dropEl.addEventListener('dragleave', function(){ dropEl.classList.remove('over'); });
  dropEl.addEventListener('drop', function(e){
    e.preventDefault(); dropEl.classList.remove('over');
    if(e.dataTransfer.files.length) pickFile(e.dataTransfer.files[0]);
  });
  fileInput.addEventListener('change', function(){
    if(fileInput.files.length) pickFile(fileInput.files[0]);
    fileInput.value = '';
  });

  $('#changeFileBtn').addEventListener('click', function(){ curSong = null; curDur = null; resetToPick(true); });
  $('#startBtn').addEventListener('click', doStart);
  $('#cancelBtn').addEventListener('click', doCancel);
  $('#retryBtn').addEventListener('click', doRetry);
  $('#againBtn').addEventListener('click', function(){ curSong=null; curDur=null; resetToPick(true); });
  $('#failAgainBtn').addEventListener('click', function(){ curSong=null; curDur=null; resetToPick(true); });

  $('#openFolderBtn').addEventListener('click', function(){
    var p = (cfg && cfg.resolved && cfg.resolved.output) || '';
    if(p) revealPath(p);
  });

  /* 作业模式：完整 / 只要谱子 / 只要音频 / 只要鼓声 */
  $('#modeSwitch').addEventListener('click', function(e){
    var b = e.target.closest('.jobmode');
    if(!b || b.disabled) return;
    jobMode = b.dataset.mode;
    renderMode();
  });

  $('#devSwitch').addEventListener('click', function(e){
    var btn = e.target.closest('button');
    if(!btn || btn.disabled) return;
    devPref = (btn.dataset.dev === 'gpu') ? 'cuda' : btn.dataset.dev;
    renderEngine();
    fetch('/api/config', { method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({ demucsDevice: devPref }) }).catch(function(){});
  });

  /* 历史/产物按钮（事件委托，避免文件名里的引号把 inline handler 打断） */
  document.addEventListener('click', function(e){
    var playBtn = e.target.closest('[data-play]');
    if(playBtn){ playHistory(playBtn.dataset.play, playBtn); return; }
    var dynBtn = e.target.closest('[data-dyn]');
    if(dynBtn){
      if(window.dqOpen) window.dqOpen(dynBtn.dataset.dyn);
      else sysPdf(dynBtn.dataset.dyn);
      return;
    }
    var pdfBtn = e.target.closest('[data-pdf]');
    if(pdfBtn){ sysPdf(pdfBtn.dataset.pdf); return; }
    var pvBtn = e.target.closest('[data-pv]');
    if(pvBtn){
      var pvBox = $('#donePv');
      if(pvBox && !pvBox.hidden) pvBox.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
      return;
    }
    var tryBtn = e.target.closest('[data-try]');
    if(tryBtn){
      var u = '';
      try { u = decodeURIComponent(tryBtn.dataset.try || ''); } catch (x) { u = tryBtn.dataset.try || ''; }
      if(window.dqPlayProduct) window.dqPlayProduct(u);
      return;
    }
    var artBtn = e.target.closest('[data-artifacts]');
    if(artBtn){ toggleArtifacts(artBtn.dataset.artifacts, artBtn); return; }
    var delSong = e.target.closest('[data-delsong]');
    if(delSong){ deleteSong(delSong.dataset.delsong); return; }
    var delItem = e.target.closest('[data-delitem]');
    if(delItem){ deleteItem(delItem.dataset.delbase, delItem.dataset.delitem); return; }
    var rev = e.target.closest('[data-reveal]');
    if(rev){ revealPath(rev.dataset.reveal); return; }
    var playSrc = e.target.closest('[data-playsrc]');
    if(playSrc){ playFromPath(playSrc.dataset.playsrc, playSrc); }
  });

  /* 单一播放器互斥 */
  document.addEventListener('play', function(e){
    if(e.target && e.target.tagName === 'AUDIO'){
      Array.prototype.forEach.call(document.querySelectorAll('audio'), function(a){
        if(a !== e.target && !a.paused) a.pause();
      });
    }
  }, true);

  /* 设置/向导是「页面」不是弹窗：Esc 不关它们（焦点模式的 Esc 归 app-score） */

  /* ---- 结果卡：预览翻页 / 放大 / 系统阅读器 / 回到本次结果 ---- */
  $('#donePvPrev').addEventListener('click', function(){ if(resPv.page > 1){ resPv.page--; resPvPaint(); } });
  $('#donePvNext').addEventListener('click', function(){ if(resPv.page < resPv.pages){ resPv.page++; resPvPaint(); } });
  $('#donePvFull').addEventListener('click', function(){ if(window.dqToggleFocus) window.dqToggleFocus(); });
  $('#donePvImg').addEventListener('click', function(){ if(window.dqToggleFocus) window.dqToggleFocus(); });
  $('#donePvSys').addEventListener('click', function(){ if(resPv.base) sysPdf(resPv.base); });
  $('#doneBack').addEventListener('click', function(){ if(window.dqBackToJob) window.dqBackToJob(); });
  window.dqResultHist = function(on, label){
    var b = $('#doneBack');
    if(!b) return;
    b.hidden = !on;
    if(on) b.title = '回到本次扒谱的结果（现在看的是 ' + (label || '历史谱面') + '）';
  };

  function openSettings(){
    var box = $('#setModal');
    if(window.dqOpenSettings){ if(box && box.hidden) window.dqOpenSettings(); }
    else { show(box); loadModels(); }        /* 外壳不可用时才自己加载，避免重复请求 */
  }
  window.dqLoadModels = loadModels;
  window.dqLoadSetup = function(){ loadSetupStatus(); };
  var _sb = $('#settingsBtn'); if(_sb) _sb.addEventListener('click', openSettings);
  var _fs = $('#footSettings'); if(_fs) _fs.addEventListener('click', function(e){ e.preventDefault(); openSettings(); });
  var _fd = $('#footOpenDocs'); if(_fd) _fd.addEventListener('click', function(e){ e.preventDefault(); toast('使用说明：程序目录下的 使用说明.txt / README.md'); });
  $('#saveCfgBtn').addEventListener('click', saveSettings);
  $('#resetCfgBtn').addEventListener('click', function(){
    SET_FIELDS.forEach(function(f){
      var el = document.getElementById('cfg_'+f[0]);
      if(el) el.value = '';
    });
    $('#cfgMsg').textContent = '已清空，点「保存」恢复默认';
  });
  /* 安装向导：预设 / 勾选 / 开始 / 返回 / 完成 */
  $('#setupPresets').addEventListener('click', function(e){
    var b = e.target.closest('.preset'); if(!b) return;
    selectPreset(b.dataset.preset);
  });
  $('#setupBannerBtn').addEventListener('click', function(){ openSetup(false, false); });
  $('#setupStartBtn').addEventListener('click', function(){ startInstall(null, false); });
  $('#setupBackBtn').addEventListener('click', function(){
    /* 返回选择：重装完成后是"补装更多"的入口，所以这里总是回到勾选页 */
    show($('#setupStep1')); hide($('#setupStep2')); hide($('#setupStep3'));
    $('#setupStartBtn').hidden = false; $('#setupStartBtn').disabled = false;
    $('#setupDoneBtn').hidden = true; $('#setupBackBtn').hidden = true;
    loadSetupStatus(function(){ selectPreset(null); renderSetupPaths(); });
  });
  $('#setupDoneBtn').addEventListener('click', function(){
    hide($('#setupModal'));
    loadConfig();
    loadSetupStatus();
  });
  $('#setupClose').addEventListener('click', function(){ hide($('#setupModal')); });

  /* 设置里的组件按钮：单个安装 / 一键补装 / 打开向导 */
  document.addEventListener('click', function(e){
    var ins = e.target.closest('[data-install]');
    if(ins){ installOne(ins.dataset.install); }
  });
  $('#recheckGpuBtn').addEventListener('click', function(){
    gpu = null;
    renderSettingsEngineLine();
    if(typeof renderCapList === 'function') renderCapList();
    pollGpu();
  });
  $('#openWizardBtn').addEventListener('click', function(){ openSetup(true, false); });
  $('#fixAllBtn').addEventListener('click', function(){
    if(!setupInfo){ return; }
    var missing = (setupInfo.components||[]).filter(function(c){ return !c.ok && !c.skip_by_default; })
      .map(function(c){ return c.id; });
    if(!missing.length){ toast('所有组件都已就绪'); return; }
    setupChosen = {};
    missing.forEach(function(id){ setupChosen[id] = true; });
    if(window.dqOpenSetup) window.dqOpenSetup(); else { show($('#setupModal')); hide($('#setModal')); }
    $('#setupStep1').hidden = true; $('#setupStep2').hidden = false; $('#setupStep3').hidden = true;
    startInstall(missing, false);
  });

  /* ================= 启动 ================= */
  showView('pick');
  renderSteps(1, false);
  renderMode();
  loadConfig();
  pollGpu();
  poll();
  loadHistory();
  loadSetupStatus(function(j){
    if(j && j.running) openSetup(false, true);        /* 上次安装还没跑完 -> 直接进进度页 */
    else if(j && j.need_setup) openSetup(false, false); /* 首次使用：自动弹出向导 */
  });
  setInterval(poll, 2000);
  setInterval(tickClock, 1000);
})();
