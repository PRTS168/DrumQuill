# -*- coding: utf-8 -*-
"""安装链回归测试（复审明细-4 F1/F3/F5/F8 中的 models_registry 部分）。

覆盖：严格字节数校验（_size_ok/_expected_bytes）、多源下载门槛（_download_file）、
download_model 的入口复核 / yaml 登记顺序 / verify_model 失败回滚。
网络下载 _http_download 与 demucs 加载 verify_model 用 monkeypatch 替身，
文件全部落在 pytest tmp_path：不联网、不依赖 venv。
install.ps1 / download.ps1 无测试框架，用 PowerShell Parser 语法检查 + 静态推演覆盖；
launcher.py 的端口探测依赖 Win32/本机端口状态，不在 CI 复现，同样走静态推演。
"""
import os

import models_registry as mr

MODEL = "htdemucs"
META = mr.BUILTIN[MODEL]
QUIET = lambda *a, **k: None  # noqa: E731


# ---------- _size_ok / _expected_bytes（严格字节数校验） ----------

def test_size_ok_accepts_exact_and_small_drift():
    want = 1_000_000
    assert mr._size_ok(want, want)
    assert mr._size_ok(want - int(want * 0.02), want)   # 2% 容差内
    assert mr._size_ok(want + int(want * 0.02), want)

def test_size_ok_rejects_truncated_and_oversized():
    want = 1_000_000
    assert not mr._size_ok(int(want * 0.85), want)      # 旧版 ≥85% 放行的截断文件
    assert not mr._size_ok(int(want * 0.97), want)      # 3% 截断同样拒绝
    assert not mr._size_ok(int(want * 1.05), want)      # 超大也拒绝（双向比对）
    assert not mr._size_ok(0, want)

def test_expected_bytes_covers_all_builtin_files():
    # BUILTIN 引用的每个权重文件都必须有精确定字节数元数据，严格校验才有依据
    for meta in mr.BUILTIN.values():
        for f in meta["files"]:
            assert f in mr._FILE_BYTES, "缺精确定字节数元数据: %s" % f
            assert mr._expected_bytes(f) == mr._FILE_BYTES[f] > 0

def test_expected_bytes_fallback_to_guess():
    assert mr._expected_bytes("no-such-file.th") == mr._guess_size("no-such-file.th")


# ---------- _download_file（多源 + 严格校验门槛） ----------

def test_download_file_accepts_exact(tmp_path, monkeypatch):
    def fake(url, out_path, log=print, label="", timeout=120):
        with open(out_path, "wb") as f:
            f.write(b"\0" * 1000)
        return True, 1000
    monkeypatch.setattr(mr, "_http_download", fake)
    assert mr._download_file(["http://x/1"], str(tmp_path / "a.th"),
                             1000, QUIET, "t") is True

def test_download_file_rejects_truncated_then_next_source_ok(tmp_path, monkeypatch):
    calls = []
    sizes = [850, 1000]  # 源1 截断（旧版 85% 会放行），源2 完整

    def fake(url, out_path, log=print, label="", timeout=120):
        calls.append(url)
        s = sizes[len(calls) - 1]
        with open(out_path, "wb") as f:
            f.write(b"\0" * s)
        return True, s

    monkeypatch.setattr(mr, "_http_download", fake)
    out = str(tmp_path / "a.th")
    assert mr._download_file(["http://x/1", "http://x/2"], out, 1000, QUIET, "t") is True
    assert len(calls) == 2                       # 源1 因字节数不符被拒后换了源
    assert os.path.getsize(out) == 1000

def test_download_file_all_sources_bad_size(tmp_path, monkeypatch):
    def fake(url, out_path, log=print, label="", timeout=120):
        with open(out_path, "wb") as f:
            f.write(b"\0" * 500)
        return True, 500
    monkeypatch.setattr(mr, "_http_download", fake)
    assert mr._download_file(["http://x/1", "http://x/2"], str(tmp_path / "a.th"),
                             1000, QUIET, "t") is False


# ---------- download_model（入口复核 / 登记顺序 / verify 失败回滚） ----------

def _patch_small_meta(monkeypatch, want=1000):
    """把期望字节数换成小值，替身不用真写几十 MB。"""
    monkeypatch.setattr(mr, "_expected_bytes", lambda fname: want)

