# -*- coding: utf-8 -*-
"""
app/installer.py — 组件安装器（引导页 / 设置重装共用）。

组件清单（可单独选装）:
  venv     Python 虚拟环境
  pip      Python 依赖包（torch CPU / demucs / librosa / flask 等，走清华镜像）
  demucs_w Demucs htdemucs 权重（约 84MB，官方源）
  adtof    ADTOF 转录引擎（源码 + 权重，ghproxy 加速链）
  ffmpeg   ffmpeg 转码器（npmmirror 淘宝源）
  mscore   MuseScore 4（渲染 PDF / 音频，choco 或手动）
  sf3      MS Basic.sf3 音色库（从 MuseScore 安装目录复制）

运行方式：后台线程执行，进度写入全局 STATE，前端轮询 /api/setup/progress。
所有下载：多源镜像链 + 断点续传 + 大小校验；pip 一律走清华镜像；
绝不动系统 Python 的任何包（只建独立 venv / 在 venv 内装包）。
"""
import json
import os
import shutil
import subprocess
import threading
import time
import urllib.request
import urllib.error
import gzip

# Windows 上静默启动子进程；非 Windows 回退 0（无副作用）
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# ---------------- 组件定义 ----------------
# required=True 的组件缺失时"根本没法扒谱"，其余缺失只是功能降级。
# group 用于界面分组；skip_by_default 的组件默认不勾选（历史遗留/用不到）。
COMPONENTS = [
    {"id": "venv", "name": "Python 运行环境", "size": "-", "required": True, "group": "基础",
     "desc": "独立的虚拟环境，不碰你系统的 Python",
     "feature": "运行全部功能",
     "without": "软件无法启动"},
    {"id": "pip", "name": "核心依赖包", "size": "约 1.5GB", "required": True, "group": "基础",
     "desc": "torch(CPU) / demucs / librosa / flask 等",
     "feature": "分离、转录、网页界面",
     "without": "软件无法启动"},
    {"id": "adtof", "name": "鼓点转录引擎 ADTOF", "size": "3.6MB 权重", "required": True, "group": "基础",
     "desc": "把鼓声识别成鼓谱 MIDI —— 扒谱的核心",
     "feature": "扒谱",
     "without": "无法扒谱（没有转录引擎）"},
    {"id": "demucs_w", "name": "鼓轨分离模型 Demucs", "size": "84MB", "required": False, "group": "增强",
     "desc": "先从混音里抽出鼓轨，再送去转录",
     "feature": "扒谱更准、更快",
     "without": "仍可扒谱：直接对整曲转录，镲片/加花更容易错、耗时更长"},
    {"id": "beats_w", "name": "节拍跟踪模型 Beat This", "size": "77MB", "required": False, "group": "增强",
     "desc": "跟踪每一拍与强拍，谱面节奏跟着音乐自己的速度走",
     "feature": "谱面与小节跟音乐对得上（有鼓组变速/非 4/4 时差别最大）",
     "without": "按全曲一个固定速度记谱：听得出、但小节常对不上，建议补装"},
    {"id": "mscore", "name": "MuseScore 4 排版", "size": "约 400MB", "required": False,
     "group": "增强", "manual": True,
     "desc": "专业五线谱排版与 PDF 渲染",
     "feature": "出版级鼓谱 PDF",
     "without": "用内置渲染出 PDF：能看能打印，排版朴素一些"},
    {"id": "ffmpeg", "name": "ffmpeg 转码", "size": "约 80MB", "required": False, "group": "增强",
     "desc": "把合成好的鼓声压成 MP3",
     "feature": "体积小 5–10 倍的鼓声 MP3",
     "without": "鼓声音频保存为 WAV（体积大，但能正常听）"},
    {"id": "gpu", "name": "GPU 加速环境", "size": "约 200MB", "required": False, "group": "加速",
     "desc": "复用系统里带 CUDA 的 Python，只补装 demucs",
     "feature": "分离快约 3 倍",
     "without": "用 CPU 分离（慢约 3 倍，功能不受影响）"},
    {"id": "sf3", "name": "MS Basic 音色库", "size": "51MB", "required": False, "group": "其他",
     "skip_by_default": True,
     "desc": "旧版本用它合成鼓声；当前流程改由 MuseScore 或内置合成器生成",
     "feature": "（当前流程未使用）",
     "without": "无影响，可以不装"},
]
COMPONENT_IDS = [c["id"] for c in COMPONENTS]
# 「推荐」= 基础 + 增强（不含 GPU、不含历史遗留）
RECOMMENDED_IDS = [c["id"] for c in COMPONENTS if c["id"] not in ("gpu", "sf3")]
# 「最小可用」= 只要能扒出谱
MINIMAL_IDS = ["venv", "pip", "adtof"]


# pip 源：不同地区/网络下可用性差别很大（实测：同一台机器上腾讯可用、清华整站不可达），
# 所以不写死单一源，而是按顺序探测 + 失败自动换下一个。
PYPI_MIRRORS = [
    "https://mirrors.tencent.com/pypi/simple",
    "https://mirrors.aliyun.com/pypi/simple",
    "https://pypi.tuna.tsinghua.edu.cn/simple",
    "https://mirrors.ustc.edu.cn/pypi/simple",
    None,   # None = 官方 PyPI
]
PYPI_MIRROR = PYPI_MIRRORS[0]        # 兼容旧引用（默认首选）
GPU_PYPI_MIRROR = PYPI_MIRRORS[0]
_UNSET = "__unset__"          # 注意：不能用 None，None 本身代表"官方 PyPI"这个合法源
_MIRROR_STATE = {"good": _UNSET}


def _mirror_order():
    """上次成功的源优先，其余按列表顺序（默认腾讯 → 阿里 → 清华 → 中科大 → 官方）。"""
    good = _MIRROR_STATE.get("good")
    order = list(PYPI_MIRRORS)
    if good != _UNSET and good in order:
        order.remove(good)
        order.insert(0, good)
    return order


def _pip_install(py, args, timeout=3600, what="pip install"):
    """带多源回退的 pip install。args 是不含 -i 的参数列表。返回 True/False。"""
    for mirror in _mirror_order():
        cmd = [py, "-m", "pip", "install"] + list(args) + ["--timeout", "60", "--retries", "2", "--prefer-binary"]
        if mirror:
            cmd += ["-i", mirror]
        tag = mirror.replace("https://", "").split("/")[0] if mirror else "官方 PyPI"
        try:
            r = subprocess.run(cmd, capture_output=True, creationflags=CREATE_NO_WINDOW,
                               text=True, timeout=timeout, errors="replace")
            if r.returncode == 0:
                _MIRROR_STATE["good"] = mirror
                _append_log("  ✓ %s 完成（源：%s）" % (what, tag))
                return True
            tail = (r.stderr or r.stdout or "")[-300:]
            _append_log("  … 源 %s 失败，换下一个（%s）" % (tag, tail.replace("\n", " ")[:160]))
        except Exception as e:
            _append_log("  … 源 %s 异常，换下一个（%s）" % (tag, str(e)[:120]))
    _append_log("  ✗ %s：所有 pip 源都失败，请检查网络/代理后重试" % what)
    return False
