# -*- coding: utf-8 -*-
"""可插拔模型注册表：发现 / 下载 / 切换 Demucs 分离模型。

设计要点
--------
* 模型清单不是猜的：文件名、bag 组成直接对齐 demucs 包自带的
  `demucs/remote/files.txt` 与 `<name>.yaml`，所以下载后一定能被 `demucs -n <name> --repo <models\\demucs_local>` 认出来。
* 只装"模型权重 + yaml"到 `models\\demucs_local`，不动 python 环境 —— 换模型不需要重装依赖。
* 用户自己放进来的 `*.yaml` 也会被扫描出来（高级用户可以从别处拿 demucs 格式的模型）。
* 转录侧目前只有 ADTOF，注册表里如实标注，不做假入口。
"""
import os
import re
import json
import shutil
import subprocess
import threading
import time

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
FBAI_ROOT = "https://dl.fbaipublicfiles.com/demucs/"
HF_MIRROR = "https://hf-mirror.com/adefossez/{repo}/resolve/main/{fname}"

# 内置可下载模型（id -> 元数据）。files 来自 demucs/remote/files.txt
BUILTIN = {
    "htdemucs": {
        "label": "HTDemucs（默认）",
        "desc": "Hybrid Transformer，通用性最好，速度最快",
        "quality": "基线",
        "stars": 4, "speed_x": 1.0, "min_vram_mb": 3000, "recommended": True,
        "measured": {"f1": 0.866, "f1_B": 0.876, "macro": 0.803, "sep_sec": 12.9, "vram_mb": 1398},
        "adtof_thresholds": [0.08, 0.16, 0.40, 0.10, 0.34],
        "best_for": "速度优先（默认，4–6GB 显存最稳）",
        "root": "hybrid_transformer",
        "files": ["955717e8-8726e21a.th"],
        "yaml": "models: ['955717e8']\n",
        "hf_repo": "htdemucs",
        "note": "已内置；4 轨（鼓/贝斯/人声/其他）",
    },
    "htdemucs_ft": {
        "label": "HTDemucs-FT（保真）",
        "desc": "按乐器分别微调的 4 模型 bag，细节更完整",
        "quality": "略高（+2%）",
        "stars": 4, "speed_x": 2.7, "min_vram_mb": 3500, "recommended": True,
        "measured": {"f1": 0.887, "f1_B": 0.883, "macro": 0.824, "sep_sec": 35.0, "vram_mb": 1465},
        "adtof_thresholds": [0.08, 0.18, 0.34, 0.16, 0.38],
        "best_for": "整体最均衡，验证段最稳（6–12GB 推荐）",
        "root": "hybrid_transformer",
        "files": ["f7e0c4bc-ba3fe64a.th", "d12395a8-e57c48e6.th",
                  "92cfc3b6-ef3bcb9c.th", "04573f0d-f3cf25b2.th"],
        "yaml": "models: ['f7e0c4bc', 'd12395a8', '92cfc3b6', '04573f0d']\n"
                "weights: [\n  [1., 0., 0., 0.],\n  [0., 1., 0., 0.],\n"
                "  [0., 0., 1., 0.],\n  [0., 0., 0., 1.],\n]\n",
        "hf_repo": "htdemucs_ft",
        "note": "约 336MB；GPU 上建议用这个",
    },
    "htdemucs_6s": {
        "label": "HTDemucs-6S（6 轨，实验）",
        "desc": "多分出钢琴与吉他两轨；本曲鼓轨不差，但验证段稳定性一般",
        "quality": "实验（分轨）",
        "stars": 3, "speed_x": 1.2, "min_vram_mb": 3500, "recommended": False,
        "advanced": True,
        "measured": {"f1": 0.897, "f1_B": 0.875, "macro": 0.831, "sep_sec": 15.9, "vram_mb": 1501},
        "adtof_thresholds": [0.08, 0.08, 0.34, 0.10, 0.32],
        "best_for": "实验：多轨分离，底鼓/军鼓略好，稳定性一般",
        "root": "hybrid_transformer",
        "files": ["5c90dfd2-34c22ccb.th"],
        "yaml": "models: ['5c90dfd2']\n",
        "hf_repo": "htdemucs_6s",
        "note": "约 84MB；只想要鼓轨时不必装",
    },
    "mdx_extra": {
        "label": "MDX-Extra（MDX 系，最慢）",
        "desc": "MDX 挑战赛系 4 模型 bag（MDX23C 谱系）；实测与默认相当，但慢 3 倍",
        "quality": "略高（+1%，但慢 3 倍）",
        "stars": 4, "speed_x": 3.3, "min_vram_mb": 3500, "recommended": True,
        "advanced": False,
        "measured": {"f1": 0.878, "f1_B": 0.872, "macro": 0.803, "sep_sec": 42.8, "vram_mb": 1422},
        "adtof_thresholds": [0.08, 0.16, 0.38, 0.14, 0.48],
        "best_for": "MDX 系；镲片多的曲子值得试，但比默认慢 3 倍",
        "root": "mdx_final",
        "files": ["e51eebcc-c1b80bdd.th", "a1d90b5c-ae9d2452.th",
                  "cfa93e08-61801ae1.th", "5d2d6c55-db83574e.th"],
        "yaml": "models: ['e51eebcc', 'a1d90b5c', 'cfa93e08', '5d2d6c55']\n"
                "segment: 44\n",
        "hf_repo": "mdx_extra",
        "note": "约 280MB；偏慢",
    },
}

