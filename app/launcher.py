# -*- coding: utf-8 -*-
"""DrumQuill launcher (PyInstaller entry / 源码可直接运行).

Responsibilities:
  0. Resolve project ROOT from: env DRUM_AI_ROOT > frozen exe layout
     (<ROOT>/dist/<app>/exe -> up two levels) > source layout (<ROOT>/app/launcher.py).
     Read optional config.json (venvDir / port).
  1. Ensure the Python venv exists; if missing, prompt and run
     bootstrap\\install.ps1 in a visible console window.
  2. Probe http://127.0.0.1:<port>/api/status.
       - 200  -> reuse existing service.
       - down -> inspect port owner:
                    * pythonw running app.py on this machine -> kill it, start ours.
                    * something else owns the port           -> fall back to port+1.
                    * port free                              -> start ours.
  3. Start service via venv pythonw.exe (CREATE_NO_WINDOW) and poll up to ~25s
     for /api/status 200.
  4. Open a pywebview window pointed at the service.
  5. On window close:
       - if a pipeline job is running -> native confirm: "任务在后台继续，关闭窗口不中断"
       - if no job running AND we started the service (not reused) -> kill our
         shim process tree.
       - if reused (pre-existing service) -> leave it running, just exit.

Logs go to <ROOT>/app/launcher.log (--noconsole has no stdout).
No literal machine paths anywhere; everything derives from ROOT.
"""
import os
import sys
import time
import json
import subprocess
import ctypes
import urllib.request
import urllib.error

READY_TIMEOUT = 25.0  # seconds
_APP_DIR_CACHE = None


def app_fingerprint():
    """app/*.py 与 *.html 的（mtime,size）指纹。

    服务启动时把指纹写进 service_stamp.json；启动器再算一次，
    不一致说明程序目录被更新过，而复用的老服务跑的是旧代码 —— 必须重启。
    app.py 里有一份同样的算法，两边必须保持一致。
    """
    d = app_dir()
    try:
        names = sorted(f for f in os.listdir(d) if f.endswith((".py", ".html")))
    except Exception:
        return None
    h = 0
    for fn in names:
        try:
            st = os.stat(os.path.join(d, fn))
        except Exception:
            continue
        h = (h * 131 + int(st.st_mtime) + st.st_size) & 0xFFFFFFFF
    return h


def stamp_path():
    """戳记在真正的 app 目录里（冻结后 __file__ 在 _internal 里，不能用它）。"""
    return os.path.join(app_dir(), "service_stamp.json")


def app_dir():
    r"""项目里 app\ 目录的绝对路径（冻结与源码两种运行方式都要对）。"""
    global _APP_DIR_CACHE
    if _APP_DIR_CACHE:
        return _APP_DIR_CACHE
    d = os.path.join(detect_root(), "app")
    if not os.path.isfile(os.path.join(d, "app.py")):
        # 兜底：退回到 launcher 自身所在目录（源码模式）
        d = os.path.dirname(os.path.abspath(__file__))
    _APP_DIR_CACHE = d
    return d


def service_is_stale():
    """正在运行的服务是不是旧代码。没有戳记时按“不旧”处理，避免每次启动都重启。"""
    try:
        with open(stamp_path(), "r", encoding="utf-8") as f:
            rec = json.load(f)
    except Exception:
        return False
    cur = app_fingerprint()
    if cur is None or rec.get("stamp") is None:
        return False
    try:
        return int(rec["stamp"]) != int(cur)
    except Exception:
        return False



# --------------------------------------------------------------------------
# Path / config resolution
# --------------------------------------------------------------------------

def detect_root():
    """Return absolute project root. Callers may override via DRUM_AI_ROOT."""
    env_root = os.environ.get("DRUM_AI_ROOT", "").strip()
    if env_root and os.path.isdir(env_root):
        return os.path.abspath(env_root)
    if getattr(sys, "frozen", False):
        # 发行布局: <ROOT>/dist/DrumQuill/DrumQuill.exe -> ROOT = up two levels
        exe_dir = os.path.dirname(os.path.abspath(sys.executable))
        cand = os.path.dirname(os.path.dirname(exe_dir))
        if os.path.isfile(os.path.join(cand, "run_pipeline.ps1")) or \
           os.path.isfile(os.path.join(cand, "app", "app.py")):
            return cand
        # 整目录拷贝走时，退而求其次用 exe 所在目录
        return exe_dir
    # 源码布局: <ROOT>/app/launcher.py
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


