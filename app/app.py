# -*- coding: utf-8 -*-
"""DrumQuill（扒鼓谱 AI）- local web app backend.
Run:  python app.py   (listens on 127.0.0.1:8756)
"""
import os
import io
import json
import platform
import shutil
import time
import subprocess
import threading
import sys
from flask import Flask, request, jsonify, send_file, render_template

# Portable paths: ROOT = env DRUM_AI_ROOT, else parent of this app/ dir;
# config.json at ROOT may override dataDir (songs/midis/output live under it).
APP_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.environ.get("DRUM_AI_ROOT") or os.path.dirname(APP_DIR)
CONFIG_FILE = os.path.join(ROOT, "config.json")

_CFG = {}


def _load_cfg():
    """Read config.json. utf-8-sig: 记事本/PowerShell 5.1 的 `-Encoding UTF8` 都会写 BOM，
    而带 BOM 的文件用 'utf-8' 解析会直接抛异常 —— 那会让 venvDir/dataDir 被静默丢弃，
    表现为“未找到虚拟环境 / 产物跑到别处”，所以这里必须容忍 BOM。"""
    global _CFG
    try:
        with open(CONFIG_FILE, encoding="utf-8-sig") as f:
            _CFG = json.load(f) or {}
    except Exception:
        _CFG = {}


def reload_cfg():
    _load_cfg()


def save_config(cfg):
    """Merge given dict into config.json on disk, then reload in-memory."""
    merged = dict(_CFG)
    for k, v in cfg.items():
        if v is None or (isinstance(v, str) and not v.strip()):
            merged.pop(k, None)
        else:
            merged[k] = v
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(merged, f, ensure_ascii=False, indent=2)
    _load_cfg()
    return merged


def cfg_get(key, default=None):
    v = _CFG.get(key)
    if v is None or (isinstance(v, str) and not v.strip()):
        return default
    return v


_load_cfg()
DATA_DIR = cfg_get("dataDir") or ROOT


def _resolved_dir(override_key, sub):
    """Directory for songs/midis/output/stems: explicit config wins, else under dataDir."""
    explicit = cfg_get(override_key)
    if explicit:
        return explicit
    return os.path.join(cfg_get("dataDir") or ROOT, sub)


def songs_dir():
    return _resolved_dir("songsDir", "songs")


def midis_dir():
    return _resolved_dir("midisDir", "midis")


def out_dir():
    return _resolved_dir("outputDir", "output")


PS1 = os.path.join(ROOT, "run_pipeline.ps1")
LOG_FILE = os.path.join(APP_DIR, "job.log")
ALLOWED_EXT = {".mp3", ".wav", ".flac", ".ogg", ".m4a", ".aac"}

# ---------------------------------------------------------------------------
# GPU 探测：Demucs 可以用 CUDA 版 torch 跑（实测分离速度提升约 3 倍）。
# torch 的 import 在 Windows 上要 5-8 秒，所以这里异步探测 + 缓存 5 分钟，
# 绝不能让 /api/config 被它拖慢。
# ---------------------------------------------------------------------------
GPU_STATE = {"status": "idle", "available": False, "name": "", "vram_mb": 0,
             "python": "", "reason": "", "at": 0, "torch": "", "cuda": ""}
_GPU_TTL = 300
_gpu_lock = threading.Lock()

# /api/setup/status 的缓存（一次检测要 spawn python，约 4-5 秒）
SETUP_CACHE = {"at": 0, "data": None}
_setup_lock = threading.Lock()


def resolve_gpu_python():
    """config gpuPython -> <ROOT>\\.venv-gpu\\Scripts\\python.exe -> None."""
    explicit = cfg_get("gpuPython")
    if explicit and os.path.exists(explicit):
        return explicit
    cand = os.path.join(ROOT, ".venv-gpu", "Scripts", "python.exe")
    if os.path.exists(cand):
        return cand
    return None


def _gpu_probe_worker(py):
    probe = os.path.join(ROOT, "scripts", "gpu_probe.py")
    info = {"available": False, "name": "", "vram_mb": 0, "reason": "", "torch": "", "cuda": ""}
    try:
        if not os.path.exists(probe):
            info["reason"] = "scripts/gpu_probe.py 缺失"
        else:
            r = subprocess.run([py, probe], capture_output=True, text=True,
                               errors="replace", timeout=90,
                               creationflags=subprocess.CREATE_NO_WINDOW)
            text = (r.stdout or "").strip().splitlines()
            if text:
                info.update(json.loads(text[-1]))
            else:
                info["reason"] = "探测无输出: %s" % (r.stderr or "")[:200]
    except Exception as e:
        info["reason"] = "%s: %s" % (type(e).__name__, str(e)[:160])
    with _gpu_lock:
        GPU_STATE.update(info)
        GPU_STATE["status"] = "ready"
        GPU_STATE["python"] = py
        GPU_STATE["at"] = time.time()


def start_gpu_probe(force=False):
    """Kick off (or reuse) the background CUDA probe. Never blocks."""
    py = resolve_gpu_python()
    with _gpu_lock:
        if not py:
            GPU_STATE.update({"status": "ready", "available": False, "python": "",
                              "reason": "未找到 GPU 运行时（.venv-gpu 或 config.gpuPython）",
                              "at": time.time()})
            return
        fresh = (GPU_STATE["status"] == "ready"
                 and GPU_STATE.get("python") == py
                 and (time.time() - GPU_STATE.get("at", 0)) < _GPU_TTL)
        if fresh and not force:
            return
        if GPU_STATE["status"] == "probing" and not force:
            return
        GPU_STATE["status"] = "probing"
    threading.Thread(target=_gpu_probe_worker, args=(py,), daemon=True).start()


def gpu_snapshot():
    with _gpu_lock:
        return dict(GPU_STATE)


app = Flask(__name__, template_folder=APP_DIR, static_folder=APP_DIR)
# index.html 改动即时生效（开发/维护友好；本地小文件无性能负担）
app.config["TEMPLATES_AUTO_RELOAD"] = True
os.makedirs(APP_DIR, exist_ok=True)
os.makedirs(songs_dir(), exist_ok=True)

STATE = {
    "running": False,
    "song": None,
    "bpm": None,
    "stage": 0,
    "stage_name": "idle",
    "started_at": None,
    "finished": None,
    "error": None,
    "pid": None,
    "cancelled": False,
    "service_pid": None,
    "mode": "full",
}
_lock = threading.Lock()
STATE_FILE = os.path.join(APP_DIR, "task_state.json")
STATE_KEYS = ("running", "pid", "stage", "stage_name", "song", "bpm", "mode",
              "started_at", "finished", "error", "cancelled", "service_pid")


def _save_state():
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump({k: STATE[k] for k in STATE_KEYS}, f, ensure_ascii=False)
    except Exception:
        pass


_STILL_ACTIVE = 259
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_FILETIME_EPOCH_OFFSET = 11644473600  # 1601-01-01 -> 1970-01-01, seconds


def _pid_started_at(pid):
    """Process creation time as epoch seconds, or None if unreadable."""
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = k32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not handle:
            return None
        try:
            created, exited, kern, user = (wintypes.FILETIME() for _ in range(4))
            ok = k32.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited),
                                     ctypes.byref(kern), ctypes.byref(user))
            if not ok:
                return None
            ticks = (created.dwHighDateTime << 32) | created.dwLowDateTime
            return ticks / 10000000.0 - _FILETIME_EPOCH_OFFSET
        finally:
            k32.CloseHandle(handle)
    except Exception:
        return None


def _pid_alive(pid, since=None):
    """True only when `pid` exists, is still running, and started after `since`.

    Never parse tasklist output: its "no matching task" message is localized
    (zh-CN prints 信息: 没有运行的任务匹配指定标准), so an English keyword test
    silently reports every dead PID as alive — which used to leave a crashed job
    looking "running" forever and permanently bricked /api/start.
    `since` (epoch seconds) guards against Windows recycling the PID for an
    unrelated process: a process created before our job started is not ours.
    """
    if not pid:
        return False
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = k32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False  # gone, or access denied (a process we cannot see is not our pipeline)
        try:
            code = wintypes.DWORD()
            if not k32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            if code.value != _STILL_ACTIVE:
                return False  # exited but handle/perf data lingers
        finally:
            k32.CloseHandle(handle)
    except Exception:
        return False
    if since:
        started = _pid_started_at(pid)
        # 5s tolerance: started_at is stamped just before Popen creates the process
        if started is not None and started < float(since) - 5:
            return False
    return True


def _epoch_of(stamp):
    """'2026-10-17 12:31:40' -> epoch seconds (local time); None on failure."""
    try:
        return time.mktime(time.strptime(stamp, "%Y-%m-%d %H:%M:%S"))
    except Exception:
        return None