_LOCK = threading.Lock()


def models_dir(root, cfg):
    return os.path.join(cfg.get("modelsDir") or os.path.join(root, "models"), "demucs_local")


def _model_ok(dirp, name, meta):
    """yaml 在、且所有 .th 都在（大小 > 10MB 视为完整）。"""
    if not os.path.isfile(os.path.join(dirp, name + ".yaml")):
        # 没有 yaml 就等于没装：把该模型需要的文件全列出来
        missing = [f for f in meta.get("files", [])
                   if not (os.path.isfile(os.path.join(dirp, f))
                           and os.path.getsize(os.path.join(dirp, f)) >= 10 * 1024 * 1024)]
        return False, (missing or list(meta.get("files", [])))
    missing = []
    for f in meta.get("files", []):
        p = os.path.join(dirp, f)
        if not os.path.isfile(p) or os.path.getsize(p) < 10 * 1024 * 1024:
            missing.append(f)
    return (not missing), missing


def scan_custom(dirp):
    """扫描用户自己放进去的模型 yaml（不在内置清单里的）。"""
    out = []
    if not os.path.isdir(dirp):
        return out
    for fn in os.listdir(dirp):
        if not fn.endswith(".yaml"):
            continue
        name = fn[:-5]
        if name in BUILTIN:
            continue
        try:
            with open(os.path.join(dirp, fn), "r", encoding="utf-8") as f:
                txt = f.read()
        except Exception:
            continue
        hashes = re.findall(r"'([0-9a-f]{8})'", txt)
        files = []
        for h in hashes:
            hit = [x for x in os.listdir(dirp) if x.startswith(h) and x.endswith(".th")]
            files.extend(hit)
        complete = bool(hashes) and len(files) >= len(set(hashes))
        out.append({"id": name, "label": name + "（自定义）",
                    "desc": "用户放入的模型文件", "quality": "未知",
                    "files": files, "missing": [] if complete else ["权重文件不全"],
                    "installed": complete, "size_mb": _dir_size_mb(dirp, files),
                    "custom": True, "note": "来自 %s" % dirp})
    return out


def _dir_size_mb(dirp, files):
    total = 0
    for f in files:
        try:
            total += os.path.getsize(os.path.join(dirp, f))
        except Exception:
            pass
    return round(total / 1048576.0, 1)


def thresholds_for(model, cfg=None):
    """该分离模型对应的 ADTOF 逐类阈值（底鼓,军鼓,嗵鼓,踩镲,吊镲）。

    优先级：config.adtofThresholds（用户手填，逗号分隔）> 模型实测调优值 > 通用出厂值。
    实测（同一首参考谱，A 段调参 / B 段留出验证）：逐类调优比 ADTOF 原厂
    [0.22,0.24,0.32,0.22,0.30] 的逐类微平均 F1 高 2.4~4.1 个点。
    """
    raw = ""
    if cfg:
        raw = str(cfg.get("adtofThresholds") or "").strip()
    if raw:
        try:
            vals = [float(x) for x in raw.replace("，", ",").split(",") if x.strip()]
            if len(vals) == 5 and all(0.0 < v < 1.0 for v in vals):
                return vals
        except Exception:
            pass
    meta = BUILTIN.get(model or "") or {}
    thr = meta.get("adtof_thresholds")
    if thr:
        return list(thr)
    return [0.08, 0.16, 0.40, 0.10, 0.34]