ROOT = detect_root()
APP_DIR = os.path.join(ROOT, "app")
APP_PY = os.path.join(APP_DIR, "app.py")
LOG_FILE = os.path.join(APP_DIR, "launcher.log")
SERVICE_LOG = os.path.join(APP_DIR, "service.log")


def load_config():
    try:
        with open(os.path.join(ROOT, "config.json"), encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


CFG = load_config()
DEFAULT_PORT = int(CFG.get("port") or 8756)
FALLBACK_PORT = DEFAULT_PORT + 1


def resolve_venv_pythonw():
    """config.json venvDir 兼容两种写法：
       - venv 根目录（如 C:\\...\\venvs\\drum-ai）-> 自动补 Scripts\\pythonw.exe
       - pythonw.exe 完整路径 -> 直接使用
       缺省 -> <ROOT>/.venv/Scripts/pythonw.exe"""
    v = (CFG.get("venvDir") or "").strip()
    if not v:
        return os.path.join(ROOT, ".venv", "Scripts", "pythonw.exe")
    v = os.path.abspath(v)
    if os.path.basename(v).lower().startswith("python"):
        return v
    return os.path.join(v, "Scripts", "pythonw.exe")


VENV_PYTHONW = resolve_venv_pythonw()


def log(msg):
    line = "[%s] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    try:
        os.makedirs(APP_DIR, exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def native_confirm(title, text):
    """MB_YESNO | MB_ICONQUESTION | MB_DEFBUTTON2. Returns True on Yes."""
    MB_YESNO = 0x4
    MB_ICONQUESTION = 0x20
    MB_DEFBUTTON2 = 0x100
    flags = MB_YESNO | MB_ICONQUESTION | MB_DEFBUTTON2
    return ctypes.windll.user32.MessageBoxW(0, text, title, flags) == 6  # IDYES=6


def native_error(text):
    MB_OK = 0x0
    MB_ICONERROR = 0x10
    ctypes.windll.user32.MessageBoxW(0, text, "DrumQuill", MB_OK | MB_ICONERROR)


# --------------------------------------------------------------------------
# Dependency ensure (first-run installer launch)
# --------------------------------------------------------------------------

def find_system_pythonw():
    """Locate a system pythonw.exe via the `py` launcher (falls back to PATH)."""
    try:
        r = subprocess.run(["py", "-3", "-c", "import sys;print(sys.executable)"],
                           capture_output=True, text=True, timeout=10,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        out = (r.stdout or "").strip().splitlines()
        if out and out[-1].lower().endswith("python.exe"):
            return out[-1][:-len("python.exe")] + "pythonw.exe"
    except Exception:
        pass
    for d in os.environ.get("PATH", "").split(";"):
        if d:
            c = os.path.join(d, "pythonw.exe")
            if os.path.exists(c):
                return c
    return None


def resolve_pythonw():
    """Return a pythonw that can run app.py: venv first, system python as
    fallback (system Python ships Flask/installer stdlib here, so the Web
    first-run wizard can drive installation — no visible installer console).
    """
    if os.path.exists(VENV_PYTHONW):
        return VENV_PYTHONW
    sys_pyw = find_system_pythonw()
    if sys_pyw:
        log("venv missing; using system pythonw: %s (Web wizard will install)" % sys_pyw)
        return sys_pyw
    return None


# --------------------------------------------------------------------------
# Service probing / control (unchanged behavior, root-relative paths)
# --------------------------------------------------------------------------

def http_get_status(port, timeout=2.0):
    url = "http://127.0.0.1:%d/api/status" % port
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            if r.status == 200:
                data = json.loads(r.read().decode("utf-8", "replace"))
                return True, data
    except Exception:
        return False, None
    return False, None


def port_owner(port):
    """Return (pid, name, commandline) of the listener on `port`, or None."""
    ps = (
        "$ErrorActionPreference='SilentlyContinue';"
        "$c = Get-NetTCPConnection -LocalPort %d -State Listen | Select-Object -First 1;"
        "if ($c) {"
        "  $p = Get-CimInstance Win32_Process -Filter ('ProcessId=' + $c.OwningProcess);"
        "  Write-Output ($c.OwningProcess.ToString() + '|' + $p.Name + '|' + $p.CommandLine)"
        "}" % port
    )
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True, text=True, timeout=8,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).stdout.strip()
    except Exception as e:
        log("port_owner query failed: %r" % e)
        return None
    if not out:
        return None
    parts = out.split("|", 2)
    if len(parts) < 3:
        return None
    try:
        return int(parts[0]), parts[1] or "", parts[2] or ""
    except Exception:
        return None


def kill_tree(pid):
    try:
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)],
                       capture_output=True, timeout=10,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        log("taskkill /T /F /PID %d done" % pid)
    except Exception as e:
        log("kill_tree(%d) failed: %r" % (pid, e))