def _job_pid_alive(state):
    """Liveness of the pipeline process recorded in `state`."""
    return _pid_alive(state.get("pid"), _epoch_of(state.get("started_at")))


def _kill_pid_tree(pid):
    try:
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(int(pid))],
                       capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
        return True
    except Exception:
        return False


def _load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        return
    with _lock:
        for k in STATE_KEYS:
            if k in d:
                STATE[k] = d[k]
        if STATE.get("running"):
            owner = STATE.get("service_pid")
            if owner and int(owner) != os.getpid() and _pid_alive(owner):
                # 另一个仍在运行的服务实例（如 8756 与 8757 并存）拥有这个任务，
                # 不要碰它的状态，也不要杀它的进程树。
                pass
            elif _job_pid_alive(STATE):
                # 上一任服务进程已经没了，但管线（powershell/demucs）还活着：
                # 新服务接不上它的 stdout，既不能推进也不能取消，只会让界面永远
                # 卡在“运行中”并挡住新任务。直接清理遗留进程树并标记中断。
                killed = _kill_pid_tree(STATE.get("pid"))
                STATE["running"] = False
                STATE["pid"] = None
                STATE["finished"] = time.strftime("%Y-%m-%d %H:%M:%S")
                STATE["stage_name"] = "任务中断"
                STATE["error"] = ("服务重启前的任务已中断（遗留进程%s），中间产物已保留，可重新开始。"
                                  % ("已清理" if killed else "未能清理"))
                STATE["cancelled"] = False
                STATE["service_pid"] = os.getpid()
                _save_state()
            else:
                # 进程已经没了：只把状态改成“中断”，不杀任何东西。
                STATE["running"] = False
                STATE["pid"] = None
                STATE["finished"] = time.strftime("%Y-%m-%d %H:%M:%S")
                STATE["stage_name"] = "任务中断"
                STATE["error"] = "任务进程已退出（服务曾重启或进程被终止），中间产物已保留，可重新开始。"
                STATE["cancelled"] = False
                STATE["service_pid"] = os.getpid()
                _save_state()
        STATE["service_pid"] = os.getpid()


def tail_log(n=40):
    try:
        with open(LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        return "".join(lines[-n:])
    except Exception:
        return ""


def write_log(text):
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(text + "\n")


STAGES = {1: "输出歌曲信息 / 检测速度", 2: "分离鼓轨 (Demucs)", 3: "转录 MIDI (ADTOF)", 4: "校正小节线 / 生成乐谱", 5: "渲染 PDF (MuseScore)", 6: "合成鼓声音频"}

# 作业模式 -> 管线开关
MODES = {
    "full":  {"name": "完整扒谱", "flags": [], "desc": "鼓谱 PDF + MusicXML + MIDI + 鼓声音频"},
    "score": {"name": "只要谱子", "flags": ["-SkipAudio"], "desc": "PDF + MusicXML + MIDI，不做音频合成"},
    "audio": {"name": "只要音频", "flags": ["-SkipScore"], "desc": "只出鼓声音频（仍会做分离与转录）"},
    "stems": {"name": "只要鼓声", "flags": ["-OnlyStems"], "desc": "只做鼓轨分离，直接给你纯鼓轨音频"},
}


def run_job(song_path, bpm, mode="full"):
    global STATE
    if mode not in MODES:
        mode = "full"
    with _lock:
        STATE["running"] = True
        STATE["song"] = os.path.basename(song_path)
        STATE["bpm"] = bpm
        STATE["mode"] = mode
        STATE["stage"] = 0
        STATE["stage_name"] = "starting"
        STATE["started_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        STATE["finished"] = None
        STATE["error"] = None
        STATE["pid"] = None  # clear stale pid from previous job; set again after Popen
        STATE["service_pid"] = os.getpid()  # 记录是哪个服务实例在跑这个任务
    STATE["audio_sec"] = _audio_seconds(song_path)
    open(LOG_FILE, "w", encoding="utf-8").close()
    write_log("%s  [0/6] 任务启动: %s（模式：%s）"
              % (time.strftime("%H:%M:%S"), os.path.basename(song_path), MODES[mode]["name"]))
    with _lock:
        STATE["cancelled"] = False
    _save_state()

    cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", PS1,
           "-SongPath", song_path]
    if bpm:
        cmd += ["-BPM", str(bpm)]
    if STATE.get("device_override"):
        cmd += ["-Device", STATE["device_override"]]
    cmd += list(MODES[mode]["flags"])
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"

    def watcher(proc):
        # read stdout line by line, update stage, write log.
        # Wrap in try/finally so a stdout/logging exception never leaves
        # STATE["running"] stuck True forever.
        try:
            for line in proc.stdout:
                line = line.rstrip("\r\n")
                if not line:
                    continue
                # skip noisy tqdm progress lines (they end with "/s]" or contain lots of block chars)
                if ("seconds/s" in line or "%|" in line) and line.count("|") >= 2:
                    continue
                write_log("%s  %s" % (time.strftime("%H:%M:%S"), line))
                for i, name in STAGES.items():
                    if ("[%d/6]" % i) in line:
                        with _lock:
                            STATE["stage"] = i
                            STATE["stage_name"] = name
                        _save_state()
                if "DONE" in line:
                    with _lock:
                        STATE["stage"] = 6
                        STATE["stage_name"] = "完成"
                    _save_state()
        except Exception as e:
            write_log("[x/6] watcher error: %r" % e)
        finally:
            try:
                proc.stdout.close()
            except Exception:
                pass
            rc = proc.wait()
            with _lock:
                cancelled = STATE.get("cancelled")
                STATE["running"] = False
                STATE["finished"] = time.strftime("%Y-%m-%d %H:%M:%S")
                if cancelled:
                    STATE["stage_name"] = "已取消"
                    STATE["error"] = "任务已被取消"
                else:
                    STATE["stage_name"] = "完成" if rc == 0 else ("失败 (exit %d)" % rc)
                    STATE["error"] = None if rc == 0 else "pipeline exit code %d" % rc
            write_log("[x/6] exit code: %d  finished: %s" % (rc, STATE["finished"]))
            _record_run()
            _mark_queue_state(STATE.get("song"), "done" if rc == 0 else "failed")
            _save_state()

    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
            creationflags=subprocess.CREATE_NO_WINDOW, env=env,
        )
        with _lock:
            STATE["pid"] = proc.pid
        _save_state()
        threading.Thread(target=watcher, args=(proc,), daemon=True).start()
    except Exception as e:
        with _lock:
            STATE["running"] = False
            STATE["error"] = str(e)
            STATE["stage_name"] = "启动失败: %s" % e
        write_log("[x/5] 启动失败: %s" % e)


@app.route("/api/cancel", methods=["POST"])
def cancel():
    with _lock:
        pid = STATE.get("pid")
        running = STATE["running"]
        alive = _job_pid_alive(STATE)
        if running and not alive:
            # nothing to kill: the pipeline already died (crash / task manager /
            # reboot). Never taskkill a PID that may now belong to something else.
            STATE["running"] = False
            STATE["pid"] = None
            STATE["finished"] = time.strftime("%Y-%m-%d %H:%M:%S")
            STATE["stage_name"] = "任务中断"
            STATE["error"] = "任务进程已退出，无需取消。"
            _save_state()
    if not running:
        return jsonify({"ok": False, "error": "没有正在运行的任务"}), 400
    if not alive or not pid:
        return jsonify({"ok": False, "error": "任务进程已退出，无需取消"}), 400
    try:
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)],
                       capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    with _lock:
        STATE["cancelled"] = True
    _save_state()
    write_log("[x/5] 任务已被取消 (taskkill pid=%d)" % pid)
    return jsonify({"ok": True})


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/favicon.ico")
def favicon():
    """应用图标（浏览器标签、pywebview 都是这个）。"""
    p = os.path.join(APP_DIR, "drumquill.ico")
    if os.path.exists(p):
        return send_file(p, mimetype="image/x-icon")
    return ("", 404)


@app.route("/icon.png")
def icon_png():
    p = os.path.join(APP_DIR, "drumquill_256.png")
    if os.path.exists(p):
        return send_file(p, mimetype="image/png")
    return ("", 404)


@app.route("/api/upload", methods=["POST"])
def upload():
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify({"ok": False, "error": "no file"}), 400
    ext = os.path.splitext(f.filename)[1].lower()
    if ext not in ALLOWED_EXT:
        return jsonify({"ok": False, "error": "unsupported format: %s" % ext}), 400
    # avoid overwriting a song currently being processed
    name = os.path.basename(f.filename)
    dest = os.path.join(songs_dir(), name)
    if os.path.exists(dest) and STATE["running"] and STATE["song"] == name:
        base, e = os.path.splitext(name)
        dest = os.path.join(songs_dir(), base + "_new" + e)
    f.save(dest)
    return jsonify({"ok": True, "name": os.path.basename(dest), "size": os.path.getsize(dest)})