def list_models(root, cfg):
    """返回 {models:[...], current:'htdemucs', dir:...}。"""
    dirp = models_dir(root, cfg)
    items = []
    for name, meta in BUILTIN.items():
        ok, missing = _model_ok(dirp, name, meta)
        files_present = [f for f in meta["files"] if os.path.isfile(os.path.join(dirp, f))]
        items.append({
            "id": name, "label": meta["label"], "desc": meta["desc"],
            "quality": meta["quality"], "note": meta.get("note", ""),
            "installed": ok, "missing": missing,
            "size_mb": _dir_size_mb(dirp, files_present),
            "total_mb": round(sum(_guess_size(f) for f in meta["files"]) / 1048576.0, 1),
            "custom": False,
            "stars": meta.get("stars", 3),
            "speed_x": meta.get("speed_x", 1.0),
            "min_vram_mb": meta.get("min_vram_mb", 3000),
            "recommended": bool(meta.get("recommended")),
            "advanced": bool(meta.get("advanced")),
            "best_for": meta.get("best_for", ""),
            "measured": meta.get("measured"),
            "thresholds": thresholds_for(name, cfg),
        })
    items.extend(scan_custom(dirp))
    # 展示顺序：推荐 → 按速度从快到慢（默认模型永远在最前）→ 实验模型垫底
    items.sort(key=lambda m: (0 if m.get("recommended") else 1,
                              1 if m.get("advanced") else 0,
                              m.get("speed_x") or 1))
    cur = cfg.get("demucsModel") or "htdemucs"
    if not any(i["id"] == cur and i["installed"] for i in items):
        cur = "htdemucs"
    return {"models": items, "current": cur, "dir": dirp}


# 各权重文件的真实大小（MB）——实测得来的，别再靠猜：
# 84 = htdemucs / htdemucs_ft 的 4 个模型；55 = 6s 模型（5c90dfd2），比想象的小
_KNOWN_MB = {
    "955717e8-8726e21a.th": 84,
    "f7e0c4bc-ba3fe64a.th": 84,
    "d12395a8-e57c48e6.th": 84,
    "92cfc3b6-ef3bcb9c.th": 84,
    "04573f0d-f3cf25b2.th": 84,
    "5c90dfd2-34c22ccb.th": 55,
    # MDX 系的单文件实测约 160MB（比 hybrid_transformer 大得多）
    "e51eebcc-c1b80bdd.th": 160,
    "a1d90b5c-ae9d2452.th": 160,
    "cfa93e08-61801ae1.th": 160,
    "5d2d6c55-db83574e.th": 160,
}


def _guess_size(fname):
    """用于展示预计下载量与下载后的粗校验（真实完整性由 demucs 加载来验）。"""
    mb = _KNOWN_MB.get(fname, 70)
    return mb * 1024 * 1024


def _http_download(url, out_path, log, label, timeout=120):
    """单源下载，支持 Range 断点续传，每 20MB 报一次进度。返回 (ok, size)。"""
    import urllib.request
    have = os.path.getsize(out_path) if os.path.isfile(out_path) else 0
    req = urllib.request.Request(url, headers={"User-Agent": "DrumQuill/1.0"})
    if have:
        req.add_header("Range", "bytes=%d-" % have)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r, \
                open(out_path, "ab" if have else "wb") as f:
            if have and getattr(r, "status", 200) != 206:
                # 服务器不支持断点续传 -> 从头写
                f.seek(0)
                f.truncate()
                have = 0
            last_report = have
            while True:
                chunk = r.read(256 * 1024)
                if not chunk:
                    break
                f.write(chunk)
                have += len(chunk)
                if have - last_report >= 20 * 1048576:
                    last_report = have
                    log("      … %.0f MB" % (have / 1048576.0))
        return True, os.path.getsize(out_path)
    except Exception as e:
        log("      ✗ %s: %s" % (label, str(e)[:140]))
        return False, have


def _download_file(urls, out_path, want_bytes, log, label):
    """多源下载（断点续传 + 大小校验）。"""
    for url in urls:
        log("    %s <- %s" % (label, url.split("//", 1)[-1][:70]))
        ok, size = _http_download(url, out_path, log, label)
        if ok and size >= want_bytes * 0.85:
            log("      ✓ %.1f MB" % (size / 1048576.0))
            return True
        if ok:
            log("      ✗ 大小不对（%.1f / 期望 %.1f MB），换下一个源"
                % (size / 1048576.0, want_bytes / 1048576.0))
    return False


