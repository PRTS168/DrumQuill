# -*- coding: utf-8 -*-
"""P1-1 修复：任务启动竞态（原子抢占，手动 start 与队列推进共用一道闸，杜绝双开）。"""
import pytest

import app


def _reset_state():
    """把任务槽恢复为空闲（测试间隔离）。"""
    app.STATE["running"] = False
    app.STATE["song"] = None
    app.STATE["error"] = None


def test_acquire_when_idle():
    _reset_state()
    ok = app._try_acquire_job(r"C:\songs\test.wav", 120, "full")
    assert ok is True
    assert app.STATE["running"] is True
    assert app.STATE["song"] == "test.wav"
    assert app.STATE["bpm"] == 120
    assert app.STATE["mode"] == "full"
    assert app.STATE["stage_name"] == "starting"


def test_acquire_rejected_when_running():
    _reset_state()
    assert app._try_acquire_job("a.wav", 100, "full") is True
    # 第二个并发抢占必须被拒（旧代码在此处会双开管线）
    assert app._try_acquire_job("b.wav", 200, "full") is False
    # 第一个任务的字段不被第二个抢占覆盖
    assert app.STATE["song"] == "a.wav"
    assert app.STATE["bpm"] == 100


def test_run_job_returns_false_when_busy(monkeypatch):
    _reset_state()
    assert app._try_acquire_job("a.wav", 100, "full") is True

    called = {"n": 0}

    def _fake_body(*a, **k):
        called["n"] += 1
        return True

    monkeypatch.setattr(app, "_run_job_body", _fake_body)
    # 队列推进在繁忙时返回 False，且不触发执行体
    assert app.run_job("b.wav", 200, "full") is False
    assert called["n"] == 0


def test_run_job_runs_body_when_idle(monkeypatch):
    _reset_state()

    called = {"n": 0}

    def _fake_body(song_path, bpm, mode, device=None):
        called["n"] += 1
        called["device"] = device
        return True

    monkeypatch.setattr(app, "_run_job_body", _fake_body)
    # 空闲时正常开跑、执行体被调用一次；无显式 device 时写 None（不覆盖用户手选）
    assert app.run_job("a.wav", 100, "full") is True
    assert called["n"] == 1
    assert app.STATE["device_override"] is None
    assert called["device"] is None


def test_run_job_releases_slot_when_prep_fails(monkeypatch):
    _reset_state()

    def _boom(_path):
        raise RuntimeError("audio probe failed")

    monkeypatch.setattr(app, "_audio_seconds", _boom)
    # 抢占成功后准备阶段抛错：异常向上传播，且任务槽被释放（不会卡死）
    with pytest.raises(RuntimeError):
        app.run_job("a.wav", 100, "full")
    assert app.STATE["running"] is False
