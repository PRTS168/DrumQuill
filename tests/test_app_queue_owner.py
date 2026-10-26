# -*- coding: utf-8 -*-
"""队列跨实例互斥（P2-5）：_advance_body 认领写 owner；重启恢复只回退 owner 已死的 running 项。

双服务实例并存（8756/8757）是设计内场景：A 实例认领的 running 项，
B 实例的重启恢复绝不能把它退回 pending，否则双方各开一条管线互踩产物。
所有文件读写都指向 tmp，不触碰工作区真实 queue.json / task_state.json。
"""
import json
import os
import subprocess
import sys

import pytest

import app


@pytest.fixture
def iso(tmp_path, monkeypatch):
    """队列/状态文件指向 tmp，并复位“每进程一次”的重启恢复标记。"""
    monkeypatch.setattr(app, "QUEUE_FILE", str(tmp_path / "queue.json"))
    monkeypatch.setattr(app, "STATE_FILE", str(tmp_path / "task_state.json"))
    monkeypatch.setattr(app, "_QUEUE_RESET_DONE", False)
    return tmp_path


def _reset_state():
    """把任务槽恢复为空闲（测试间隔离）。"""
    app.STATE["running"] = False
    app.STATE["song"] = None
    app.STATE["pid"] = None
    app.STATE["started_at"] = None
    app.STATE["finished"] = None
    app.STATE["error"] = None
    app.STATE["stage_name"] = "idle"
    app.STATE["device_override"] = None


def _write_queue(items, paused=False):
    with open(app.QUEUE_FILE, "w", encoding="utf-8") as f:
        json.dump({"items": items, "paused": paused}, f, ensure_ascii=False)


def _read_queue():
    with open(app.QUEUE_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def _dead_pid():
    """起一个立即退出的子进程，拿到一个确定已死的 pid。"""
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


def test_advance_body_stamps_owner_on_claim(iso, monkeypatch):
    _reset_state()
    songs = iso / "songs"
    songs.mkdir()
    (songs / "a.wav").write_bytes(b"x")
    monkeypatch.setattr(app, "songs_dir", lambda: str(songs))
    monkeypatch.setattr(app, "run_job", lambda *a, **k: True)
    _write_queue([{"song": "a.wav", "mode": "full", "bpm": None, "state": "pending"}])
    app._advance_body()
    it = _read_queue()["items"][0]
    assert it["state"] == "running"
    assert it["owner"] == os.getpid()   # 认领时写入 owner=service_pid


def test_queue_list_resets_item_with_dead_owner(iso):
    """owner 进程已死的 running 项：重启恢复照旧回退 pending 并落盘。"""
    _reset_state()
    dead = _dead_pid()
    assert app._pid_alive(dead) is False
    _write_queue([{"song": "a.wav", "state": "running", "owner": dead}])
    assert app._queue_list()["items"][0]["state"] == "pending"
    assert _read_queue()["items"][0]["state"] == "pending"
    assert "owner" not in _read_queue()["items"][0]


def test_queue_list_keeps_item_with_live_owner(iso):
    """存活实例（owner 进程活着）的 running 项绝不能被重置（P2-5 双开根源）。"""
    _reset_state()
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        assert app._pid_alive(p.pid) is True
        _write_queue([{"song": "a.wav", "state": "running", "owner": p.pid}])
        assert app._queue_list()["items"][0]["state"] == "running"
        assert _read_queue()["items"][0]["state"] == "running"
        assert _read_queue()["items"][0]["owner"] == p.pid
    finally:
        p.kill()
        p.wait()


def test_advance_body_fallback_leaves_foreign_running_item(iso, monkeypatch):
    """抢占失败回退时只动自己认领的项：已被别的实例覆盖认领（owner 非本进程）就不能退。"""
    _reset_state()
    songs = iso / "songs"
    songs.mkdir()
    (songs / "a.wav").write_bytes(b"x")
    monkeypatch.setattr(app, "songs_dir", lambda: str(songs))
    foreign_pid = os.getpid() + 12345

    def _fake_run_job(*a, **k):
        q = _read_queue()
        q["items"][0]["owner"] = foreign_pid   # 模拟另一实例在我认领后覆盖写入
        _write_queue(q["items"])
        return False

    monkeypatch.setattr(app, "run_job", _fake_run_job)
    _write_queue([{"song": "a.wav", "mode": "full", "bpm": None, "state": "pending"}])
    app._advance_body()
    it = _read_queue()["items"][0]
    assert it["state"] == "running"
    assert it["owner"] == foreign_pid


def test_queue_list_still_resets_legacy_item_without_owner(iso):
    """旧格式（无 owner）的遗留 running 项：视为无主，重启恢复照旧回退。"""
    _reset_state()
    _write_queue([{"song": "a.wav", "state": "running"}])
    assert app._queue_list()["items"][0]["state"] == "pending"