@app.route("/api/start", methods=["POST"])
def start():
    body = request.get_json(silent=True) or {}
    song = (body.get("song") or "").strip()
    bpm = body.get("bpm") or 0
    mode = (body.get("mode") or "full").strip().lower()
    if mode not in MODES:
        mode = "full"
    with _lock:
        if STATE["running"]:
            return jsonify({"ok": False, "error": "已有任务在运行"}), 409
    if not song:
        return jsonify({"ok": False, "error": "no song"}), 400
    # always resolve against songs_dir(): strip any directory components / traversal
    path = os.path.join(songs_dir(), os.path.basename(song))
    if not os.path.exists(path):
        return jsonify({"ok": False, "error": "file not found: %s" % path}), 404
    try:
        bpm = int(float(bpm)) if bpm else None
    except Exception:
        bpm = None
    # UI limits BPM to 40-300; values outside a sane musical range (e.g. 99999 / -5)
    # would produce invalid MIDI tempo -> fall back to pipeline auto-detect instead.
    if bpm is not None and not (20 <= bpm <= 400):
        bpm = None
    dev = (body.get("device") or "").strip().lower()
    STATE["device_override"] = dev if dev in ("cpu", "cuda", "gpu") else None
    threading.Thread(target=run_job, args=(path, bpm, mode), daemon=True).start()
    return jsonify({"ok": True, "mode": mode, "device": STATE["device_override"]})


@app.route("/api/status")
def status():
    with _lock:
        # zombie guard: running=True but pipeline process already gone ->
        # flip to a clear "interrupted" state so the UI never shows a dead task as "running".
        _grace = 15.0
        try:
            _age = time.time() - time.mktime(time.strptime(STATE.get("started_at") or "", "%Y-%m-%d %H:%M:%S"))
        except Exception:
            _age = 0.0
        if STATE.get("running") and (STATE.get("pid") or _age > _grace) and not _job_pid_alive(STATE):
            STATE["running"] = False
            STATE["pid"] = None
            STATE["finished"] = time.strftime("%Y-%m-%d %H:%M:%S")
            STATE["stage_name"] = "任务中断"
            STATE["error"] = "任务进程已退出（可能内存不足或被系统终止），中间产物已保留，可重新开始。"
            _save_state()
            try:
                _mark_queue_state(STATE.get("song"), "failed")
            except Exception:
                pass
        s = dict(STATE)
    # 队列推进（前端每 2 秒轮询 status，顺手在这里驱动）
    try:
        _queue_advance_if_idle()
    except Exception:
        pass
    s["queue"] = _queue_view()
    s["hints"] = _hints()
    s["eta_sec"] = _eta_seconds(s)
    # results of the most recent finished job: find files matching the song base name
    results = []
    if s.get("song"):
        base = os.path.splitext(s["song"])[0]
        results = _result_files(base)
    s["results"] = results
    s["log_tail"] = tail_log(60)
    # heartbeat: mtime of the job log (seconds, epoch) + whether the pipeline pid is alive
    try:
        s["log_mtime"] = os.path.getmtime(LOG_FILE)
    except Exception:
        s["log_mtime"] = None
    s["alive"] = _job_pid_alive(s)
    s["device"] = _last_device()
    s["detected_bpm"] = _detected_bpm()
    return jsonify(s)


_DEVICE_RE = None
_BPM_RE = None

# 成品文件命名（full/audio 模式是 _鼓谱音频，stems 模式是 _鼓声）
_AUDIO_NAMES = ("_鼓谱音频.mp3", "_鼓谱音频.wav", "_鼓声.mp3", "_鼓声.wav")


def _result_files(base):
    """某个歌曲基名的成品清单（按 PDF / 音频 / XML / MIDI 顺序，只列出真实存在的）。"""
    out = []
    pdf = os.path.join(out_dir(), base + "_鼓谱.pdf")
    if os.path.exists(pdf):
        out.append({"kind": "pdf", "name": base + "_鼓谱.pdf", "size": os.path.getsize(pdf)})
    for suf in _AUDIO_NAMES:
        p = os.path.join(out_dir(), base + suf)
        if os.path.exists(p):
            out.append({"kind": "audio", "name": base + suf, "size": os.path.getsize(p)})
            break
    xml = os.path.join(out_dir(), base + ".musicxml")
    if os.path.exists(xml):
        out.append({"kind": "xml", "name": base + ".musicxml", "size": os.path.getsize(xml)})
    try:
        cands = [fn for fn in os.listdir(midis_dir())
                 if fn.startswith(base + "_") and fn.endswith(".mid")]
        if cands:
            latest = max(cands, key=lambda fn: os.path.getmtime(os.path.join(midis_dir(), fn)))
            out.append({"kind": "mid", "name": latest,
                        "size": os.path.getsize(os.path.join(midis_dir(), latest))})
    except Exception:
        pass
    return out


def _stems_root():
    return cfg_get("stemsDir") or os.path.join(cfg_get("dataDir") or ROOT, "stems")


def _stem_dirs(base):
    """某首歌在各分离模型下的鼓轨目录（stems\<模型>\<歌曲>）。

    切换分离模型后产物会落在不同子目录，产物管理/下载/删除都要跟着模型走，
    不能写死 htdemucs（否则 mdx_extra 作业的中间产物在界面里看不到）。
    """
    root = _stems_root()
    cur = cfg_get("demucsModel") or "htdemucs"
    try:
        names = [d for d in sorted(os.listdir(root)) if os.path.isdir(os.path.join(root, d))]
    except Exception:
        names = []
    out = []
    for n in [cur] + [x for x in names if x != cur] + ["htdemucs"]:
        p = os.path.join(root, n, base)
        if p not in out:
            out.append(p)
    return out


def _song_dirs(base):
    """可能存放某首歌产物的位置。"""
    return {
        "output": out_dir(),
        "midis": midis_dir(),
        "stems": _stem_dirs(base)[0],
        "stemdirs": _stem_dirs(base),
        "songs": songs_dir(),
    }


def _artifact_list(base):
    """中间产物 + 成品，供界面「产物管理」用。"""
    dirs = _song_dirs(base)
    items = []

    def add(kind, path, label):
        try:
            if os.path.isfile(path):
                items.append({"kind": kind, "label": label, "name": os.path.basename(path),
                              "path": path, "size": os.path.getsize(path),
                              "time": time.strftime("%m-%d %H:%M",
                                                    time.localtime(os.path.getmtime(path)))})
        except Exception:
            pass

    # 成品（output）
    for suf, kind, label in (("_鼓谱.pdf", "pdf", "鼓谱 PDF"),
                             (".musicxml", "xml", "MusicXML 乐谱"),
                             ("_鼓谱音频.mp3", "audio", "鼓声音频 MP3"),
                             ("_鼓谱音频.wav", "audio", "鼓声音频 WAV"),
                             ("_鼓声.mp3", "audio", "分离鼓声 MP3"),
                             ("_鼓声.wav", "audio", "分离鼓声 WAV")):
        add(kind, os.path.join(dirs["output"], base + suf), label)
    # 中间：分离轨道（可能存在于多个模型目录下）
    for stem_dir in dirs["stemdirs"]:
        if not os.path.isdir(stem_dir):
            continue
        try:
            tag = os.path.basename(os.path.dirname(stem_dir))
            for fn in sorted(os.listdir(stem_dir)):
                add("stem", os.path.join(stem_dir, fn), "分离轨道 " + fn + "（" + tag + "）")
        except Exception:
            pass
    # 中间：MIDI
    try:
        for fn in sorted(os.listdir(dirs["midis"])):
            if fn.startswith(base) and fn.endswith(".mid"):
                add("midi", os.path.join(dirs["midis"], fn), "MIDI " + fn)
    except Exception:
        pass
    # 源文件
    try:
        for ext in ALLOWED_EXT:
            p = os.path.join(dirs["songs"], base + ext)
            if os.path.isfile(p):
                add("source", p, "源音频 " + base + ext)
    except Exception:
        pass
    return items


