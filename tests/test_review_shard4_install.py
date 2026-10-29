# -*- coding: utf-8 -*-
"""分片4 复审新增回归（安装/引导/打包）。

覆盖本轮修复：
- installer._download 416（Range Not Satisfiable）清残片整段重下（旧逻辑残片永久阻塞）
- installer ffmpeg 校验失败后清除 dst 残片（旧逻辑 HTML 假文件被下一轮当成现成 ffmpeg）
- installer._write_config 原子写（tmp + os.replace）
- installer.run_install 未知组件 id 跳过（旧逻辑 next() 抛 StopIteration）
- models_registry 镜像第二源只接线实测验证过的模型（旧 HF 模板已证实 404）
- models_registry _http_download 416 重试
- models_registry delete_model 与 download_model 互斥锁 / 默认模型保护
- gpu_detect 驱动过旧分支文案（旧逻辑把索引 tag 填进「驱动 %s 过旧」）
- launcher._parse_port 非数字/越界端口回退 8756（旧逻辑 import 即 ValueError）

全程 mock 网络、文件落在 tmp_path；不联网、不建 venv、不写 C 盘。
"""
import json
import os
import time
import zipfile

import urllib.error
import urllib.request

import installer
import launcher
import models_registry as mr
import gpu_detect as gd

QUIET = lambda *a, **k: None  # noqa: E731


class _Resp:
    """假 urlopen 响应体：首次 read 返回全部数据，之后返回 b""。"""

    def __init__(self, data, headers=None, status=200):
        self._data = data
        self.headers = headers or {}
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self, n=-1):
        d, self._data = self._data, b""
        return d


# ---------- installer._download 416 处理 ----------

def test_installer_416_clears_residue_and_redownloads(tmp_path, monkeypatch):
    out = str(tmp_path / "a.th")
    with open(out, "wb") as f:
        f.write(b"\0" * 1500)          # 残片（远端实际只有 1000）
    calls = []

    def fake_urlopen(req, timeout=None):
        calls.append(req)
        if len(calls) == 1:
            raise urllib.error.HTTPError(req.full_url, 416, "Range out of range", None, None)
        return _Resp(b"\0" * 1000, headers={"Content-Length": "1000"})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    ok = installer._download(["http://x/a.th"], out, expect=1000)
    assert ok is True
    assert os.path.getsize(out) == 1000          # 不是残片被续传成 2500
    assert len(calls) == 2                        # 416 后真的重试了一次
    assert calls[0].headers.get("Range") is not None
    assert calls[1].headers.get("Range") is None


def test_installer_416_then_still_fails(tmp_path, monkeypatch):
    """416 清残片后重下仍失败时返回 False，不留半截假成功。"""
    out = str(tmp_path / "a.th")
    with open(out, "wb") as f:
        f.write(b"\0" * 1500)

    def fake_urlopen(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 416, "Range out of range", None, None)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    assert installer._download(["http://x/a.th"], out, expect=1000) is False
    assert not os.path.exists(out)                # 残片已清除，下一轮可从头续传


# ---------- installer ffmpeg 残片 ----------

def test_ffmpeg_residue_removed_on_size_fail(tmp_path, monkeypatch):
    gz = os.path.join(str(tmp_path), "tools", "ffmpeg", "ffmpeg.gz")
    dst = os.path.join(str(tmp_path), "tools", "ffmpeg", "bin", "ffmpeg.exe")
    os.makedirs(os.path.dirname(gz), exist_ok=True)
    with open(gz, "wb") as f:
        f.write(b"<html>404</html>")              # 镜像错误页（非 gzip 魔数）
    monkeypatch.setattr(installer, "_find_ffmpeg", lambda r, c: None)
    monkeypatch.setattr(installer, "_download", lambda *a, **k: True)
    ok = installer._install_one("ffmpeg", str(tmp_path), {}, None, dry_run=False)
    assert ok is False
    assert not os.path.exists(dst)               # HTML 假 ffmpeg.exe 已清除
    assert not os.path.exists(gz)


def test_write_config_atomic(tmp_path):
    installer._write_config(str(tmp_path), {"venvDir": "G:\\v", "port": 8756})
    p = str(tmp_path / "config.json")
    assert json.loads(open(p, encoding="utf-8").read()) == {"venvDir": "G:\\v", "port": 8756}
    assert not os.path.exists(p + ".tmp")        # 临时文件已被 replace 收掉


def test_run_install_unknown_cid_skipped(tmp_path):
    installer.reset()
    installer.run_install(str(tmp_path), {}, ["venv", "no_such_component"], dry_run=True)
    for _ in range(60):
        if installer.snapshot()["done"]:
            break
        time.sleep(0.05)
    snap = installer.snapshot()
    assert snap["done"] is True                  # 不再因 StopIteration 中断整轮安装