FALLBACK_PORT_TRIES = 10


def pick_fallback_port(start_port):
    """回退端口同样要探测占用（F8）：8757 一带是常见代理/开发端口段，盲目启用会让
    app.py 绑定失败，launcher 只会在 25 秒后报「服务启动超时」，用户无从排查。"""
    for i in range(FALLBACK_PORT_TRIES):
        cand = start_port + i
        if port_owner(cand) is None:
            return cand
        log("fallback port %d is occupied too" % cand)
    last = start_port + FALLBACK_PORT_TRIES - 1
    log("no free port in %d..%d, using %d anyway" % (start_port, last, last))
    return last


def start_service(port, pythonw_path):
    """Launch venv pythonw shim running app.py. Return Popen."""
    env = dict(os.environ)
    env["DRUM_AI_ROOT"] = ROOT
    env["DRUM_AI_PORT"] = str(port)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    log("starting service: %s %s (DRUM_AI_PORT=%d)" % (pythonw_path, APP_PY, port))
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    # 服务的 stdout/stderr 落盘：pythonw 没有控制台，DEVNULL 会把崩溃 traceback 全丢掉，
    # 出了“任务跑到一半服务没了”这类问题就完全无从下手。
    try:
        os.makedirs(APP_DIR, exist_ok=True)
        svc_out = open(SERVICE_LOG, "a", encoding="utf-8", errors="replace")
        svc_out.write("\n===== service start %s (port %d) =====\n"
                      % (time.strftime("%Y-%m-%d %H:%M:%S"), port))
        svc_out.flush()
    except Exception as e:
        log("cannot open service log: %r" % e)
        svc_out = subprocess.DEVNULL
    proc = subprocess.Popen(
        [pythonw_path, APP_PY],
        env=env, creationflags=creationflags,
        stdout=svc_out, stderr=subprocess.STDOUT,
        cwd=APP_DIR,
    )
    if svc_out is not subprocess.DEVNULL:
        try:
            svc_out.close()  # 句柄已交给子进程，父进程不必留着
        except Exception:
            pass
    return proc