DEMUCS_URLS = [
    "https://dl.fbaipublicfiles.com/demucs/hybrid_transformer/955717e8-8726e21a.th",
]
ADTOF_ZIP_URLS = [
    "https://ghproxy.net/https://github.com/xavriley/ADTOF-pytorch/archive/refs/heads/main.zip",
    "https://gh-proxy.com/https://github.com/xavriley/ADTOF-pytorch/archive/refs/heads/main.zip",
]
ADTOF_W_URLS = [
    "https://ghproxy.net/https://raw.githubusercontent.com/xavriley/ADTOF-pytorch/main/data/adtof_frame_rnn_pytorch_weights.pth",
    "https://gh-proxy.com/https://raw.githubusercontent.com/xavriley/ADTOF-pytorch/main/data/adtof_frame_rnn_pytorch_weights.pth",
    "https://raw.githubusercontent.com/xavriley/ADTOF-pytorch/main/data/adtof_frame_rnn_pytorch_weights.pth",
]
FFMPEG_GZ_URLS = [
    "https://registry.npmmirror.com/-/binary/ffmpeg-static/b6.1.1/ffmpeg-win32-x64.gz",
    "https://registry.npmmirror.com/-/binary/ffmpeg-static/b6.1.1/ffmpeg-win32-x64",
]
SIZE_DEMUCS_TH = 84141911
SIZE_ADTOF_PTH = 3617805
SIZE_SF3 = 51278610
SIZE_FFMPEG_EXE = 82797568
# Beat This 权重（torch hub 缓存里的 beat_this-final0.ckpt）
SIZE_BEATS_CKPT = 81058141
BEATS_CKPT_NAME = "beat_this-final0.ckpt"
BEATS_URLS = ["https://cloud.cp.jku.at/public.php/dav/files/7ik4RrBKTS273gp/final0.ckpt"]
BEATS_MIN_BYTES = 60 * 1024 * 1024      # 低于这个数就是没下完的残片
PIP_KEY_IMPORTS = ["torch", "demucs", "beat_this", "librosa", "pretty_midi", "numpy", "soundfile", "flask", "requests"]
PIP_OPT_IMPORTS = ["webview", "reportlab", "cairosvg"]

# ---------------- 全局状态 ----------------
STATE = {
    "running": False, "done": False, "ok": False,
    "step_index": 0, "steps": [], "current": "",
    "log": [], "log_seq": 0,
    "failed": [], "manual": [],
    "started_at": None, "finished_at": None, "dry_run": False,
}
_lock = threading.Lock()


def _append_log(msg):
    with _lock:
        STATE["log"].append({"seq": STATE["log_seq"], "text": str(msg)})
        STATE["log_seq"] += 1


def _set_step(i, total, name, detail=""):
    with _lock:
        STATE["step_index"] = i
        STATE["steps"] = [{"index": i, "total": total, "name": name, "detail": detail}]
        STATE["current"] = name
    _append_log("[%d/%d] %s %s" % (i + 1, total, name, (" - " + detail if detail else "")))


