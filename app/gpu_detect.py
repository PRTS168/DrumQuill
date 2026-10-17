# -*- coding: utf-8 -*-
"""GPU 探测与 torch 后端选择（不依赖 torch）。

思路参考：
  * kthtda/stablebear 的 gpu.py —— 用 PowerShell / WMIC 在无 CUDA 库时也能认出 N 卡；
  * AUTOMATIC1111 webui 的 launch_utils.py —— 用 TORCH_INDEX_URL 把 torch 指向对应 cuXX 索引，
    并用 `import torch; assert torch.cuda.is_available()` 作硬门槛；
  * uv 的 --torch-backend=auto —— 按驱动能力挑 CUDA 后端。

这里只做"决策"，真正的安装/校验在 installer.py 里。
"""
import json
import os
import re
import subprocess
import sys

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# 驱动版本 -> 该驱动能跑的最高 CUDA 主版本（保守取法，宁低勿高）
# 依据 NVIDIA CUDA 兼容性文档的驱动最低版本要求；低于 11.8 的家用卡直接建议升级驱动。
_DRIVER_MIN = [
    (580, "13.0"),
    (575, "12.9"),
    (570, "12.8"),
    (550, "12.4"),
    (545, "12.3"),
    (535, "12.2"),
    (531, "12.1"),
    (527, "12.0"),
    (520, "11.8"),
]

# 显卡型号 -> 计算能力（遇到 nvidia-smi 不可用、只能拿到型号时兜底用）
_NAME_CC = [
    ("RTX 50", (12, 0)), ("RTX 40", (8, 9)), ("RTX 30", (8, 6)),
    ("RTX 20", (7, 5)), ("GTX 16", (7, 5)), ("MX5", (7, 5)),
    ("GTX 10", (6, 1)), ("GTX 9", (5, 2)), ("TITAN", (3, 7)),
    ("QUADRO P", (6, 1)), ("QUADRO M", (5, 2)), ("QUADRO K", (3, 7)),
    ("V100", (7, 0)), ("A100", (8, 0)), ("A10", (8, 6)), ("A40", (8, 6)),
    ("L4", (8, 9)), ("L40", (8, 9)), ("H100", (9, 0)), ("H200", (9, 0)),
    ("TESLA T4", (7, 5)), ("TESLA P", (6, 1)),
]

# torch 的 CUDA 索引；值是该索引覆盖的最低计算能力（低于它就别选这个索引）
_TORCH_INDEX = [
    ("cu128", (7, 5)),
    ("cu126", (5, 0)),
    ("cu124", (6, 1)),
    ("cu121", (5, 0)),
    ("cu118", (5, 0)),
]
_TORCH_INDEX_BASE = "https://download.pytorch.org/whl/"


def _run(cmd, timeout=15):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           creationflags=CREATE_NO_WINDOW, errors="replace")
        if r.returncode == 0:
            return (r.stdout or "").strip()
    except Exception:
        pass
    return ""


def _name_to_cc(name):
    up = (name or "").upper()
    for key, cc in _NAME_CC:
        if key in up:
            return cc
    return None


def _driver_to_cuda(driver):
    try:
        major = int(str(driver).split(".")[0])
    except Exception:
        return None
    for min_drv, cuda in _DRIVER_MIN:
        if major >= min_drv:
            return cuda
    return None


def detect():
    """返回 N 卡列表：[{name, vram_mb, driver, cc:[maj,min]}]；没有则返回 []。

    优先 nvidia-smi（能拿到显存/驱动/算力），拿不到就退回显卡名称探测
    （PowerShell Get-CimInstance，参考 stablebear 的做法），保证"至少知道有没有 N 卡"。
    """
    gpus = []
    out = _run(["nvidia-smi",
                "--query-gpu=name,memory.total,driver_version,compute_cap",
                "--format=csv,noheader,nounits"])
    if out:
        for line in out.splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) < 3:
                continue
            name = parts[0]
            try:
                vram = int(float(parts[1]))
            except Exception:
                vram = 0
            driver = parts[2]
            cc = None
            if len(parts) >= 4 and re.match(r"^\d+\.\d+$", parts[3]):
                cc = tuple(int(x) for x in parts[3].split("."))
            if cc is None:
                cc = _name_to_cc(name)
            gpus.append({"name": name, "vram_mb": vram, "driver": driver,
                         "cc": list(cc) if cc else None})
        if gpus:
            return gpus

    # 退路：只认得出型号（没有驱动/显存信息）
    out = _run(["powershell", "-NoProfile", "-Command",
                "Get-CimInstance Win32_VideoController | "
                "Where-Object { $_.Name -match 'NVIDIA' } | "
                "Select-Object -ExpandProperty Name"])
    if not out:
        out = _run(["wmic", "path", "win32_videocontroller", "get", "name"])
        out = "\n".join(l.strip() for l in out.splitlines()
                        if "NVIDIA" in l.upper())
    for line in out.splitlines():
        name = line.strip()
        if not name:
            continue
        cc = _name_to_cc(name)
        gpus.append({"name": name, "vram_mb": 0, "driver": "", "cc": list(cc) if cc else None})
    return gpus