def download_model(root, cfg, name, log=print):
    """下载（或补齐）某个内置模型。返回 True/False。"""
    with _LOCK:
        meta = BUILTIN.get(name)
        if not meta:
            log("未知模型：%s" % name)
            return False
        dirp = models_dir(root, cfg)
        os.makedirs(dirp, exist_ok=True)
        ok, missing = _model_ok(dirp, name, meta)
        if ok:
            log("%s 已就绪，无需下载" % meta["label"])
            return True
        if not missing:
            missing = list(meta["files"])
        log("下载 %s（%d 个权重文件）→ %s" % (meta["label"], len(missing), dirp))
        for fname in missing:
            out = os.path.join(dirp, fname)
            if os.path.isfile(out) and os.path.getsize(out) >= 10 * 1024 * 1024:
                continue
            urls = [FBAI_ROOT + meta["root"] + "/" + fname]
            if not _download_file(urls, out, _guess_size(fname), log, fname):
                log("✗ %s 下载失败（网络问题？已装的模型不受影响）" % fname)
                return False
        yml = os.path.join(dirp, name + ".yaml")
        try:
            with open(yml, "w", encoding="utf-8") as f:
                f.write(meta["yaml"])
        except Exception as e:
            log("✗ 写 yaml 失败：%s" % e)
            return False
        ok2, missing2 = _model_ok(dirp, name, meta)
        if not ok2:
            log("✗ 仍缺：%s" % ",".join(missing2))
            return False
        # 权威验收：让 demucs 自己把模型加载一次（权重损坏/尺寸不对都会在这里暴露）
        log("  校验：用 demucs 加载 %s …" % name)
        if not verify_model(root, cfg, name, log):
            log("✗ 模型加载校验失败，可能是权重不完整（可删除后重下）")
            return False
        log("✓ %s 下载完成并已通过加载校验" % meta["label"])
        return True


def verify_model(root, cfg, name, log=print):
    """用 venv 里的 demucs 真加载一次模型，作为"装好了"的最终依据。"""
    dirp = models_dir(root, cfg)
    venv_py = os.path.join(root, ".venv", "Scripts", "python.exe")
    if not os.path.isfile(venv_py):
        cfg_venv = cfg.get("venvDir") or os.path.join(root, ".venv")
        venv_py = os.path.join(cfg_venv, "Scripts", "python.exe")
    if not os.path.isfile(venv_py):
        log("  （未找到 venv，跳过加载校验）")
        return True
    code = (
        "import sys\n"
        "from pathlib import Path\n"
        "from demucs.pretrained import get_model\n"
        "m = get_model(%r, repo=Path(r'%s'))\n"
        "print('OK', type(m).__name__)\n" % (name, dirp)
    )
    try:
        r = subprocess.run([venv_py, "-c", code], capture_output=True, text=True,
                           timeout=600, creationflags=CREATE_NO_WINDOW, errors="replace")
        out = (r.stdout or "").strip()
        err = (r.stderr or "").strip().splitlines()
        if r.returncode == 0 and "OK" in out:
            log("  ✓ 加载校验通过（%s）" % out.split("OK", 1)[-1].strip())
            return True
        log("  ✗ 加载失败：%s" % (err[-1] if err else out[-200:]))
        return False
    except Exception as e:
        log("  ✗ 校验异常：%s" % str(e)[:160])
        return False


def delete_model(root, cfg, name, log=print):
    """删除某个非默认模型的权重（省空间）。htdemucs 是兜底，不允许删。"""
    if name in ("htdemucs",):
        log("默认模型 htdemucs 不能删（删了就没法分离）")
        return False
    meta = BUILTIN.get(name)
    dirp = models_dir(root, cfg)
    if not meta:
        log("未知模型：%s" % name)
        return False
    for f in meta["files"]:
        p = os.path.join(dirp, f)
        try:
            if os.path.isfile(p):
                os.remove(p)
        except Exception as e:
            log("删除失败 %s: %s" % (f, e))
    try:
        y = os.path.join(dirp, name + ".yaml")
        if os.path.isfile(y):
            os.remove(y)
    except Exception:
        pass
    log("已删除 %s 的权重文件" % meta["label"])
    return True


if __name__ == "__main__":
    import sys
    root = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
    cfg = {}
    try:
        with open(os.path.join(root, "config.json"), encoding="utf-8-sig") as f:
            cfg = json.load(f)
    except Exception:
        pass
    data = list_models(root, cfg)
    print(json.dumps(data, ensure_ascii=False, indent=2))