def _last_device():
    """本次/上次任务实际用的分离设备，从 job.log 里读（管线会打 '[2/6] 分离鼓轨（Demucs · GPU ...'）。"""
    global _DEVICE_RE
    try:
        import re
        if _DEVICE_RE is None:
            _DEVICE_RE = re.compile(r"\[2/6\][^\n]*?(GPU|CPU)")
        with open(LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
        m = _DEVICE_RE.search(text)
        if m:
            return m.group(1)
    except Exception:
        pass
    return None


def _detected_bpm():
    """界面显示的 BPM = 管线里**最终生效**的那个。

    日志里会出现多次：
        [*] BPM = 148              ← librosa 原始检测
        改用 74 BPM                 ← 八度校正
        校正为 155.98 BPM / 中位速度 73.2 BPM  ← 起音精调 / 拍栅格
    以前取第一条，速度被校正后界面仍显示旧值。改为取出现位置最靠后的一条。
    """
    global _BPM_RE
    try:
        import re
        if _BPM_RE is None:
            _BPM_RE = [re.compile(p) for p in (
                r"\[\*\] BPM = ([\d.]+)",
                r"改用 ([\d.]+) BPM",
                r"校正为 ([\d.]+) BPM",
                r"中位速度 ([\d.]+) BPM",
            )]
        with open(LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
        best = None
        for rx in _BPM_RE:
            for m in rx.finditer(text):
                if best is None or m.start() > best[0]:
                    best = (m.start(), float(m.group(1)))
        if best:
            val = best[1]
            return int(val) if abs(val - int(val)) < 0.01 else round(val, 1)
    except Exception:
        pass
    return None


@app.route("/api/gpu")
def gpu():
    """GPU 探测状态（首次访问会自动起后台探测，不阻塞）。"""
    start_gpu_probe()
    snap = gpu_snapshot()
    snap["deviceSetting"] = cfg_get("demucsDevice", "auto")
    return jsonify(snap)


# ---------------- 模型管理（分离模型可下载 / 可切换） ----------------
@app.route("/api/models")
def models_list():
    import models_registry
    return jsonify(models_registry.list_models(ROOT, _CFG))


@app.route("/api/models/select", methods=["POST"])
def models_select():
    import models_registry
    body = request.get_json(silent=True) or {}
    name = (body.get("model") or "").strip()
    info = models_registry.list_models(ROOT, _CFG)
    hit = [m for m in info["models"] if m["id"] == name and m.get("installed")]
    if not hit:
        return jsonify({"ok": False, "error": "该模型还没下载完整"}), 400
    save_config({"demucsModel": name})
    return jsonify({"ok": True, "current": name})


@app.route("/api/models/download", methods=["POST"])
def models_download():
    import installer, models_registry
    body = request.get_json(silent=True) or {}
    name = (body.get("model") or "").strip()
    if not name:
        return jsonify({"ok": False, "error": "no model"}), 400
    if installer.snapshot().get("running"):
        return jsonify({"ok": False, "error": "已有安装/下载任务在跑，等它结束"}), 409
    if name not in models_registry.BUILTIN:
        return jsonify({"ok": False, "error": "未知模型：%s" % name}), 400
    installer.reset()
    installer.run_model_download(ROOT, _CFG, name)
    return jsonify({"ok": True, "model": name})


@app.route("/api/models/delete", methods=["POST"])
def models_delete():
    import installer, models_registry
    body = request.get_json(silent=True) or {}
    name = (body.get("model") or "").strip()
    msgs = []
    ok = models_registry.delete_model(ROOT, _CFG, name, log=lambda s: msgs.append(s))
    if ok and (_CFG.get("demucsModel") or "") == name:
        save_config({"demucsModel": "htdemucs"})
    return jsonify({"ok": ok, "log": msgs})


@app.route("/api/songs")
def songs():
    items = []
    for fn in sorted(os.listdir(songs_dir()), key=lambda x: os.path.getmtime(os.path.join(songs_dir(), x)), reverse=True):
        p = os.path.join(songs_dir(), fn)
        if os.path.isfile(p):
            items.append({"name": fn, "size": os.path.getsize(p)})
    return jsonify(items)


@app.route("/api/probe")
def probe():
    """歌曲时长（秒），用于界面给出可信的"预计用时"。ffprobe 缺失时返回 null。"""
    name = os.path.basename(request.args.get("song", "") or "")
    if not name:
        return jsonify({"ok": False, "error": "no song"}), 400
    path = os.path.join(songs_dir(), name)
    if not os.path.exists(path):
        return jsonify({"ok": False, "error": "file not found"}), 404
    dur = None
    try:
        from shutil import which
        exe = which("ffprobe")
        if exe:
            r = subprocess.run([exe, "-v", "error", "-show_entries", "format=duration",
                                "-of", "default=nw=1:nk=1", path],
                               capture_output=True, text=True, timeout=20,
                               creationflags=subprocess.CREATE_NO_WINDOW)
            first = (r.stdout or "").strip().splitlines()
            if first:
                dur = float(first[0])
    except Exception:
        dur = None
    return jsonify({"ok": True, "song": name, "duration_s": round(dur, 1) if dur else None,
                    "size": os.path.getsize(path)})


def _detected_tools():
    """自动探测 MuseScore / ffmpeg（供设置面板显示"实际生效"的路径）。"""
    found = {"museScorePath": "", "ffmpegPath": ""}
    for cand in (r"C:\Program Files\MuseScore 4\bin\MuseScore4.exe",
                 r"C:\Program Files (x86)\MuseScore 4\bin\MuseScore4.exe",
                 r"C:\Program Files\MuseScore 3\bin\MuseScore3.exe"):
        if os.path.exists(cand):
            found["museScorePath"] = cand
            break
    try:
        from shutil import which
        found["ffmpegPath"] = which("ffmpeg") or ""
    except Exception:
        pass
    return found


@app.route("/api/config", methods=["GET"])
def config_get():
    """Return current config + resolved effective paths for the UI settings panel."""
    root_dir = cfg_get("dataDir") or ROOT
    effective = {
        "dataDir": root_dir,
        "songsDir": songs_dir(),
        "midisDir": midis_dir(),
        "outputDir": out_dir(),
        "stemsDir": cfg_get("stemsDir") or os.path.join(root_dir, "stems"),
        "venvDir": cfg_get("venvDir") or os.path.join(ROOT, ".venv"),
        "museScorePath": cfg_get("museScorePath", ""),
        "ffmpegPath": cfg_get("ffmpegPath", ""),
        "adtofDir": cfg_get("adtofDir", ""),
        "soundDir": cfg_get("soundDir") or os.path.join(ROOT, "sound"),
        "modelsDir": cfg_get("modelsDir") or os.path.join(ROOT, "models"),
    }
    # short aliases kept for the header/footer hints in index.html
    resolved = dict(effective)
    detected = _detected_tools()
    if not effective.get("museScorePath"):
        resolved["museScorePath"] = detected["museScorePath"]
    if not effective.get("ffmpegPath"):
        resolved["ffmpegPath"] = detected["ffmpegPath"]
    resolved["gpuPython"] = resolve_gpu_python() or ""
    resolved.update({
        "songs": effective["songsDir"],
        "midis": effective["midisDir"],
        "output": effective["outputDir"],
        "stems": effective["stemsDir"],
        "venv": effective["venvDir"],
        "root": ROOT,
    })
    return jsonify({
        "dataDir": cfg_get("dataDir") or ROOT,
        "songsDir": cfg_get("songsDir", ""),
        "midisDir": cfg_get("midisDir", ""),
        "outputDir": cfg_get("outputDir", ""),
        "stemsDir": cfg_get("stemsDir", ""),
        "venvDir": cfg_get("venvDir", ""),
        "museScorePath": cfg_get("museScorePath", ""),
        "ffmpegPath": cfg_get("ffmpegPath", ""),
        "adtofDir": cfg_get("adtofDir", ""),
        "soundDir": cfg_get("soundDir", ""),
        "modelsDir": cfg_get("modelsDir", ""),
        "bpmFallback": cfg_get("bpmFallback", 120),
        "modes": [{"id": k, "name": v["name"], "desc": v["desc"]} for k, v in MODES.items()],
        "mode": cfg_get("mode", "full"),
        # 未设置时返回空串，界面才会显示"(默认)"而不是误导性的 0
        "demucsJobs": cfg_get("demucsJobs", ""),
        "demucsDevice": cfg_get("demucsDevice", "auto"),
        "gpuPython": cfg_get("gpuPython", ""),
        "adtofThresholds": cfg_get("adtofThresholds", ""),
        "gpu": gpu_snapshot(),
        "resolved": resolved,
    })


@app.route("/api/config", methods=["POST"])
def config_post():
    """Merge settings into config.json (hot reload, no restart needed for dirs)."""
    if STATE.get("running"):
        return jsonify({"ok": False, "error": "任务运行中，请结束后再修改配置"}), 409
    body = request.get_json(silent=True) or {}
    merged = save_config(body)
    os.makedirs(songs_dir(), exist_ok=True)
    os.makedirs(midis_dir(), exist_ok=True)
    os.makedirs(out_dir(), exist_ok=True)
    # 换 GPU 运行时 / 改设备偏好后重新探测（异步，不阻塞保存动作）
    if any(k in body for k in ("gpuPython", "demucsDevice")):
        start_gpu_probe(force=True)
    return jsonify({"ok": True, "config": merged})


@app.route("/api/setup/status")
def setup_status():
    """组件清单 + 能力矩阵：界面据此决定"能开始扒谱吗""哪些功能不可用"。

    检测本身要 spawn 一次 python（import torch/librosa，约 4-5 秒），所以这里做 60 秒缓存；
    装完组件后前端会带 ?refresh=1 强制重查。
    """
    import installer
    force = request.args.get("refresh") == "1"
    with _setup_lock:
        fresh = (SETUP_CACHE["data"] is not None and (time.time() - SETUP_CACHE["at"]) < 60)
        if fresh and not force:
            return jsonify(SETUP_CACHE["data"])
    start_gpu_probe()   # 顺带触发一次（异步），让 GPU 组件状态尽快确定
    st = installer.check_status(ROOT, _CFG, gpu=gpu_snapshot())
    comps = []
    for meta in installer.component_meta():
        s = st.get(meta["id"], {"ok": False, "note": ""})
        item = dict(meta)
        item["ok"] = bool(s.get("ok"))
        item["note"] = s.get("note", "")
        comps.append(item)
    caps = installer.capabilities(st)
    snap = installer.snapshot()
    data = {
        "components": comps,
        "capabilities": caps,
        "need_setup": installer.need_setup(st),
        "can_transcribe": installer.can_transcribe(st),
        "ok_count": sum(1 for c in comps if c["ok"]),
        "total_count": len(comps),
        "missing_required": [c["id"] for c in comps if c["required"] and not c["ok"]],
        "missing_optional": [c["id"] for c in comps if (not c["required"]) and (not c["ok"]) and not c["skip_by_default"]],
        "running": snap.get("running", False),
        "presets": {"recommended": installer.RECOMMENDED_IDS, "minimal": installer.MINIMAL_IDS},
    }
    with _setup_lock:
        SETUP_CACHE["data"] = data
        SETUP_CACHE["at"] = time.time()
    return jsonify(data)


@app.route("/api/setup/start", methods=["POST"])
def setup_start():
    import installer
    body = request.get_json(silent=True) or {}
    if installer.snapshot().get("running"):
        return jsonify({"ok": False, "error": "安装已在进行中"}), 409
    comps = [c for c in body.get("components", []) if c in installer.COMPONENT_IDS]
    dry = bool(body.get("dry_run"))
    paths = {k: v for k, v in (body.get("paths") or {}).items() if isinstance(v, str) and v.strip()}
    if paths:
        save_config(paths)
    if not comps:
        return jsonify({"ok": False, "error": "未选择任何组件"}), 400
    ordered = [c for c in installer.COMPONENT_IDS if c in comps]
    installer.reset()
    installer.run_install(ROOT, _CFG, ordered, dry_run=dry)
    with _setup_lock:
        SETUP_CACHE["data"] = None   # 装完要让 /api/setup/status 重新检测
        SETUP_CACHE["at"] = 0
    return jsonify({"ok": True, "components": ordered, "dry_run": dry})


@app.route("/api/setup/progress")
def setup_progress():
    import installer
    try:
        seq = int(request.args.get("last_seq", 0) or 0)
    except Exception:
        seq = 0
    snap = installer.snapshot()
    snap["log"] = installer.get_log_after(seq)
    return jsonify(snap)


@app.route("/api/download")
def download():
    kind = request.args.get("kind", "pdf")
    song = request.args.get("song", "")
    base = os.path.splitext(os.path.basename(song))[0]
    # name= 允许直接按真实文件名下载（产物管理里可能下载中间产物）
    want = os.path.basename(request.args.get("name", "") or "")
    if want:
        for d in [out_dir(), midis_dir()] + _stem_dirs(base):
            cand = os.path.join(d, want)
            if os.path.isfile(cand):
                return send_file(cand, as_attachment=True, download_name=want)
        return jsonify({"ok": False, "error": "file not found"}), 404
    if kind == "pdf":
        p = os.path.join(out_dir(), base + "_鼓谱.pdf")
        dl = base + "_鼓谱.pdf"
    elif kind == "xml":
        p = os.path.join(out_dir(), base + ".musicxml")
        dl = base + ".musicxml"
    elif kind == "mid":
        # most recent _<bpm>.mid
        cands = [os.path.join(midis_dir(), fn) for fn in os.listdir(midis_dir())
                 if fn.startswith(base + "_") and fn.endswith(".mid")]
        if not cands:
            return jsonify({"ok": False, "error": "mid not found"}), 404
        p = max(cands, key=os.path.getmtime)
        dl = os.path.basename(p)
    elif kind == "audio":
        p = None
        dl = None
        for suf in _AUDIO_NAMES:
            cand = os.path.join(out_dir(), base + suf)
            if os.path.exists(cand):
                p, dl = cand, base + suf
                break
        if p is None:
            return jsonify({"ok": False, "error": "audio not found"}), 404
    else:
        return jsonify({"ok": False, "error": "bad kind"}), 400
    if not os.path.exists(p):
        return jsonify({"ok": False, "error": "file not found"}), 404
    preview = request.args.get("preview") == "1"
    if preview:
        if kind == "pdf":
            return send_file(p, mimetype="application/pdf")
        if kind == "audio":
            # correct mime by actual extension (mp3 vs wav fallback) — a wrong
            # mime like audio/mpeg for a wav breaks seeking / progress bar
            ext = os.path.splitext(p)[1].lower()
            mime = {"mp3": "audio/mpeg", "wav": "audio/wav",
                    "flac": "audio/flac", "ogg": "audio/ogg"}.get(ext, "audio/mpeg")
            return send_file(p, mimetype=mime, conditional=True)
    return send_file(p, as_attachment=True, download_name=dl)


@app.route("/api/history")
def history():
    """Recently produced drum sheets in output dir (任何模式的成品都会列出来)."""
    items = []
    for fn in sorted(os.listdir(out_dir()), key=lambda x: os.path.getmtime(os.path.join(out_dir(), x)), reverse=True):
        p = os.path.join(out_dir(), fn)
        if not os.path.isfile(p):
            continue
        if fn.endswith("_鼓谱.pdf"):
            base = fn[:-len("_鼓谱.pdf")]
        elif fn.endswith("_鼓声.mp3") or fn.endswith("_鼓声.wav"):
            base = fn[:-len("_鼓声.mp3")] if fn.endswith(".mp3") else fn[:-len("_鼓声.wav")]
        elif fn.endswith("_鼓谱音频.mp3") or fn.endswith("_鼓谱音频.wav"):
            continue          # 音频只是成品的附属，让 PDF/鼓声 行代表它
        else:
            continue
        items.append({"name": fn, "base": base, "size": os.path.getsize(p),
                      "time": time.strftime("%m-%d %H:%M", time.localtime(os.path.getmtime(p)))})
    # 同一个 base 只保留一条（PDF 优先）
    seen = {}
    for it in items:
        seen.setdefault(it["base"], it)
    return jsonify(list(seen.values()))


@app.route("/api/artifacts")
def artifacts():
    """某首歌的全部产物（含中间产物），供「产物管理」使用。"""
    base = os.path.splitext(os.path.basename(request.args.get("song", "") or ""))[0]
    if not base:
        return jsonify({"ok": False, "error": "no song"}), 400
    return jsonify({"ok": True, "base": base, "items": _artifact_list(base)})


@app.route("/api/delete", methods=["POST"])
def delete_artifacts():
    """删除某首歌的产物 / 中间产物。keep_source 默认 true（不删歌曲文件本身）。"""
    body = request.get_json(silent=True) or {}
    base = os.path.splitext(os.path.basename((body.get("song") or "").strip()))[0]
    if not base:
        return jsonify({"ok": False, "error": "no song"}), 400
    if STATE.get("running") and os.path.splitext(os.path.basename(STATE.get("song") or ""))[0] == base:
        return jsonify({"ok": False, "error": "这首歌正在处理中，结束或取消后再删"}), 409
    keep_source = body.get("keep_source", True)
    one = os.path.basename((body.get("name") or "").strip())
    if one:
        # 删除单个产物（产物管理里每行一个删除按钮）
        for d in [out_dir(), midis_dir()] + _stem_dirs(base) + [songs_dir()]:
            cand = os.path.join(d, one)
            if not os.path.isfile(cand):
                continue
            if d == songs_dir() and keep_source:
                return jsonify({"ok": False, "error": "源音频默认保留；要删请显式指定 keep_source=false"}), 400
            try:
                os.remove(cand)
            except Exception as e:
                return jsonify({"ok": False, "error": str(e)}), 500
            return jsonify({"ok": True, "removed": [one], "failed": []})
        return jsonify({"ok": False, "error": "文件不存在"}), 404
    removed, failed = [], []
    for it in _artifact_list(base):
        if it["kind"] == "source" and keep_source:
            continue
        try:
            os.remove(it["path"])
            removed.append(it["name"])
        except Exception as e:
            failed.append("%s: %s" % (it["name"], e))
    # 清掉空的分离目录（含空出来的模型目录）
    for stem_dir in _stem_dirs(base):
        try:
            if os.path.isdir(stem_dir) and not os.listdir(stem_dir):
                os.rmdir(stem_dir)
                parent = os.path.dirname(stem_dir)
                if os.path.isdir(parent) and not os.listdir(parent):
                    os.rmdir(parent)
        except Exception:
            pass
    return jsonify({"ok": True, "removed": removed, "failed": failed})


@app.route("/api/reveal", methods=["POST"])
def reveal():
    """在资源管理器里定位文件（或打开目录）。只允许数据目录内的路径。"""
    body = request.get_json(silent=True) or {}
    raw = (body.get("path") or "").strip()
    if not raw:
        return jsonify({"ok": False, "error": "no path"}), 400
    ap = os.path.abspath(raw)
    if not os.path.exists(ap):
        return jsonify({"ok": False, "error": "文件不存在（可能已被删除）"}), 404
    roots = [os.path.abspath(d) for d in (out_dir(), midis_dir(), songs_dir(),
             cfg_get("stemsDir") or os.path.join(cfg_get("dataDir") or ROOT, "stems"))]
    if not any(ap.lower().startswith(r.lower()) for r in roots):
        return jsonify({"ok": False, "error": "只能打开数据目录内的文件"}), 403
    try:
        if os.path.isdir(ap):
            subprocess.Popen(["explorer", ap])
        else:
            subprocess.Popen(["explorer", "/select,", ap])
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# 鼓谱 PDF 预览：用 PyMuPDF 在服务端把页面渲染成 PNG。
# 不依赖浏览器内置 PDF 插件（pywebview/WebView2 里 iframe 嵌 PDF 往往空白）。
# ---------------------------------------------------------------------------
PDF_CACHE_DIR = os.path.join(ROOT, ".cache", "pdf")
_pdf_lock = threading.Lock()


def _pdf_file(base):
    return os.path.join(out_dir(), base + "_鼓谱.pdf")


def _in_data_dirs(path):
    roots = [os.path.abspath(d) for d in (out_dir(), midis_dir(), songs_dir(),
             cfg_get("stemsDir") or os.path.join(cfg_get("dataDir") or ROOT, "stems"))]
    ap = os.path.abspath(path)
    return any(ap.lower().startswith(r.lower()) for r in roots)


@app.route("/api/pdf/info")
def pdf_info():
    """鼓谱 PDF 的页数等信息，前端据此做翻页器。"""
    base = os.path.splitext(os.path.basename(request.args.get("song", "") or ""))[0]
    p = _pdf_file(base)
    if not base or not os.path.isfile(p):
        return jsonify({"ok": False, "error": "没有这份鼓谱 PDF"}), 404
    try:
        import pymupdf
        with pymupdf.open(p) as doc:
            pages = doc.page_count
            r = doc[0].rect
        return jsonify({"ok": True, "pages": pages, "width": round(r.width), "height": round(r.height),
                        "size": os.path.getsize(p), "mtime": os.path.getmtime(p)})
    except Exception as e:
        return jsonify({"ok": False, "error": "%s: %s" % (type(e).__name__, str(e)[:160])}), 500


@app.route("/api/pdf/page")
def pdf_page():
    """渲染某一页为 PNG（带磁盘缓存；缩略图用 dpi=40）。"""
    base = os.path.splitext(os.path.basename(request.args.get("song", "") or ""))[0]
    p = _pdf_file(base)
    if not base or not os.path.isfile(p):
        return jsonify({"ok": False, "error": "没有这份鼓谱 PDF"}), 404
    try:
        page = max(1, int(request.args.get("page", 1)))
        dpi = int(request.args.get("dpi", 120))
        dpi = max(24, min(220, dpi))
    except Exception:
        page, dpi = 1, 120
    try:
        mtime = int(os.path.getmtime(p))
        safe = "".join(c if (c.isalnum() or c in "-_") else "_" for c in base)[:80]
        out = os.path.join(PDF_CACHE_DIR, "%s_%d_p%d_d%d.png" % (safe, mtime, page, dpi))
        if not os.path.exists(out):
            os.makedirs(PDF_CACHE_DIR, exist_ok=True)
            import pymupdf
            with _pdf_lock:
                if not os.path.exists(out):
                    with pymupdf.open(p) as doc:
                        if page > doc.page_count:
                            return jsonify({"ok": False, "error": "页码超出范围"}), 404
                        pix = doc[page - 1].get_pixmap(dpi=dpi)
                        pix.save(out)
        resp = send_file(out, mimetype="image/png", conditional=True)
        resp.headers["Cache-Control"] = "public, max-age=86400"
        return resp
    except Exception as e:
        return jsonify({"ok": False, "error": "%s: %s" % (type(e).__name__, str(e)[:160])}), 500


@app.route("/api/open", methods=["POST"])
def open_with_system():
    """用系统默认程序打开文件（PDF 会用 Edge/Adobe 等阅读器）。"""
    body = request.get_json(silent=True) or {}
    raw = (body.get("path") or "").strip()
    if not raw:
        return jsonify({"ok": False, "error": "no path"}), 400
    ap = os.path.abspath(raw)
    if not os.path.isfile(ap):
        return jsonify({"ok": False, "error": "文件不存在"}), 404
    if not _in_data_dirs(ap):
        return jsonify({"ok": False, "error": "只能打开数据目录内的文件"}), 403
    try:
        os.startfile(ap)   # Windows: 用默认关联程序打开
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify({"ok": True})


def _install_crash_hooks():
    """把「服务进程意外消失」变成可诊断的证据。

    pythonw 没有控制台；过去服务一旦崩掉就什么线索都没有（job.log 只到分离阶段，
    launcher.log 只说 status 不通）。这里做两件事：
      1. faulthandler 抓原生崩溃 / 段错误（torch、WebView 相关 DLL 都会）。
      2. excepthook + atexit 记录未捕获异常与正常/异常退出。

    注意：写 service_crash.log，而不是 service.log。service.log 由 launcher 用作
    子进程的 stdout/stderr 重定向目标；两个句柄写同一个文件会互相覆盖（实测
    启动标记会被后续 stdout 输出冲掉）。
    """
    log_path = os.path.join(APP_DIR, "service_crash.log")
    try:
        import faulthandler
        fh = open(log_path, "a", encoding="utf-8", errors="replace")
        faulthandler.enable(file=fh, all_threads=True)
        app.extensions["_crash_fh"] = fh  # keep a reference so it is never GC'd
    except Exception:
        pass
    try:
        import atexit

        def _excepthook(exc_type, exc, tb):
            import traceback
            try:
                with open(log_path, "a", encoding="utf-8", errors="replace") as f:
                    f.write("\n[%s] UNCAUGHT pid=%d\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), os.getpid()))
                    traceback.print_exception(exc_type, exc, tb, file=f)
                    f.flush()
            except Exception:
                pass
            sys.__excepthook__(exc_type, exc, tb)

        sys.excepthook = _excepthook

        def _on_exit():
            try:
                with open(log_path, "a", encoding="utf-8", errors="replace") as f:
                    f.write("[%s] service exiting pid=%d\n"
                            % (time.strftime("%Y-%m-%d %H:%M:%S"), os.getpid()))
            except Exception:
                pass

        atexit.register(_on_exit)
    except Exception:
        pass


def _write_service_stamp():
    """把 app/*.py + *.html 的指纹落盘，启动器据此判断“复用的是不是旧代码”。

    算法必须与 launcher.py 的 app_fingerprint() 完全一致。
    """
    try:
        names = sorted(f for f in os.listdir(APP_DIR) if f.endswith((".py", ".html")))
        h = 0
        for fn in names:
            try:
                st = os.stat(os.path.join(APP_DIR, fn))
            except Exception:
                continue
            h = (h * 131 + int(st.st_mtime) + st.st_size) & 0xFFFFFFFF
        with open(os.path.join(APP_DIR, "service_stamp.json"), "w", encoding="utf-8") as f:
            json.dump({"stamp": h, "pid": os.getpid(), "started": time.time()}, f)
    except Exception:
        pass



# ===========================================================================
# 批量队列 / ETA / 失败提示 / 诊断包
# ===========================================================================
QUEUE_FILE = os.path.join(APP_DIR, "queue.json")
_ADVANCING = False
STATS_FILE = os.path.join(APP_DIR, "job_stats.json")
STAGES_N = 6


def _read_json(path, default):
    try:
        with open(path, encoding="utf-8-sig") as f:   # 容忍 BOM
            return json.load(f)
    except Exception:
        return default


def _write_json(path, data):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


def _queue_list():
    global _ADVANCING
    q = _read_json(QUEUE_FILE, {"items": [], "paused": False})
    if not isinstance(q, dict):
        q = {"items": [], "paused": False}
    q.setdefault("items", [])
    q.setdefault("paused", False)
    # 服务重启后，上次中断的 running 项退回 pending，避免永久卡住
    changed = False
    for it in q["items"]:
        if it.get("state") == "running" and not STATE.get("running"):
            it["state"] = "pending"
            changed = True
    if changed:
        _write_json(QUEUE_FILE, q)
    return q


def _queue_save(q):
    _write_json(QUEUE_FILE, q)


def _queue_view():
    q = _queue_list()
    done = sum(1 for it in q["items"] if it.get("state") == "done")
    return {"paused": q["paused"], "total": len(q["items"]), "done": done,
            "items": q["items"][-30:]}


def _queue_advance_if_idle():
    """任务空闲时，取下一个待处理项开跑。"""
    global _ADVANCING
    with _lock:
        if STATE.get("running") or _ADVANCING:
            return
        _ADVANCING = True
    try:
        _advance_body()
    finally:
        with _lock:
            _ADVANCING = False


def _advance_body():
    q = _queue_list()
    if q["paused"]:
        return
    nxt = None
    for it in q["items"]:
        if it.get("state") == "pending":
            nxt = it
            break
    if not nxt:
        return
    path = os.path.join(songs_dir(), nxt["song"])
    if not os.path.isfile(path):
        nxt["state"] = "missing"
        _queue_save(q)
        return
    nxt["state"] = "running"
    _queue_save(q)
    with _lock:
        STATE["device_override"] = nxt.get("device") or None
    try:
        run_job(path, nxt.get("bpm") or None, nxt.get("mode") or "full")
    except Exception:
        nxt["state"] = "pending"


def _mark_queue_state(song, state):
    q = _queue_list()
    changed = False
    for it in q["items"]:
        if it.get("song") == song and it.get("state") in ("running", "pending"):
            it["state"] = state
            changed = True
            break
    if changed:
        _queue_save(q)


@app.route("/api/queue", methods=["GET", "POST"])
def queue_api():
    if request.method == "GET":
        return jsonify(_queue_list())
    body = request.get_json(silent=True) or {}
    action = (body.get("action") or "").lower()
    q = _queue_list()
    if action == "add":
        names = body.get("songs") or ([body["song"]] if body.get("song") else [])
        added = 0
        for n in names:
            n = os.path.basename((n or "").strip())
            if not n or not os.path.isfile(os.path.join(songs_dir(), n)):
                continue
            if any(it.get("song") == n and it.get("state") in ("pending", "running") for it in q["items"]):
                continue
            q["items"].append({"song": n, "mode": body.get("mode") or "full",
                               "bpm": body.get("bpm") or None, "state": "pending"})
            added += 1
        _queue_save(q)
        return jsonify({"ok": True, "added": added, "queue": _queue_view()})
    if action == "clear":
        cur = STATE.get("song") if STATE.get("running") else None
        q["items"] = [it for it in q["items"] if it.get("state") == "running" and it.get("song") == cur]
        _queue_save(q)
        return jsonify({"ok": True, "queue": _queue_view()})
    if action == "remove":
        n = os.path.basename((body.get("song") or "").strip())
        q["items"] = [it for it in q["items"] if not (it.get("song") == n and it.get("state") != "running")]
        _queue_save(q)
        return jsonify({"ok": True, "queue": _queue_view()})
    if action in ("pause", "resume"):
        q["paused"] = (action == "pause")
        _queue_save(q)
        return jsonify({"ok": True, "queue": _queue_view()})
    return jsonify({"ok": False, "error": "unknown action"}), 400


# ---------------- ETA ----------------
def _audio_seconds(path):
    try:
        ff = cfg_get("ffmpegPath") or "ffmpeg"
        probe = shutil.which("ffprobe") or "ffprobe"
        r = subprocess.run([probe, "-v", "error", "-show_entries", "format=duration",
                            "-of", "default=nw=1:nk=1", path],
                           capture_output=True, text=True, timeout=30,
                           creationflags=subprocess.CREATE_NO_WINDOW)
        return float((r.stdout or "0").strip() or 0)
    except Exception:
        return 0.0


def _stats():
    d = _read_json(STATS_FILE, {"runs": []})
    if not isinstance(d, dict):
        d = {"runs": []}
    d.setdefault("runs", [])
    return d


def _record_run():
    """任务结束时记录一次耗时，供 ETA 估算。"""
    try:
        st = STATE.get("started_at")
        if not st:
            return
        t0 = time.mktime(time.strptime(st, "%Y-%m-%d %H:%M:%S"))
        sec = max(1.0, time.time() - t0)
        audio = STATE.get("audio_sec") or 0
        d = _stats()
        d["runs"].append({"device": _last_device() or "CPU", "mode": STATE.get("mode") or "full",
                          "seconds": round(sec, 1), "audio_sec": round(audio, 1),
                          "when": time.strftime("%Y-%m-%d %H:%M")})
        d["runs"] = d["runs"][-50:]
        _write_json(STATS_FILE, d)
    except Exception:
        pass


def _eta_seconds(s):
    if not s.get("running") or not s.get("started_at"):
        return None
    try:
        dev = s.get("device") or "CPU"
        mode = s.get("mode") or "full"
        runs = [r for r in _stats()["runs"]
                if r.get("device") == dev and r.get("mode") == mode and r.get("seconds", 0) > 5]
        if not runs:
            return None
        rate = sorted(r["seconds"] / (r["audio_sec"] or 1) for r in runs if r.get("audio_sec"))
        if not rate:
            return None
        audio = s.get("audio_sec") or 0
        if audio <= 0:
            return None
        total = rate[len(rate) // 2] * audio
        t0 = time.mktime(time.strptime(s["started_at"], "%Y-%m-%d %H:%M:%S"))
        left = total - (time.time() - t0)
        return int(max(0, min(left, 3600 * 2)))
    except Exception:
        return None


# ---------------- 失败提示 ----------------
def _hints():
    """从日志尾部识别常见失败，给出可执行的下一步。"""
    if STATE.get("running") or not STATE.get("error"):
        return []
    try:
        with open(LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
            tail = f.read()[-8000:].lower()
    except Exception:
        tail = ""
    mode = STATE.get("mode") or "full"
    out = []
    if "未找到分离模型" in tail or "分离鼓轨失败" in tail or "模型缺失" in tail:
        out.append({"text": "分离模型缺失或不可用", "actions": [
            {"label": "去设置下载/切换模型", "kind": "settings"},
            {"label": "只出鼓声重试", "kind": "rerun", "body": {"mode": "stems"}}]})
    if "musescore" in tail and ("失败" in tail or "未找到" in tail or "未产出" in tail):
        out.append({"text": "MuseScore 不可用，PDF 无法渲染", "actions": [
            {"label": "用内置渲染重试", "kind": "rerun", "body": {"mode": mode}}]})
    if "out of memory" in tail or "cuda" in tail and "错误" in tail:
        out.append({"text": "显存不足或 GPU 出错", "actions": [
            {"label": "改用 CPU 重试", "kind": "rerun", "body": {"device": "cpu", "mode": mode}}]})
    if "未找到 adtof" in tail or "转录失败" in tail:
        out.append({"text": "转录引擎不可用", "actions": [
            {"label": "打开设置检查组件", "kind": "settings"}]})
    if "没有音符" in tail or "分离没成功" in tail:
        out.append({"text": "这段音频里可能没有可识别的鼓", "actions": [
            {"label": "只出鼓声看看", "kind": "rerun", "body": {"mode": "stems"}}]})
    return out


# ---------------- 诊断包 ----------------
@app.route("/api/diagnostics")
def diagnostics():
    import zipfile
    buf = io.BytesIO()
    notes = []
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, path in (("job.log", LOG_FILE),
                           ("service.log", os.path.join(APP_DIR, "service.log")),
                           ("launcher.log", os.path.join(APP_DIR, "launcher.log")),
                           ("service_crash.log", os.path.join(APP_DIR, "service_crash.log"))):
            try:
                if os.path.isfile(path):
                    with open(path, "rb") as f:
                        data = f.read()[-2 * 1024 * 1024:]
                    z.writestr(name, data)
            except Exception as e:
                notes.append("%s: %s" % (name, e))
        try:
            cfg = dict(_CFG)
            for k in list(cfg):
                if isinstance(cfg[k], str) and (":\\" in cfg[k] or cfg[k].startswith("/")):
                    cfg[k] = "<PATH>"
            z.writestr("config.json", json.dumps(cfg, ensure_ascii=False, indent=1))
        except Exception as e:
            notes.append("config: %s" % e)
        try:
            z.writestr("task_state.json", json.dumps({k: STATE.get(k) for k in STATE_KEYS},
                                                    ensure_ascii=False, indent=1))
            z.writestr("queue.json", json.dumps(_queue_list(), ensure_ascii=False, indent=1))
            z.writestr("job_stats.json", json.dumps(_stats(), ensure_ascii=False, indent=1))
        except Exception as e:
            notes.append("state: %s" % e)
        try:
            g = gpu_snapshot()
            info = ["python: " + sys.version.replace("\n", " "),
                    "platform: " + platform.platform(),
                    "gpu: %s" % json.dumps(g, ensure_ascii=False),
                    "root: <PATH>", "notes: %s" % ("; ".join(notes) or "-")]
            z.writestr("system.txt", "\n".join(info))
        except Exception as e:
            z.writestr("system.txt", "collect failed: %s" % e)
        try:
            listing = []
            for d in (out_dir(), midis_dir()):
                if os.path.isdir(d):
                    for fn in sorted(os.listdir(d))[:200]:
                        try:
                            listing.append("%s/%s\t%d" % (os.path.basename(d), fn,
                                                          os.path.getsize(os.path.join(d, fn))))
                        except Exception:
                            pass
            z.writestr("artifacts.txt", "\n".join(listing))
        except Exception:
            pass
    buf.seek(0)
    name = "drumquill-diagnostics-%s.zip" % time.strftime("%Y%m%d-%H%M%S")
    return send_file(buf, mimetype="application/zip", as_attachment=True, download_name=name)


@app.route("/api/scoremap")
def scoremap():
    """给"动态谱"用：每小节的起止秒 + 该小节的音符（鼓类 + 十六分格位置）。"""
    base = os.path.splitext(os.path.basename(request.args.get("song", "") or ""))[0]
    plan_p = os.path.join(midis_dir(), base + ".beats_plan.json")
    mid_p = os.path.join(midis_dir(), base + ".beats.mid")
    if not base or not os.path.isfile(plan_p) or not os.path.isfile(mid_p):
        return jsonify({"ok": False, "error": "no beat-grid data"}), 404
    try:
        with open(plan_p, encoding="utf-8-sig") as f:
            plan = json.load(f)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    bars = plan.get("bars") or []
    meter = int(plan.get("meter") or 4)
    bpm = float(plan.get("bpm_median") or 120.0)
    try:
        import pretty_midi
        pm = pretty_midi.PrettyMIDI(mid_p)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    cls_of = {35: "kick", 38: "snare", 47: "tom", 42: "hihat", 49: "cymbal"}
    cells = {}
    bar_ticks = meter * 480
    for inst in pm.instruments:
        for n in inst.notes:
            c = cls_of.get(int(n.pitch))
            if not c:
                continue
            tick = int(round(n.start * (bpm / 60.0) * 480))
            bi, t = divmod(tick, bar_ticks)
            cells.setdefault(bi, []).append({"c": c, "p": int(t // 120), "v": int(n.velocity or 100)})
    out = []
    for i, b in enumerate(bars):
        out.append({"i": i, "start": b.get("start"), "end": b.get("end"), "bpm": b.get("bpm"),
                    "notes": sorted(cells.get(i, []), key=lambda x: x["p"])})
    return jsonify({"ok": True, "base": base, "meter": meter, "bars": out,
                    "audio": "/api/download?kind=audio&song=%s&preview=1" % base})


def _pdf_layout(pdf_path):
    """从 MuseScore 导出的 PDF 里读出每个系统行的横线与小节线坐标。"""
    import pymupdf
    doc = pymupdf.open(pdf_path)
    pages = []
    for pno, page in enumerate(doc, 1):
        hl, vl = [], []
        for d in page.get_drawings():
            for it in d["items"]:
                if it[0] != "l":
                    continue
                p1, p2 = it[1], it[2]
                if abs(p1.y - p2.y) < 0.6 and abs(p1.x - p2.x) > 40:
                    hl.append((round(p1.y, 1), round(min(p1.x, p2.x), 1), round(max(p1.x, p2.x), 1)))
                elif abs(p1.x - p2.x) < 0.6:
                    vl.append((round(p1.x, 1), round(min(p1.y, p2.y), 1), round(max(p1.y, p2.y), 1)))
        heads = []
        try:
            for blk in page.get_text("rawdict")["blocks"]:
                for line in blk.get("lines", []):
                    for span in line["spans"]:
                        for ch in span["chars"]:
                            o = ord(ch["c"][0])
                            if 0xE0A0 <= o <= 0xE0FF:      # SMuFL 音符头区间
                                heads.append(ch)
        except Exception:
            heads = []
        ys = sorted({h[0] for h in hl})
        # 五线一组
        syss, i = [], 0
        while i + 4 < len(ys):
            g = ys[i:i + 5]
            step = (g[-1] - g[0]) / 4.0
            if step > 2 and all(abs((g[k + 1] - g[k]) - step) < 0.6 for k in range(4)):
                syss.append((g[0], g[-1], step))
                i += 5
            else:
                i += 1
        systems = []
        for top, bot, step in syss:
            xs = [h for h in hl if abs(h[0] - top) < 0.6]
            if not xs:
                continue
            x0 = min(h[1] for h in xs)
            x1 = max(h[2] for h in xs)
            bars = sorted({x for x, y0, y1 in vl if abs(y0 - top) < 1.5 and abs(y1 - bot) < 1.5})
            bounds = [x0] + [b for b in bars if x0 + 1 < b <= x1 + 1]
            bounds = sorted(set(round(b, 1) for b in bounds))
            if x1 > bounds[-1] + 1:
                bounds.append(round(x1, 1))
            # 音符头（SMuFL 私有区字形）的 x：一列（同一拍位）可能多个鼓，聚类后即该列的位置
            nx = sorted({round(ch["origin"][0], 1) for ch in heads
                         if abs(ch["origin"][1] - top) < (bot - top) + 12})
            systems.append({"top": top, "bottom": bot, "x0": round(x0, 1), "x1": round(x1, 1),
                            "bounds": bounds, "nx": nx})
        pages.append({"page": pno, "w": round(page.rect.width, 1), "h": round(page.rect.height, 1),
                      "systems": systems})
    return pages


@app.route("/api/scorelayout")
def scorelayout():
    """PDF 版谱面的几何（系统行 + 小节线），供前端叠光标。"""
    base = os.path.splitext(os.path.basename(request.args.get("song", "") or ""))[0]
    pdf_p = os.path.join(out_dir(), base + "_鼓谱.pdf")
    if not base or not os.path.isfile(pdf_p):
        return jsonify({"ok": False, "error": "no pdf"}), 404
    try:
        pages = _pdf_layout(pdf_p)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    total = sum(max(0, len(s["bounds"]) - 1) for p in pages for s in p["systems"])
    return jsonify({"ok": True, "base": base, "pages": pages, "bars_detected": total})


@app.route("/api/source")
def api_source():
    """原曲音频（动态谱跟着原曲走：拍点就是从原曲跟出来的，时间轴一致）。"""
    name = os.path.basename(request.args.get("song", "") or "")
    path = os.path.join(songs_dir(), name)
    # 历史列表给的常常是不带扩展名的基名：按常见音频后缀补全
    if name and not os.path.isfile(path) and not os.path.splitext(name)[1]:
        for ext in (".mp3", ".wav", ".flac", ".ogg", ".m4a", ".aac"):
            cand = os.path.join(songs_dir(), name + ext)
            if os.path.isfile(cand):
                path = cand
                break
    if not name or not os.path.isfile(path):
        return jsonify({"ok": False, "error": "not found"}), 404
    return send_file(path, conditional=True)


if __name__ == "__main__":
    os.makedirs(APP_DIR, exist_ok=True)
    _install_crash_hooks()
    _write_service_stamp()
    try:
        with open(os.path.join(APP_DIR, "service_crash.log"), "a", encoding="utf-8") as f:
            f.write("[%s] service start pid=%d root=%s port=%s\n"
                    % (time.strftime("%Y-%m-%d %H:%M:%S"), os.getpid(), ROOT,
                       os.environ.get("DRUM_AI_PORT", "8756")))
            f.flush()
    except Exception:
        pass
    _load_state()  # restore + zombie-guard after a service restart
    start_gpu_probe()  # 后台探测 CUDA，首屏即可显示显卡型号
    port = int(os.environ.get("DRUM_AI_PORT", "8756"))
    print("Drum AI web app: http://127.0.0.1:%d  (close with Ctrl+C)" % port)
    app.run(host="127.0.0.1", port=port, debug=False, threaded=True)