def wait_ready(port, timeout=READY_TIMEOUT):
    t0 = time.time()
    while time.time() - t0 < timeout:
        ok, _ = http_get_status(port, timeout=1.0)
        if ok:
            log("service ready on port %d after %.1fs" % (port, time.time() - t0))
            return True
        time.sleep(0.5)
    log("service NOT ready on port %d after %.1fs" % (port, timeout))
    return False


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main():
    log("=" * 60)
    log("launcher starting, exe=%s ROOT=%s" % (sys.executable, ROOT))

    # 0. resolve usable pythonw (venv first, system python fallback)
    pythonw_path = resolve_pythonw()
    if not pythonw_path:
        native_error(
            "找不到可用的 Python 运行时。\n"
            "请先安装 Python 3.9+（https://www.python.org/downloads/）后重新启动。"
        )
        log("FATAL: no pythonw found at all")
        return 1
    if not os.path.isfile(APP_PY):
        native_error("找不到服务程序：\n%s\n请确认程序目录完整。" % APP_PY)
        log("FATAL: app.py missing at %s" % APP_PY)
        return 1

    # 1. probe default port
    port = DEFAULT_PORT
    ok, data = http_get_status(port)
    reused = False
    our_proc = None

    if ok:
        # 探活成功 ≠ 是自己的服务：另一份工作区/旧版也在跑 app.py（设计内的 8756/8757 并存）。
        # 不核对 owner 就复用，用户会「双击新版却继续用旧代码」。必须比对命令行里的 app.py 路径。
        owner = port_owner(port)
        mine = bool(owner) and os.path.normcase(os.path.abspath(APP_PY)) in os.path.normcase(owner[2] or "")
        if not mine:
            port = pick_fallback_port(FALLBACK_PORT)
            log("port %d served by ANOTHER app.py (%r) -> starting ours on %d"
                % (DEFAULT_PORT, (owner[2] if owner else "?")[:120], port))
            our_proc = start_service(port, pythonw_path)
            if not wait_ready(port):
                native_error("DrumQuill 服务启动超时。\n请查看日志：\n" + LOG_FILE)
                return 1
        elif service_is_stale():
            # 程序目录被更新过（例如换了新版 app 文件），老服务内存里还是旧代码
            owner = port_owner(port)
            log("running service uses stale code (stamp mismatch) -> restart")
            if owner:
                log("killing stale service pid=%d name=%s" % (owner[0], owner[1]))
                kill_tree(owner[0])
                time.sleep(1.0)
            our_proc = start_service(port, pythonw_path)
            if not wait_ready(port):
                native_error("DrumQuill 服务重启超时。\n请查看日志：\n" + LOG_FILE)
                return 1
        else:
            reused = True
            log("reusing existing service on %d (running=%s)" % (port, data.get("running")))
    else:
        owner = port_owner(port)
        if owner:
            pid, name, cmd = owner
            log("port %d occupied by pid=%d name=%s cmd=%r" % (port, pid, name, cmd))
            is_ours = ("pythonw" in (name or "").lower()) and ("app.py" in (cmd or "").lower())
            if is_ours:
                log("occupant looks like a stale app.py -> killing")
                kill_tree(pid)
                time.sleep(1.0)
                our_proc = start_service(port, pythonw_path)
            else:
                port = pick_fallback_port(FALLBACK_PORT)
                log("port owned by unrelated process -> using fallback %d" % port)
                our_proc = start_service(port, pythonw_path)
        else:
            log("port %d free -> starting service" % port)
            our_proc = start_service(port, pythonw_path)

        if our_proc is not None:
            if not wait_ready(port):
                native_error("DrumQuill 服务启动超时。\n请查看日志：\n" + LOG_FILE)
                return 1

    # 2. open pywebview window (EXE build) or default browser (source run)
    if not getattr(sys, "frozen", False):
        import webbrowser
        webbrowser.open("http://127.0.0.1:%d/" % port)
        log("source mode: opened default browser; launcher exiting (service keeps running)")
        return 0
    try:
        import webview
    except Exception as e:
        log("pywebview import failed: %r -> fallback to webbrowser" % e)
        import webbrowser
        webbrowser.open("http://127.0.0.1:%d/" % port)
        log("opened default browser; launcher exiting (service keeps running)")
        return 0

    url = "http://127.0.0.1:%d/" % port
    log("opening pywebview window -> %s" % url)
    # pywebview 默认 ALLOW_DOWNLOADS=False：桌面窗口里点「下载」会被静默取消
    # （edgechromium.on_download_starting 里 args.Cancel = True），表现就是"点了没反应"。
    try:
        webview.settings['ALLOW_DOWNLOADS'] = True
        log("ALLOW_DOWNLOADS enabled (otherwise downloads are silently cancelled)")
    except Exception as e:
        log("cannot enable ALLOW_DOWNLOADS: %r" % e)
    window = webview.create_window(
        "DrumQuill · 扒鼓谱", url=url,
        width=1200, height=860,
        min_size=(980, 700),
    )

    def on_closing():
        # query running state at close time
        ok, st = http_get_status(port, timeout=1.5)
        running = bool(ok and st and st.get("running"))
        log("window closing event: reused=%s running=%s ok=%s" % (reused, running, ok))
        if running:
            go = native_confirm(
                "DrumQuill",
                "后台有扒谱任务正在运行。\n\n"
                "关闭本窗口不会中断任务（服务继续在后台运行）。\n\n"
                "是否仍然关闭窗口？",
            )
            if not go:
                return False  # prevent close
            # user chose to close; leave service running
            return True
        # no job running
        if reused:
            log("reused service, leaving it running on exit")
            return True
        if our_proc is not None:
            log("we started the service and idle -> killing our process tree pid=%d" % our_proc.pid)
            kill_tree(our_proc.pid)
            # belt-and-suspenders: also kill whatever listens on port now
            own = port_owner(port)
            if own and own[0] != our_proc.pid:
                log("also killing port owner pid=%d" % own[0])
                kill_tree(own[0])
        return True

    window.events.closing += on_closing

    try:
        webview.start()
    except Exception as e:
        log("webview.start crashed: %r" % e)
        native_error("窗口启动失败: %r\n请查看日志：%s" % (e, LOG_FILE))
        return 1

    log("launcher exiting cleanly")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        log("FATAL: %r" % e)
        try:
            ctypes.windll.user32.MessageBoxW(0, "DrumQuill 启动失败: %r" % e,
                                            "DrumQuill", 0x10)
        except Exception:
            pass
        sys.exit(1)