def _fake_dl_ok(monkeypatch, want=1000):
    """替身下载：按续传语义补齐到 want 字节。"""
    def fake(url, out_path, log=print, label="", timeout=120):
        have = os.path.getsize(out_path) if os.path.isfile(out_path) else 0
        with open(out_path, "ab") as f:
            f.write(b"\0" * (want - have))
        return True, want
    monkeypatch.setattr(mr, "_http_download", fake)

def _models_tmp(tmp_path):
    return {"modelsDir": str(tmp_path)}, tmp_path / "demucs_local"

def test_download_model_unknown_name(tmp_path):
    assert mr.download_model(str(tmp_path), {}, "no_such_model", log=QUIET) is False

def test_download_model_happy_path(tmp_path, monkeypatch):
    cfg, d = _models_tmp(tmp_path)
    _patch_small_meta(monkeypatch)
    _fake_dl_ok(monkeypatch)
    monkeypatch.setattr(mr, "verify_model", lambda *a, **k: True)
    assert mr.download_model(str(tmp_path), cfg, MODEL, log=QUIET) is True
    assert (d / (MODEL + ".yaml")).read_text(encoding="utf-8") == META["yaml"]

def test_download_model_download_fail_no_yaml(tmp_path, monkeypatch):
    cfg, d = _models_tmp(tmp_path)
    _patch_small_meta(monkeypatch)
    monkeypatch.setattr(mr, "_http_download", lambda *a, **k: (False, 0))
    assert mr.download_model(str(tmp_path), cfg, MODEL, log=QUIET) is False
    assert not (d / (MODEL + ".yaml")).exists()   # 下载没成不登记

def test_download_model_verify_fail_rolls_back(tmp_path, monkeypatch):
    cfg, d = _models_tmp(tmp_path)
    _patch_small_meta(monkeypatch)
    _fake_dl_ok(monkeypatch)
    monkeypatch.setattr(mr, "verify_model", lambda *a, **k: False)
    assert mr.download_model(str(tmp_path), cfg, MODEL, log=QUIET) is False
    # 回滚：yaml 与权重全部清除，不残留「已安装」假象
    assert not (d / (MODEL + ".yaml")).exists()
    assert list(d.glob("*.th")) == []
    ok, missing = mr._model_ok(str(d), MODEL, META)
    assert ok is False

def test_download_model_recovers_after_rollback(tmp_path, monkeypatch):
    cfg, d = _models_tmp(tmp_path)
    _patch_small_meta(monkeypatch)
    _fake_dl_ok(monkeypatch)
    monkeypatch.setattr(mr, "verify_model", lambda *a, **k: False)
    assert mr.download_model(str(tmp_path), cfg, MODEL, log=QUIET) is False
    monkeypatch.setattr(mr, "verify_model", lambda *a, **k: True)
    assert mr.download_model(str(tmp_path), cfg, MODEL, log=QUIET) is True

def test_download_model_ready_early_return(tmp_path, monkeypatch):
    cfg, d = _models_tmp(tmp_path)
    _patch_small_meta(monkeypatch)
    d.mkdir(parents=True)
    (d / (MODEL + ".yaml")).write_text(META["yaml"], encoding="utf-8")
    for f in META["files"]:
        with open(d / f, "wb") as fh:
            fh.write(b"\0" * 1000)                # 与替换后的期望字节数一致
    called = []
    monkeypatch.setattr(mr, "_http_download",
                        lambda *a, **k: called.append(1) or (True, 0))
    assert mr.download_model(str(tmp_path), cfg, MODEL, log=QUIET) is True
    assert called == []                            # 完整则不触发任何下载

def test_download_model_redownloads_stale_partial(tmp_path, monkeypatch):
    """≥10MB 的旧截断残片不能被粗检当成完整文件跳过（无 venv 时加载校验也会被跳过）。"""
    cfg, d = _models_tmp(tmp_path)
    want = 12_000_000
    monkeypatch.setattr(mr, "_expected_bytes", lambda fname: want)
    _fake_dl_ok(monkeypatch, want)
    d.mkdir(parents=True)
    fname = META["files"][0]
    with open(d / fname, "wb") as f:
        f.write(b"\0" * 11_000_000)               # ≥10MB：旧逻辑会跳过并直接登记
    monkeypatch.setattr(mr, "verify_model", lambda *a, **k: True)
    assert mr.download_model(str(tmp_path), cfg, MODEL, log=QUIET) is True
    assert os.path.getsize(d / fname) == want     # 残片被续传补齐到期望大小