def pick_backend(gpus):
    """选 torch 的 CUDA 索引。

    返回 dict: {ok, index_url, tag, feature, reason}
      cc=5.x(Maxwell) 只在 cu121/cu126 里还有 sm_50 目标（本机实测 torch 2.5.1+cu121
      的 arch_list 含 sm_50）；较新卡用更新的索引更划算。
    """
    if not gpus:
        return {"ok": False, "reason": "没有检测到 NVIDIA 显卡（AMD/Intel 显卡目前只能走 CPU）"}
    g = gpus[0]
    cc = tuple(g["cc"]) if g.get("cc") else None
    driver = g.get("driver") or ""

    if cc is None:
        return {"ok": False,
                "reason": "认得显卡型号但没拿到计算能力（nvidia-smi 不可用？请更新 NVIDIA 驱动）",
                "name": g["name"]}
    if cc < (5, 0):
        return {"ok": False, "reason": "显卡太老（计算能力 %d.%d，低于 5.0），没有可用的 torch CUDA 轮子" % cc}

    driver_cuda = _driver_to_cuda(driver) if driver else None

    # 1) 先按算力分档挑候选。Maxwell(5.x)/Pascal(6.x) 只有 cu121/cu118 还带 sm_50/sm_61 目标
    #    （本机实测 torch 2.5.1+cu121 的 arch_list 含 sm_50）；新卡才值得用 cu124+。
    if cc[0] <= 5:
        prefer = ["cu121", "cu118"]
    elif cc[0] == 6:
        prefer = ["cu124", "cu121", "cu118"]
    elif cc[0] == 7:
        prefer = ["cu126", "cu124", "cu121", "cu118"]
    else:
        prefer = ["cu128", "cu126", "cu124", "cu121"]

    # 2) 再按驱动能力裁剪（CUDA 13 的驱动向下兼容 12.x，直接放开）
    note = ""
    if not driver:
        prefer = ["cu121", "cu118"]          # 连驱动版本都拿不到 → 保守
        note = "；未能读取驱动版本，按保守方案选 %s"
    elif driver_cuda is None:
        prefer = ["cu118"]                   # 驱动太老（<11.8 要求）→ 只留最老索引
        note = "；驱动 %s 过旧，建议升级 NVIDIA 驱动后再装 GPU 组件"
    else:
        cap = float(driver_cuda)
        limit = 99.0 if cap >= 13.0 else cap
        prefer = [t for t in prefer if float(t[2:]) / 10.0 <= limit + 1e-9] or ["cu118"]

    for tag in prefer:
        min_cc = dict(_TORCH_INDEX)[tag]
        if cc >= min_cc:
            why = "计算能力 %d.%d / 驱动 %s → %s" % (cc[0], cc[1], driver or "未知", tag)
            if note:
                why += note % (tag if "%s" in note else (driver,))
            return {"ok": True, "index_url": _TORCH_INDEX_BASE + tag, "tag": tag,
                    "feature": "%d.%d" % cc, "driver": driver,
                    "driver_cuda": driver_cuda, "name": g["name"],
                    "vram_mb": g.get("vram_mb") or 0, "reason": why}
    return {"ok": False, "name": g["name"], "cc": list(cc),
            "reason": "计算能力 %d.%d 没有匹配的 torch CUDA 轮子" % cc}


# 显存 -> htdemucs 段长（秒，整数；htdemucs 上限 7.8，所以最大 7）
_SEGMENTS = [(3000, 4), (5000, 5), (8000, 7), (99999, 7)]


def best_segment(vram_mb):
    """按显存给一个稳妥的 Demucs 段长。4GB 卡实测 segment=7 峰值约 1.2GB，够用。"""
    v = int(vram_mb or 0)
    for cap, seg in _SEGMENTS:
        if v < cap:
            return seg
    return 7


def segment_ladder(vram_mb, preferred=None):
    """OOM 时要"越试越小"：preferred -> preferred-2 -> preferred-4（下限 3）。

    注意别把更大的段长排在后面 —— 显存不够时换更大只会更糟。
    """
    first = max(3, min(7, int(preferred or best_segment(vram_mb))))
    ladder = [first]
    s = first
    while s > 3:
        s = max(3, s - 2)
        if s not in ladder:
            ladder.append(s)
    return ladder


def report():
    """一次性给界面/日志用的概要。"""
    gpus = detect()
    pick = pick_backend(gpus)
    return {"gpus": gpus, "pick": pick,
            "segment": best_segment((gpus[0]["vram_mb"] if gpus else 0))}


if __name__ == "__main__":
    print(json.dumps(report(), ensure_ascii=False, indent=2))