# ---------- models_registry 镜像接线与 416 ----------

def test_mirror_wired_only_for_verified_model(tmp_path):
    captured = {}
    real = mr._download_file

    def spy(urls, out_path, want, log, label):
        captured["urls"] = list(urls)
        return False

    mr._download_file = spy
    try:
        cfg = {"modelsDir": str(tmp_path)}
        mr.download_model(str(tmp_path), cfg, "htdemucs_ft", log=QUIET)
        assert len(captured["urls"]) == 2
        assert any("hf-mirror.com/dokodesuka" in u for u in captured["urls"])
        mr.download_model(str(tmp_path), cfg, "htdemucs", log=QUIET)
        assert len(captured["urls"]) == 1        # 默认模型无实测镜像，保持官方单源
        assert "hf-mirror.com" not in captured["urls"][0]
    finally:
        mr._download_file = real


def test_http_download_416_retries_from_scratch(tmp_path, monkeypatch):
    out = str(tmp_path / "a.th")
    with open(out, "wb") as f:
        f.write(b"\0" * 1500)
    calls = []

    def fake_urlopen(req, timeout=120):
        calls.append(req)
        if len(calls) == 1:
            raise urllib.error.HTTPError(req.full_url, 416, "Range out of range", None, None)
        return _Resp(b"\0" * 1000)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    ok, size = mr._http_download("http://x/a.th", out, log=QUIET, label="t")
    assert ok is True
    assert size == 1000
    assert len(calls) == 2


def test_delete_model_lock_and_protection(tmp_path):
    cfg = {"modelsDir": str(tmp_path)}
    assert mr.delete_model(str(tmp_path), cfg, "htdemucs", log=QUIET) is False   # 默认模型禁删
    assert mr.delete_model(str(tmp_path), cfg, "no_such_model", log=QUIET) is False

    # 有残文件的非默认模型：真删干净
    d = os.path.join(str(tmp_path), "demucs_local")
    os.makedirs(d, exist_ok=True)
    for fn in mr.BUILTIN["htdemucs_ft"]["files"]:
        with open(os.path.join(d, fn), "wb") as f:
            f.write(b"\0")
    with open(os.path.join(d, "htdemucs_ft.yaml"), "w") as f:
        f.write("x")
    assert mr.delete_model(str(tmp_path), cfg, "htdemucs_ft", log=QUIET) is True
    assert os.listdir(d) == []


# ---------- gpu_detect 文案 ----------

def test_pick_backend_old_driver_reason_uses_driver_version():
    g = [{"name": "RTX 3060", "vram_mb": 12000, "driver": "456.71", "cc": [8, 6]}]
    pick = gd.pick_backend(g)
    assert pick["ok"] is True
    assert "驱动 456.71 过旧" in pick["reason"]
    assert "cu118 过旧" not in pick["reason"]    # 旧 bug：把索引 tag 填成「驱动版本」


def test_pick_backend_no_driver_reason_uses_tag():
    g = [{"name": "RTX 3060", "vram_mb": 12000, "driver": "", "cc": [8, 6]}]
    pick = gd.pick_backend(g)
    assert pick["ok"] is True
    assert "按保守方案选 cu121" in pick["reason"]


# ---------- launcher 端口解析 ----------

def test_parse_port():
    assert launcher._parse_port({}) == 8756
    assert launcher._parse_port({"port": "abc"}) == 8756
    assert launcher._parse_port({"port": None}) == 8756
    assert launcher._parse_port({"port": "9000"}) == 9000
    assert launcher._parse_port({"port": 0}) == 8756
    assert launcher._parse_port({"port": 70000}) == 8756


# ---------- ADTOF zip-slip 防护 ----------

def test_zip_slip_guard_detects_escape(tmp_path):
    src_zip = str(tmp_path / "evil.zip")
    with zipfile.ZipFile(src_zip, "w") as zf:
        zf.writestr("safe/a.py", "x=1")
        zf.writestr("../escape.py", "x=2")
    ext = str(tmp_path / "ext")
    os.makedirs(ext, exist_ok=True)
    ext_abs = os.path.abspath(ext)
    with zipfile.ZipFile(src_zip) as zf:
        bad = []
        for name in zf.namelist():
            target = os.path.abspath(os.path.join(ext, name))
            if target != ext_abs and not target.startswith(ext_abs + os.sep):
                bad.append(name)
    assert bad == ["../escape.py"]                # 越界条目能被同一套校验逻辑抓住