# ---------------- 工具 ----------------
def _read_config(root):
    cfg = {}
    p = os.path.join(root, "config.json")
    if os.path.exists(p):
        try:
            with open(p, "r", encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception:
            cfg = {}
    return cfg


def _write_config(root, cfg):
    p = os.path.join(root, "config.json")
    with open(p, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def _find_system_python(root=None):
    r"""找一个能用 -m venv 的 Python（优先包内 runtime，其次 py -3 启动器/系统 Python）。

    整合包场景：目标机可能没有系统 Python；包内 runtime\python 是完整 CPython，
    可直接 -m venv（已实测），必须排在系统 Python 之前，GPU 环境后装才能在干净机器走通。
    """
    cands = []
    bundled = os.path.join(root, "runtime", "python", "python.exe") if root else ""
    if bundled and os.path.isfile(bundled):
        cands.append([bundled])
    cands += [["py", "-3"], ["python"], ["python3"]]
    for cand in cands:
        try:
            r = subprocess.run(cand + ["-c", "import sys;print(sys.executable)"],
                               capture_output=True, creationflags=CREATE_NO_WINDOW, text=True, timeout=20)
            out = (r.stdout or "").strip().splitlines()
            if r.returncode == 0 and out:
                return out[-1].strip()
        except Exception:
            continue
    return None


def _seed_vc_runtime(dst_dir, root):
    """把随包的 VC++ 运行库（app-local）带进新建的解释器目录：纯净 Windows 可能没有
    msvcp140/vcruntime140，torch/numpy 的 DLL 依赖会解析失败。这些 DLL 为微软
    可再分发组件，随应用部署是官方允许的方式。"""
    src = os.path.join(root, ".venv", "Scripts")
    if not os.path.isdir(src):
        return
    for name in ("msvcp140.dll", "vcruntime140.dll", "vcruntime140_1.dll"):
        p = os.path.join(src, name)
        if os.path.isfile(p) and not os.path.isfile(os.path.join(dst_dir, name)):
            try:
                shutil.copy2(p, dst_dir)
                _append_log("  随包运行库 -> %s" % os.path.join(dst_dir, name))
            except Exception:
                pass


def _venv_py(root, cfg):
    v = cfg.get("venvDir")
    if v:
        cand = v if v.lower().endswith("python.exe") or v.lower().endswith("pythonw.exe") \
            else os.path.join(v, "Scripts", "python.exe")
        if os.path.exists(cand):
            return cand
    cand = os.path.join(root, ".venv", "Scripts", "python.exe")
    if os.path.exists(cand):
        return cand
    return None


def _file_ok(path, expect, tol=0.02):
    if not path or not os.path.exists(path):
        return False
    size = os.path.getsize(path)
    if size <= 0:
        return False  # 0 字节（下载被中断 / 占位文件）绝不能算“已就绪”
    if expect <= 0:
        return True
    return expect * (1 - tol) <= size <= expect * (1 + tol)


def _find_mscore():
    for pf in [os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)")]:
        if not pf:
            continue
        for p in [os.path.join(pf, d) for d in os.listdir(pf) if d.startswith("MuseScore")]:
            exe = os.path.join(p, "bin", "MuseScore4.exe")
            if os.path.exists(exe):
                return exe
    return None


def _find_ffmpeg(root, cfg):
    v = cfg.get("ffmpegPath")
    if v and os.path.exists(v):
        return v
    local = os.path.join(root, "tools", "ffmpeg", "bin", "ffmpeg.exe")
    if os.path.exists(local):
        return local
    for d in os.environ.get("PATH", "").split(";"):
        if d:
            exe = os.path.join(d, "ffmpeg.exe")
            if os.path.exists(exe):
                return exe
    return None


# ---------------- 检测 ----------------
# 注意：这个模板用 __MODS__ 占位再替换，不能用 % 格式化 ——
# 里面有 "%d.%d" % sys.version_info 这种百分号，会和外层的 % 格式化打架。
_PROBE_SRC = r"""
import json, sys, importlib
out = {}
out["version"] = "{}.{}".format(sys.version_info[0], sys.version_info[1])
miss = []
for m in __MODS__:
    try:
        importlib.import_module(m)
    except Exception:
        miss.append(m)
out["missing"] = miss
try:
    import os, adtof_pytorch
    out["adtof"] = os.path.dirname(adtof_pytorch.__file__)
except Exception:
    out["adtof"] = ""
print(json.dumps(out))
"""


def _venv_probe(venv_py):
    """一次子进程把 venv 版本 / 缺失依赖 / adtof 路径全查出来。

    之前分三次 spawn python，每次都要 import torch/librosa，一次检测要 7-8 秒；
    合并成一次后只剩一次 import 开销，再配合 app.py 的缓存就够快了。
    """
    info = {"version": "", "missing": list(PIP_KEY_IMPORTS), "adtof": ""}
    if not venv_py:
        return info
    try:
        import json as _json
        src = _PROBE_SRC.replace("__MODS__", repr(list(PIP_KEY_IMPORTS)))
        r = subprocess.run([venv_py, "-c", src], capture_output=True, creationflags=CREATE_NO_WINDOW,
                           text=True, timeout=180, errors="replace")
        lines = [l for l in (r.stdout or "").strip().splitlines() if l.strip()]
        if lines:
            info.update(_json.loads(lines[-1]))
    except Exception:
        pass
    return info


def _beats_ckpt_candidates(root, cfg):
    """Beat This 权重可能出现的位置（新位置在前）。"""
    models = cfg.get("modelsDir") or os.path.join(root, "models")
    out = [os.path.join(models, "torch", "hub", "checkpoints", BEATS_CKPT_NAME),
           os.path.join(models, "torch-hub", "hub", "checkpoints", BEATS_CKPT_NAME)]
    th = os.environ.get("TORCH_HOME")
    if th:
        out.append(os.path.join(th, "hub", "checkpoints", BEATS_CKPT_NAME))
    out.append(os.path.join(os.path.expanduser("~"), ".cache", "torch", "hub", "checkpoints", BEATS_CKPT_NAME))
    return out


def _beats_ckpt(root, cfg):
    """返回 (可用权重路径 or None, 说明文字)。<60MB 一律当残片。"""
    partial = None
    for p in _beats_ckpt_candidates(root, cfg):
        try:
            if not os.path.isfile(p):
                continue
            if os.path.getsize(p) >= BEATS_MIN_BYTES:
                return p, p
            partial = p
        except Exception:
            pass
    if partial:
        try:
            mb = os.path.getsize(partial) / 1024.0 / 1024.0
        except Exception:
            mb = 0.0
        return None, "权重没下完（%0.1fMB）" % mb
    return None, "未下载"


def _beats_target(root, cfg):
    """权威位置：仓库内 models\\torch\\hub\\checkpoints（与 track_beats.py 一致）。"""
    models = cfg.get("modelsDir") or os.path.join(root, "models")
    return os.path.join(models, "torch", "hub", "checkpoints", BEATS_CKPT_NAME)


def _pin_beats_ckpt(root, cfg):
    """把散落在别处的权重收敛到标准位置，并清掉没下完的残片。"""
    dst = _beats_target(root, cfg)
    got, _note = _beats_ckpt(root, cfg)
    if got and os.path.abspath(got) != os.path.abspath(dst):
        try:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(got, dst)
            _append_log("  已把权重归位到 %s" % dst)
        except Exception as e:
            _append_log("  权重归位失败（不影响使用）: %s" % str(e)[:120])
    for p in _beats_ckpt_candidates(root, cfg):
        try:
            if os.path.isfile(p) and os.path.getsize(p) < BEATS_MIN_BYTES:
                os.remove(p)
                _append_log("  清掉没下完的残片: %s" % p)
        except Exception:
            pass


def check_status(root, cfg, gpu=None):
    """返回每个组件的就绪状态。gpu 可传入 app.py 已缓存好的探测结果（避免再 import torch）。"""
    out = {}
    venv_py = _venv_py(root, cfg)
    probe = _venv_probe(venv_py) if venv_py else {"version": "", "missing": list(PIP_KEY_IMPORTS), "adtof": ""}

    # venv
    venv_ok = False
    if probe.get("version"):
        try:
            major, minor = [int(x) for x in str(probe["version"]).split(".")[:2]]
            venv_ok = (major, minor) >= (3, 9)
        except Exception:
            venv_ok = False
    out["venv"] = {"ok": venv_ok, "note": venv_py or ""}

    # pip
    miss = probe.get("missing") or []
    out["pip"] = {"ok": bool(venv_py) and venv_ok and not miss,
                  "note": ("缺 " + ",".join(miss)) if miss else ("venv 内" if venv_py else "未找到 venv")}

    # demucs_w
    dem_dir = os.path.join(cfg.get("modelsDir") or os.path.join(root, "models"), "demucs_local")
    dem_th = os.path.join(dem_dir, "955717e8-8726e21a.th")
    dem_ok = _file_ok(dem_th, SIZE_DEMUCS_TH)
    out["demucs_w"] = {"ok": dem_ok, "note": dem_th if dem_ok else ("缺权重 %s" % dem_th)}

    # beats_w（Beat This 拍点权重，torch hub 缓存）
    bp, bnote = _beats_ckpt(root, cfg)
    out["beats_w"] = {"ok": bool(bp), "note": bp if bp else bnote}

    # adtof
    adtof_ok = False
    adtof_note = "引擎未装"
    pkg = probe.get("adtof") or ""
    if pkg:
        w = os.path.join(pkg, "data", "adtof_frame_rnn_pytorch_weights.pth")
        adtof_ok = _file_ok(w, SIZE_ADTOF_PTH)
        adtof_note = w if adtof_ok else "权重缺失"
    out["adtof"] = {"ok": adtof_ok, "note": adtof_note}

    # ffmpeg
    ff = _find_ffmpeg(root, cfg)
    out["ffmpeg"] = {"ok": bool(ff), "note": ff or "未找到"}

    # mscore
    ms = _find_mscore()
    out["mscore"] = {"ok": bool(ms), "note": ms or "未安装"}

    # sf3
    sf3 = os.path.join(cfg.get("soundDir") or os.path.join(root, "sound"), "MS_Basic.sf3")
    sf3_ok = _file_ok(sf3, SIZE_SF3)
    out["sf3"] = {"ok": sf3_ok, "note": sf3 if sf3_ok else "未安装（当前流程未使用）"}

    # gpu：优先用调用方缓存的探测结果；探测未完成时先按"环境是否存在"给个初值
    genv = cfg.get("gpuVenvDir") or os.path.join(root, ".venv-gpu")
    gpy = cfg.get("gpuPython") or os.path.join(genv, "Scripts", "python.exe")
    if gpu is not None and gpu.get("status") == "ready":
        gpu_ok = bool(gpu.get("available"))
        if gpu_ok:
            gpu_note = "%s（%s）" % (gpu.get("name") or "GPU", gpu.get("python") or gpy)
        else:
            gpu_note = gpu.get("reason") or "未启用"
    elif os.path.exists(gpy):
        gpu_ok = True
        gpu_note = "检测中…（%s）" % gpy
    else:
        gpu_ok = False
        gpu_note = "未安装"
    out["gpu"] = {"ok": gpu_ok, "note": gpu_note}

    return out


def need_setup(status):
    """必需组件缺失 -> 需要引导安装。"""
    for c in COMPONENTS:
        if c.get("required") and not status.get(c["id"], {}).get("ok"):
            return True
    return False


def can_transcribe(status):
    """能不能至少扒出谱（必需组件齐全）。"""
    for c in COMPONENTS:
        if c.get("required") and not status.get(c["id"], {}).get("ok"):
            return False
    return True


def capabilities(status):
    """把组件状态翻译成"现在能干什么 / 缺什么会退化成什么"，供界面直接显示。

    每条：key / name / ok / required / fallback（缺失时的降级表现）/ need（需要哪些组件）
    """
    def ok_all(*ids):
        return all(status.get(i, {}).get("ok") for i in ids)

    return [
        {"key": "run", "name": "启动与界面", "ok": ok_all("venv", "pip"),
         "required": True, "need": ["venv", "pip"],
         "fallback": "", "note": "" if ok_all("venv", "pip") else "缺少运行环境，软件无法启动"},
        {"key": "transcribe", "name": "扒谱（鼓轨 → MIDI）", "ok": ok_all("venv", "pip", "adtof"),
         "required": True, "need": ["adtof"],
         "fallback": "", "note": "" if ok_all("adtof") else "缺少转录引擎，无法扒谱"},
        {"key": "separate", "name": "鼓轨分离（更准）", "ok": ok_all("demucs_w"),
         "required": False, "need": ["demucs_w"],
         "fallback": "整曲直接转录（精度略低）",
         "note": "" if ok_all("demucs_w") else "未安装分离模型：仍能扒谱，但直接对整曲转录，镲片/加花更容易错"},
        {"key": "beat", "name": "按真实拍点记谱（Beat This）", "ok": ok_all("beats_w"),
         "required": False, "need": ["beats_w"],
         "fallback": "全曲固定速度（小节容易和音乐对不上）",
         "note": "" if ok_all("beats_w") else "未安装拍点模型：谱面按全曲一个速度记，遇到变速或非 4/4 会明显对不上"},
        {"key": "pdf_pro", "name": "专业排版 PDF", "ok": ok_all("mscore"),
         "required": False, "need": ["mscore"],
         "fallback": "内置渲染 PDF（排版朴素）",
         "note": "" if ok_all("mscore") else "未安装 MuseScore：PDF 改用内置渲染，能看能打印"},
        {"key": "mp3", "name": "MP3 鼓声", "ok": ok_all("ffmpeg"),
         "required": False, "need": ["ffmpeg"],
         "fallback": "WAV 鼓声（体积更大）",
         "note": "" if ok_all("ffmpeg") else "未安装 ffmpeg：鼓声音频会保存为 WAV，体积大一些"},
        {"key": "gpu", "name": "GPU 加速", "ok": bool(status.get("gpu", {}).get("ok")),
         "required": False, "need": ["gpu"],
         "fallback": "CPU 分离（慢约 3 倍）",
         "note": "" if status.get("gpu", {}).get("ok") else "未启用 GPU：用 CPU 分离，功能不受影响但慢约 3 倍"},
    ]


def component_meta():
    """给界面的组件清单（去掉内部字段，补上默认勾选策略）。"""
    out = []
    for c in COMPONENTS:
        out.append({
            "id": c["id"], "name": c["name"], "size": c.get("size", ""),
            "desc": c.get("desc", ""), "feature": c.get("feature", ""),
            "without": c.get("without", ""), "group": c.get("group", "其他"),
            "required": bool(c.get("required")),
            "manual": bool(c.get("manual")),
            "skip_by_default": bool(c.get("skip_by_default")),
        })
    return out



# ---------------- 下载器 ----------------
def _download(urls, out_path, expect=0, max_sec=1800, label=""):
    """多源链 + 断点续传 + 大小校验。成功返回 True。"""
    if _file_ok(out_path, expect):
        _append_log("  已存在 %s (大小校验通过)" % out_path)
        return True
    d = os.path.dirname(out_path)
    if d and not os.path.exists(d):
        os.makedirs(d, exist_ok=True)
    partial = os.path.exists(out_path)
    for url in urls:
        _append_log("  下载: %s" % url)
        try:
            req = urllib.request.Request(url)
            if partial:
                have = os.path.getsize(out_path)
                req.add_header("Range", "bytes=%d-" % have)
            with urllib.request.urlopen(req, timeout=max_sec) as resp:
                total = int(resp.headers.get("Content-Length") or 0)
                mode = "ab" if partial and resp.status == 206 else "wb"
                have0 = os.path.getsize(out_path) if mode == "ab" else 0
                full_total = total + have0
                wrote = have0
                last_mark = -1
                with open(out_path, mode) as f:
                    while True:
                        chunk = resp.read(1 << 16)
                        if not chunk:
                            break
                        f.write(chunk)
                        wrote += len(chunk)
                        if full_total:
                            pct = min(100.0, wrote * 100.0 / full_total)
                            mark = int(pct // 10)
                            if mark > last_mark:
                                _append_log("    %d%% (%d/%d bytes)" % (mark * 10, wrote, full_total))
                                last_mark = mark
            # 服务器不支持 Range 时可能重下了一遍完整文件 -> 以 wb 重试一次
            if partial and _file_ok(out_path, expect) is False and _size_bigger(out_path, expect):
                _append_log("  服务器忽略 Range，重下完整文件")
                partial = False
                for url2 in urls:
                    _append_log("  重下: %s" % url2)
                    with urllib.request.urlopen(urllib.request.Request(url2), timeout=max_sec) as resp:
                        with open(out_path, "wb") as f:
                            while True:
                                chunk = resp.read(1 << 16)
                                if not chunk:
                                    break
                                f.write(chunk)
                    if _file_ok(out_path, expect):
                        break
            if _file_ok(out_path, expect):
                _append_log("  ✓ 下载完成 (%s bytes)" % os.path.getsize(out_path))
                return True
            _append_log("  ✗ 大小校验失败，尝试下一源")
        except Exception as e:
            _append_log("  ✗ 下载失败: %s" % str(e)[:160])
        partial = os.path.exists(out_path)
    return _file_ok(out_path, expect)


def _size_bigger(path, expect):
    if not os.path.exists(path):
        return False
    return os.path.getsize(path) >= expect * 0.5


# ---------------- 安装执行 ----------------
def run_install(root, cfg, components, dry_run=False):
    """后台线程入口。components: 选中的组件 id 列表（按 COMPONENTS 顺序执行）。"""
    def _run():
        total = len(components)
        STATE["running"] = True
        STATE["done"] = False
        STATE["ok"] = True
        STATE["failed"] = []
        STATE["manual"] = []
        STATE["started_at"] = time.time()
        STATE["dry_run"] = dry_run
        _append_log("开始安装（共 %d 项，dry_run=%s）" % (total, dry_run))
        try:
            for i, cid in enumerate(components):
                comp = next(c for c in COMPONENTS if c["id"] == cid)
                _set_step(i, total, comp["name"], comp["desc"])
                # 每轮重新解析：装完 venv 之后 pip/adtof 才能拿到 venv 里的 python，
                # 否则同一批选中「venv + pip」时 pip 会一直报“venv 未就绪”。
                ok = _install_one(cid, root, cfg, _venv_py(root, cfg), dry_run)
                if not ok and cid in ("venv", "pip", "demucs_w", "adtof", "beats_w"):
                    STATE["ok"] = False
                    STATE["failed"].append(cid)
                elif not ok:
                    STATE["manual"].append(cid)
                if dry_run:
                    time.sleep(0.4)
            STATE["done"] = True
            STATE["running"] = False
            STATE["finished_at"] = time.time()
            _append_log("安装结束：%s" % ("全部完成" if not STATE["failed"] and not STATE["manual"]
                                           else "部分未完成（详见结果）"))
        except Exception as e:
            STATE["done"] = True
            STATE["running"] = False
            STATE["ok"] = False
            STATE["finished_at"] = time.time()
            _append_log("安装异常: %s" % str(e)[:300])

    threading.Thread(target=_run, daemon=True).start()


def _install_one(cid, root, cfg, venv_py, dry_run):
    """安装单个组件。返回 True=成功/无需装；False=失败或需手动。"""
    if dry_run:
        _append_log("  [dry-run] %s 安装成功" % cid)
        return True

    if cid == "venv":
        target = cfg.get("venvDir") or os.path.join(root, ".venv")
        if os.path.exists(os.path.join(target, "Scripts", "python.exe")):
            _append_log("  venv 已存在: %s" % target)
            return True
        for launcher in [["py", "-3"], ["python"]]:
            try:
                _append_log("  用 %s 创建虚拟环境 -> %s" % (" ".join(launcher), target))
                r = subprocess.run(launcher + ["-m", "venv", target],
                                   capture_output=True, creationflags=CREATE_NO_WINDOW, text=True, timeout=300)
                if r.returncode == 0 and os.path.exists(os.path.join(target, "Scripts", "python.exe")):
                    vp = os.path.join(target, "Scripts", "python.exe")
                    _append_log("  ✓ venv 创建完成")
                    _pip_install(vp, ["--upgrade", "pip"], timeout=300, what="pip 自升级")
                    return True
                _append_log("  ✗ venv 创建失败: %s" % (r.stderr or "")[:200])
            except Exception as e:
                _append_log("  ✗ %s" % str(e)[:200])
        _append_log("  需要系统已安装 Python 3.9+（python.org 下载）")
        return False

    if cid == "pip":
        if not venv_py:
            _append_log("  venv 未就绪，跳过依赖安装")
            return False
        # 1) adtof 源码（不在 PyPI）
        probe = "import importlib;print(1) if importlib.import_module('adtof_pytorch') else print(0)"
        r = subprocess.run([venv_py, "-c", "import adtof_pytorch"], capture_output=True, creationflags=CREATE_NO_WINDOW, text=True, timeout=30)
        if r.returncode != 0:
            _append_log("  安装 ADTOF 源码包（GitHub 加速链）...")
            zip_p = os.path.join(os.environ.get("TEMP", root), "adtof_src.zip")
            if _download(ADTOF_ZIP_URLS, zip_p, expect=0, max_sec=600, label="ADTOF 源码"):
                ext = os.path.join(os.environ.get("TEMP", root), "adtof_src_extract")
                if os.path.exists(ext):
                    shutil.rmtree(ext, ignore_errors=True)
                import zipfile
                try:
                    with zipfile.ZipFile(zip_p) as zf:
                        zf.extractall(ext)
                    subs = [os.path.join(ext, d) for d in os.listdir(ext)]
                    src = subs[0] if subs else None
                    if src:
                        rr = subprocess.run([venv_py, "-m", "pip", "install", src,
                                             "-i", PYPI_MIRROR, "--timeout", "30"],
                                            capture_output=True, creationflags=CREATE_NO_WINDOW, text=True, timeout=600)
                        if rr.returncode == 0:
                            _append_log("  ✓ ADTOF 源码安装完成")
                        else:
                            _append_log("  ✗ pip 装 ADTOF 失败: %s" % (rr.stderr or "")[:200])
                except Exception as e:
                    _append_log("  ✗ 解压/安装失败: %s" % str(e)[:200])
                finally:
                    if os.path.exists(zip_p):
                        os.remove(zip_p)
                    if os.path.exists(ext):
                        shutil.rmtree(ext, ignore_errors=True)
        else:
            _append_log("  ADTOF 已安装")
        # 2) requirements（过滤 adtof 行）
        req = os.path.join(root, "requirements.txt")
        pkgs = []
        if os.path.exists(req):
            with open(req, "r", encoding="utf-8") as f:
                for line in f:
                    s = line.strip()
                    if s and not s.startswith("#") and "adtof" not in s.lower():
                        pkgs.append(s)
        if pkgs:
            # torch split out: try tsinghua mirror first, fall back to the
            # official CPU-only index (both proven on this machine; tsinghua
            # occasionally drops a Windows wheel version)
            torch_pkgs = [p for p in pkgs if p.lower().startswith("torch")]
            other_pkgs = [p for p in pkgs if not p.lower().startswith("torch")]
            if other_pkgs:
                _append_log("  安装 %d 个依赖包…" % len(other_pkgs))
                if not _pip_install(venv_py, other_pkgs, what="依赖包安装"):
                    return False
            if torch_pkgs:
                _append_log("  安装 torch（CPU 版，约 200MB）…")
                if not _pip_install(venv_py, torch_pkgs, what="torch 安装"):
                    # 再试官方 CPU 专用索引
                    rr = subprocess.run([venv_py, "-m", "pip", "install"] + torch_pkgs +
                                        ["--index-url", "https://download.pytorch.org/whl/cpu",
                                         "--timeout", "60", "--retries", "2"],
                                        capture_output=True, creationflags=CREATE_NO_WINDOW, text=True, timeout=3600)
                    if rr.returncode != 0:
                        _append_log("  ✗ torch 安装失败（末尾日志）: %s" % (rr.stderr or "")[-300:])
                        return False
                    _append_log("  ✓ torch 安装完成（官方 CPU 索引）")
        # 复核
        probe = "import importlib\nmiss=[]\nfor m in %r:\n    try: importlib.import_module(m)\n    except Exception: miss.append(m)\nprint(' '.join(miss))" % (PIP_KEY_IMPORTS,)
        r = subprocess.run([venv_py, "-c", probe], capture_output=True, creationflags=CREATE_NO_WINDOW, text=True, timeout=60)
        miss = (r.stdout or "").strip().split()
        if miss:
            _append_log("  ✗ 仍有缺失: %s" % ",".join(miss))
            return False
        _append_log("  ✓ 依赖全部就绪")
        return True

    if cid == "demucs_w":
        dem_dir = os.path.join(cfg.get("modelsDir") or os.path.join(root, "models"), "demucs_local")
        dem_th = os.path.join(dem_dir, "955717e8-8726e21a.th")
        if _file_ok(dem_th, SIZE_DEMUCS_TH):
            _append_log("  权重已存在")
        else:
            _append_log("  下载 Demucs 权重（约 84MB，官方源）...")
            if not _download(DEMUCS_URLS, dem_th, expect=SIZE_DEMUCS_TH, max_sec=1800):
                return False
        yml = os.path.join(dem_dir, "htdemucs.yaml")
        if not os.path.exists(yml):
            with open(yml, "w", encoding="ascii") as f:
                f.write("models: ['955717e8']\n")
        return True

    if cid == "adtof":
        if not venv_py:
            _append_log("  venv 未就绪，跳过")
            return False
        r = subprocess.run([venv_py, "-c", "import os,adtof_pytorch;print(os.path.dirname(adtof_pytorch.__file__))"],
                           capture_output=True, creationflags=CREATE_NO_WINDOW, text=True, timeout=30)
        pkg = (r.stdout or "").strip().splitlines()
        if not pkg:
            _append_log("  先安装 ADTOF 源码包（见 pip 步骤）")
            return False
        w = os.path.join(pkg[-1], "data", "adtof_frame_rnn_pytorch_weights.pth")
        if _file_ok(w, SIZE_ADTOF_PTH):
            _append_log("  权重已存在")
        else:
            _append_log("  下载 ADTOF 权重（3.6MB，ghproxy 链）...")
            if not _download(ADTOF_W_URLS, w, expect=SIZE_ADTOF_PTH, max_sec=600):
                return False
        return True

    if cid == "beats_w":
        dst = _beats_target(root, cfg)
        got, note = _beats_ckpt(root, cfg)
        if got:
            _append_log("  权重已存在: %s" % got)
            _pin_beats_ckpt(root, cfg)
            return True
        # 残片先清掉：留着的话 torch.hub 会把它当完整缓存直接用
        for p in _beats_ckpt_candidates(root, cfg):
            try:
                if os.path.isfile(p) and os.path.getsize(p) < BEATS_MIN_BYTES:
                    os.remove(p)
                    _append_log("  清掉没下完的残片: %s" % p)
            except Exception:
                pass
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        _append_log("  下载 Beat This 权重（约 77MB，CPJKU 官方源，支持续传）...")
        # Beat This 源（cloud.cp.jku.at）速度不稳定：77MB 在 1800s 内常下不完，
        # 调大到 3600s（与 install.ps1 的 -MaxTimeSec 3600 同口径）；
        # _download 对已有残片带 Range: bytes=N- 续传，重跑接著下。
        if not _download(BEATS_URLS, dst, expect=SIZE_BEATS_CKPT, max_sec=3600):
            # 拿不到精确大小时，退一步只要求"看起来是完整文件"
            try:
                ok_size = os.path.isfile(dst) and os.path.getsize(dst) >= BEATS_MIN_BYTES
            except Exception:
                ok_size = False
            if not ok_size:
                _append_log("  ✗ 权重没下完整，稍后重试这一项即可（已下载部分会续传）")
                return False
        _append_log("  ✓ 拍点权重就绪: %s" % dst)
        return True

    if cid == "ffmpeg":
        ff = _find_ffmpeg(root, cfg)
        if ff:
            _append_log("  ffmpeg 已存在: %s" % ff)
            return True
        gz = os.path.join(root, "tools", "ffmpeg", "ffmpeg.gz")
        _append_log("  下载 ffmpeg（npmmirror 淘宝源，约 80MB）...")
        if not _download(FFMPEG_GZ_URLS, gz, expect=0, max_sec=1200):
            return False
        dst = os.path.join(root, "tools", "ffmpeg", "bin", "ffmpeg.exe")
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        try:
            with open(gz, "rb") as f:
                head = f.read(2)
            if head == b"\x1f\x8b":
                with gzip.open(gz, "rb") as gf, open(dst, "wb") as of:
                    shutil.copyfileobj(gf, of)
            else:
                shutil.copy(gz, dst)
        except Exception as e:
            # 坏包 / 镜像返回 HTML 错误页：只判失败这一个组件，别让整轮安装崩掉
            _append_log("  ffmpeg 解压失败：%s（请重试或手动放 ffmpeg.exe）" % str(e)[:200])
            try:
                if os.path.exists(dst):
                    os.remove(dst)
                if os.path.exists(gz):
                    os.remove(gz)
            except Exception:
                pass
            return False
        if os.path.exists(gz):
            os.remove(gz)
        if _file_ok(dst, SIZE_FFMPEG_EXE):
            _append_log("  ✓ ffmpeg 就绪: %s" % dst)
            cfg["ffmpegPath"] = dst
            _write_config(root, cfg)
            return True
        _append_log("  ✗ ffmpeg 文件校验失败")
        return False

    if cid == "mscore":
        ms = _find_mscore()
        if ms:
            _append_log("  MuseScore 已存在: %s" % ms)
            cfg["museScorePath"] = ms
            return True
        choco = shutil.which("choco")
        if choco:
            _append_log("  用 Chocolatey 安装 MuseScore（需要管理员权限）...")
            try:
                rr = subprocess.run([choco, "install", "musescore", "-y", "--no-progress"],
                                    capture_output=True, creationflags=CREATE_NO_WINDOW, text=True, timeout=1800)
                ms2 = _find_mscore()
                if ms2:
                    cfg["museScorePath"] = ms2
                    _write_config(root, cfg)
                    _append_log("  ✓ MuseScore 安装完成")
                    return True
                _append_log("  ✗ choco 安装未成功，请手动下载安装（musescore.org）")
            except Exception as e:
                _append_log("  ✗ choco 失败: %s" % str(e)[:200])
        else:
            _append_log("  未检测到 choco，请手动下载安装 MuseScore 4（musescore.org）")
        return False

    if cid == "gpu":
        # 通用 GPU 适配：
        #   1) 先探测（不依赖 torch）：有没有 N 卡、算力、显存、驱动版本
        #   2) 按"算力 + 驱动能力"选 torch 的 cuXX 索引（抄 A1111 的 TORCH_INDEX_URL / uv 的 auto 思路）
        #   3) 优先复用用户已有的 CUDA 环境；没有再建独立干净 venv 自己装
        #   4) 装完必须校验 arch_list 含该卡算力 + demucs 冒烟，才算成功
        import gpu_detect as gd
        rep = gd.report()
        pick = rep["pick"]
        if not rep["gpus"]:
            _append_log("  ✗ %s" % pick.get("reason", "没有检测到 NVIDIA 显卡"))
            _append_log("  GPU 组件只对 NVIDIA 显卡有效；现在会继续用 CPU（功能不受影响）")
            return False
        g0 = rep["gpus"][0]
        _append_log("  检测到显卡：%s（显存 %s MB，驱动 %s）"
                    % (g0.get("name"), g0.get("vram_mb") or "?", g0.get("driver") or "?"))
        if not pick.get("ok"):
            _append_log("  ✗ %s" % pick.get("reason", "无法为该显卡选择 CUDA 轮子"))
            return False
        index_url = pick["index_url"]
        _append_log("  选用 PyTorch 索引：%s（%s）" % (index_url, pick.get("reason", "")))

        # ---- (3a) 已有可用 GPU 环境？直接复用，不重复下载 ----
        def _probe_env(py):
            """返回 {ok, torch, name, vram_mb, cc, arch_ok} 或 None。"""
            if not py or not os.path.exists(py):
                return None
            code = (
                "import json\n"
                "r={'ok':False}\n"
                "try:\n"
                "    import torch\n"
                "    r['torch']=torch.__version__\n"
                "    r['cuda']=torch.version.cuda\n"
                "    if torch.cuda.is_available():\n"
                "        p=torch.cuda.get_device_properties(0)\n"
                "        cc='%d.%d'%(p.major,p.minor)\n"
                "        r.update(ok=True,name=p.name,vram_mb=int(p.total_memory/1048576),cc=cc)\n"
                "        archs=list(torch.cuda.get_arch_list())\n"
                "        r['arch_ok']=('sm_'+cc.replace('.','')) in archs or any(a.startswith('compute_'+cc.replace('.','')) for a in archs)\n"
                "        r['archs']=archs\n"
                "except Exception as e:\n"
                "    r['err']=str(e)[:200]\n"
                "print(json.dumps(r))\n"
            )
            try:
                rr = subprocess.run([py, "-c", code], capture_output=True, creationflags=CREATE_NO_WINDOW,
                                    text=True, timeout=240, errors="replace")
                lines = [l for l in (rr.stdout or "").strip().splitlines() if l.strip()]
                import json as _json
                return _json.loads(lines[-1]) if lines else None
            except Exception:
                return None

        genv = cfg.get("gpuVenvDir") or os.path.join(root, ".venv-gpu")
        candidate = cfg.get("gpuPython") or os.path.join(genv, "Scripts", "python.exe")
        info = _probe_env(candidate)
        if info and info.get("ok"):
            _append_log("  复用已有 CUDA 环境：%s（torch %s, %s）"
                        % (candidate, info.get("torch"), info.get("name")))
            py_ok = candidate
        else:
            # ---- (3b) 建独立干净 venv，自己装 CUDA 版 torch ----
            sys_py = _find_system_python(root)
            if not sys_py:
                _append_log("  ✗ 找不到系统 Python（需 3.9+）")
                return False
            if os.path.isdir(genv):
                _append_log("  清理旧的 GPU 环境：%s" % genv)
                shutil.rmtree(genv, ignore_errors=True)
            _append_log("  创建独立 GPU 环境（约 5GB 磁盘）-> %s" % genv)
            try:
                r = subprocess.run([sys_py, "-m", "venv", genv], capture_output=True,
                                   creationflags=CREATE_NO_WINDOW, text=True, timeout=600)
                if r.returncode != 0:
                    _append_log("  ✗ 创建 venv 失败: %s" % (r.stderr or "")[:200])
                    return False
            except Exception as e:
                _append_log("  ✗ 创建 venv 异常: %s" % str(e)[:200])
                return False
            gpy = os.path.join(genv, "Scripts", "python.exe")
            _seed_vc_runtime(os.path.join(genv, "Scripts"), root)
            _pip_install(gpy, ["--upgrade", "pip"], timeout=300, what="pip 自升级")

            # torch + torchaudio 必须从 pytorch 官方索引装（PyPI 上 Windows 只有 CPU 版）
            tries = [index_url]
            for tag in ("cu121", "cu118", "cu126", "cu124", "cu128"):
                u = "https://download.pytorch.org/whl/" + tag
                if u not in tries:
                    tries.append(u)
            installed = False
            for url in tries:
                _append_log("  安装 torch + torchaudio（%s，约 2.4GB，慢的话要等一会儿）…" % url.rsplit("/", 1)[-1])
                cmd = [gpy, "-m", "pip", "install", "torch", "torchaudio",
                       "--index-url", url, "--prefer-binary", "--timeout", "120", "--retries", "2"]
                try:
                    rr = subprocess.run(cmd, capture_output=True, creationflags=CREATE_NO_WINDOW,
                                        text=True, timeout=7200, errors="replace")
                except Exception as e:
                    _append_log("    ✗ %s" % str(e)[:160])
                    continue
                if rr.returncode != 0:
                    _append_log("    …该索引失败，换下一个（%s）" % (rr.stderr or "")[-140:].replace("\n", " "))
                    continue
                info = _probe_env(gpy)
                if info and info.get("ok"):
                    installed = True
                    if not info.get("arch_ok"):
                        _append_log("    …轮子不含 sm_%s（arch_list=%s），换下一个索引"
                                    % (str(info.get("cc", "")).replace(".", ""),
                                       ",".join(info.get("archs", [])[:6])))
                        continue
                    break
                # is_available False：可能装成了 CPU 版或驱动不匹配
                installed = False
                continue
            if not installed:
                _append_log("  ✗ 没装出可用的 CUDA 版 torch（可能是驱动太旧或网络问题）")
                _append_log("    建议：更新 NVIDIA 驱动后重试；或继续用 CPU（功能不受影响）")
                shutil.rmtree(genv, ignore_errors=True)
                return False
            py_ok = gpy
            _append_log("  ✓ CUDA torch 就绪：%s（%s，%s）"
                        % (info.get("torch"), info.get("name"), info.get("cuda") or ""))

        # ---- (4) 装 demucs（--no-deps 防止它把 torch 换回 CPU 版）+ 其依赖 ----
        _append_log("  安装 demucs 及其依赖…")
        ok_demucs = _pip_install(py_ok, ["demucs==4.1.0", "--no-deps"], timeout=1800, what="demucs")
        deps = ["einops", "julius", "lameenc", "openunmix", "dora-search", "submitit",
                "jtype", "treetable", "PyYAML", "tqdm"]
        if ok_demucs:
            _pip_install(py_ok, deps, timeout=1800, what="demucs 依赖")
        chk = subprocess.run([py_ok, "-c", "import demucs, torch; print(torch.cuda.is_available())"],
                             capture_output=True, creationflags=CREATE_NO_WINDOW, text=True,
                             timeout=240, errors="replace")
        if not ok_demucs or chk.returncode != 0 or "True" not in (chk.stdout or ""):
            _append_log("  ✗ demucs 安装/校验失败（%s）" % ((chk.stderr or chk.stdout or "")[:160].replace("\n", " ")))
            return False

        cfg["gpuPython"] = py_ok
        cfg["demucsDevice"] = "auto"
        cfg["gpuSegment"] = gd.best_segment((rep["gpus"][0] or {}).get("vram_mb"))
        _write_config(root, cfg)
        _append_log("  ✓ GPU 加速已就绪：显存 %s MB，分离段长建议 %s 秒"
                    % (rep["gpus"][0].get("vram_mb") or "?", cfg["gpuSegment"]))
        _append_log("    实测 Demucs 分离比 CPU 快约 3 倍；显存不够时会自动回落 CPU")
        return True

    if cid == "sf3":
        sf3 = os.path.join(cfg.get("soundDir") or os.path.join(root, "sound"), "MS_Basic.sf3")
        if _file_ok(sf3, SIZE_SF3):
            _append_log("  音色库已存在")
            return True
        ms = _find_mscore()
        if not ms:
            _append_log("  MuseScore 未装，无法复制音色库（先装 MuseScore）")
            return False
        # ...\MuseScore 4\bin\MuseScore4.exe -> ...\MuseScore 4 -> sound\MS Basic.sf3
        # （三层 dirname 会跑到 C:\Program Files\sound\... 永远找不到）
        src = os.path.join(os.path.dirname(os.path.dirname(ms)), "sound", "MS Basic.sf3")
        if not os.path.exists(src):
            _append_log("  未在 MuseScore 目录找到 MS Basic.sf3")
            return False
        os.makedirs(os.path.dirname(sf3), exist_ok=True)
        shutil.copy(src, sf3)
        if _file_ok(sf3, SIZE_SF3):
            _append_log("  ✓ 音色库已复制: %s" % sf3)
            return True
        return False

    return False


def run_model_download(root, cfg, name, dry_run=False):
    """下载某个 Demucs 分离模型（复用同一套进度/日志通道，界面用 /api/setup/progress 看）。"""
    def _run():
        STATE["running"] = True
        STATE["done"] = False
        STATE["ok"] = True
        STATE["failed"] = []
        STATE["manual"] = []
        STATE["started_at"] = time.time()
        STATE["dry_run"] = dry_run
        _set_step(0, 1, "下载模型 %s" % name)
        _append_log("开始下载分离模型：%s" % name)
        try:
            import models_registry
            ok = models_registry.download_model(root, cfg, name, log=_append_log)
            STATE["ok"] = bool(ok)
            if not ok:
                STATE["failed"].append(name)
        except Exception as e:
            STATE["ok"] = False
            STATE["failed"].append(name)
            _append_log("下载异常：%s" % str(e)[:200])
        finally:
            STATE["done"] = True
            STATE["running"] = False
            STATE["finished_at"] = time.time()
            _append_log("模型下载结束：%s" % ("成功" if STATE["ok"] else "失败"))

    threading.Thread(target=_run, daemon=True).start()


def snapshot():
    """返回前端轮询用的进度快照。"""
    with _lock:
        return {
            "running": STATE["running"],
            "done": STATE["done"],
            "ok": STATE["ok"],
            "step_index": STATE["step_index"],
            "steps": STATE["steps"],
            "current": STATE["current"],
            "failed": list(STATE["failed"]),
            "manual": list(STATE["manual"]),
            "dry_run": STATE["dry_run"],
            "started_at": STATE["started_at"],
            "finished_at": STATE["finished_at"],
            "log_len": STATE["log_seq"],
        }


def get_log_after(seq):
    with _lock:
        return [l for l in STATE["log"] if l["seq"] > seq]


def reset():
    with _lock:
        STATE.update(running=False, done=False, ok=False, step_index=0, steps=[],
                     current="", failed=[], manual=[], started_at=None, finished_at=None,
                     dry_run=False)
