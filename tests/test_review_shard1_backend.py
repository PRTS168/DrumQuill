# -*- coding: utf-8 -*-
"""复审分片 1（app 后端）新回归：

  1. 队列跨进程互斥（交接第九节遗留）：msvcrt 字节锁在真正的第二个进程持锁时，
     本进程的队列认领/读-改-写必须让开；锁释放后恢复正常。
  2. /api/start 与 /api/probe 的 ".." 目录穿越收死（basename("..") 会拼出父目录，
     旧代码 exists 为真直通管线参数）。
  3. device 残留继承语义（交接第九节文档化项）：队列任务无显式 device 时，
     eff_device 沿用同一进程内上次手动 start 的覆盖值（行为钉死，防静默回退默认）。

所有文件读写都指向 tmp，不触碰工作区真实 queue.json / task_state.json / job.log。
"""
import io
import json
import os
import subprocess
import sys
import time

import pytest

import app


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


@pytest.fixture
def iso(tmp_path, monkeypatch):
    """队列/状态/锁文件指向 tmp，并复位“每进程一次”的重启恢复标记。"""
    monkeypatch.setattr(app, "QUEUE_FILE", str(tmp_path / "queue.json"))
    monkeypatch.setattr(app, "STATE_FILE", str(tmp_path / "task_state.json"))
    monkeypatch.setattr(app, "QUEUE_XLOCK_FILE", str(tmp_path / ".queue.lock"))
    monkeypatch.setattr(app, "_QUEUE_RESET_DONE", False)
    return tmp_path


def _write_queue(items, paused=False):
    with open(app.QUEUE_FILE, "w", encoding="utf-8") as f:
        json.dump({"items": items, "paused": paused}, f, ensure_ascii=False)


def _read_queue():
    with open(app.QUEUE_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


# --------------------------------------------- ① 跨进程队列互斥

_HOLDER_CODE = (
    "import os, msvcrt, sys, time;"
    "p = sys.argv[1];"
    "fd = os.open(p, os.O_RDWR | os.O_CREAT);"
    "os.lseek(fd, 0, os.SEEK_SET);"
    "msvcrt.locking(fd, msvcrt.LK_NBLCK, 1);"
    "time.sleep(float(sys.argv[2]))"
)


def test_xlock_basic_acquire_release(iso):
    x = app._QueueXLock()
    assert x.acquire() is True
    x.release()


def test_xlock_same_process_second_acquire_blocked(iso):
    """字节区间互斥：同进程第二把锁也拿不到（否则跨进程互斥不成立）。"""
    x1 = app._QueueXLock()
    assert x1.acquire() is True
    x2 = app._QueueXLock()
    assert x2.acquire() is False
    x1.release()


def test_xlock_degrades_when_lock_file_unopenable(monkeypatch):
    """锁文件打不开（目录只读等）时降级放行，绝不让队列停摆。"""
    def _boom(*_a, **_k):
        raise OSError("permission denied")

    monkeypatch.setattr(os, "open", _boom)
    x = app._QueueXLock()
    assert x.acquire() is True


def test_advance_skips_when_other_process_holds_xlock(iso, monkeypatch):
    """真·第二进程持锁：本进程 advance 不得认领 pending 项；锁释放后恢复认领。"""
    _reset_state()
    songs = iso / "songs"
    songs.mkdir()
    (songs / "a.wav").write_bytes(b"x")
    monkeypatch.setattr(app, "songs_dir", lambda: str(songs))
    monkeypatch.setattr(app, "run_job", lambda *a, **k: True)
    _write_queue([{"song": "a.wav", "mode": "full", "bpm": None, "state": "pending"}])

    holder = subprocess.Popen(
        [sys.executable, "-c", _HOLDER_CODE, app.QUEUE_XLOCK_FILE, "30.0"])
    try:
        time.sleep(1.0)   # 等持锁子进程就位
        app._advance_body()
        it = _read_queue()["items"][0]
        assert it["state"] == "pending"          # 让开：未被认领
        assert "owner" not in it
    finally:
        holder.kill()
        holder.wait()

    # 持锁进程被杀（OS 自动回收其锁）后：advance 正常认领
    app._advance_body()
    it = _read_queue()["items"][0]
    assert it["state"] == "running"
    assert it["owner"] == os.getpid()


def test_queue_api_returns_409_when_xlock_contended(iso, monkeypatch):
    """跨进程锁拿不到时，队列写操作返回 409 让用户重试，不静默丢更新。"""
    _reset_state()
    monkeypatch.setattr(app, "_queue_xlock", lambda: None)
    rv = app.app.test_client().post("/api/queue", json={"action": "add", "songs": ["x.wav"]})
    assert rv.status_code == 409


# --------------------------------------------- ② 路径穿越收死

def test_start_rejects_dotdot_path(iso):
    _reset_state()
    rv = app.app.test_client().post("/api/start", json={"song": ".."})
    assert rv.status_code == 404
    assert app.STATE["running"] is False


def test_probe_rejects_dotdot_path(iso):
    rv = app.app.test_client().get("/api/probe", query_string={"song": ".."})
    assert rv.status_code == 404


# --------------------------------------------- ③ device 残留继承语义

def test_queue_job_inherits_residual_device_override(monkeypatch, tmp_path):
    """队列任务无显式 device 时，eff_device 沿用同进程内上次手动 start 的覆盖值
    （文档化的“残留继承”语义；改行为前必须先改这段契约注释）。"""
    _reset_state()
    app.STATE["device_override"] = "gpu"
    monkeypatch.setattr(app, "LOG_FILE", str(tmp_path / "job.log"))
    monkeypatch.setattr(app, "_audio_seconds", lambda p: 10.0)
    monkeypatch.setattr(app, "_archive_job_log", lambda: None)
    monkeypatch.setattr(app, "_save_state", lambda: None)
    monkeypatch.setattr(app, "_record_run", lambda: None)
    monkeypatch.setattr(app, "_mark_queue_state", lambda *a, **k: None)
    monkeypatch.setattr(app, "_schedule_advance_fallback", lambda *a, **k: None)
    captured = {}

    class _FakeProc:
        stdout = io.StringIO("")
        pid = 99999

        def wait(self):
            return 0

    def _fake_popen(cmd, **_k):
        captured["cmd"] = cmd
        return _FakeProc()

    monkeypatch.setattr(app.subprocess, "Popen", _fake_popen)
    assert app.run_job("a.wav", None, "full", device=None) is True
    assert "-Device" in captured["cmd"]
    assert captured["cmd"][captured["cmd"].index("-Device") + 1] == "gpu"
