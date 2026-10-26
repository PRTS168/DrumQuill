# -*- coding: utf-8 -*-
"""打磨包回归：job.log 归档、队列自驱动兜底、duration 入队、add 入参清洗、
only_intermediate 清理、device 写入时序。文件全部指向 tmp，不碰工作区真实状态。"""
import time

import pytest

import app


@pytest.fixture
def iso(tmp_path, monkeypatch):
    """队列/状态/日志文件全部指向 tmp，并复位每进程一次的重启恢复标记。"""
    monkeypatch.setattr(app, "QUEUE_FILE", str(tmp_path / "queue.json"))
    monkeypatch.setattr(app, "STATE_FILE", str(tmp_path / "task_state.json"))
    monkeypatch.setattr(app, "_QUEUE_RESET_DONE", False)
    return tmp_path


@pytest.fixture
def client(iso):
    app.STATE["running"] = False
    app.STATE["song"] = None
    return app.app.test_client()


# ------------------------------------------------ job.log 归档

@pytest.fixture
def log_file(tmp_path, monkeypatch):
    app_dir = tmp_path / "appdir"
    app_dir.mkdir()
    monkeypatch.setattr(app, "APP_DIR", str(app_dir))
    lf = app_dir / "job.log"
    monkeypatch.setattr(app, "LOG_FILE", str(lf))   # LOG_FILE 是模块级常量，必须单独打补丁
    return lf


def test_archive_job_log_keeps_last_10(log_file):
    for i in range(12):
        log_file.write_text("任务 #%d 的崩溃现场\n" % i, encoding="utf-8")
        app._archive_job_log()
        log_file.write_text("", encoding="utf-8")  # 模拟清空
    logs = sorted(log_file.parent.glob("logs/job-*.log"))
    assert len(logs) == 10                                    # 只保留最近 10 份
    contents = [p.read_text(encoding="utf-8") for p in logs]
    assert "任务 #11 的崩溃现场" in contents[-1]              # 最新的在
    assert "任务 #0 的崩溃现场" not in "".join(contents)      # 最老的被淘汰


def test_archive_skips_empty_log(log_file):
    log_file.write_text("", encoding="utf-8")
    app._archive_job_log()
    assert not (log_file.parent / "logs").exists()            # 空日志不产生归档


# ------------------------------------------------ 队列自驱动兜底

def test_advance_fallback_called_after_delay(iso, monkeypatch):
    calls = []
    monkeypatch.setattr(app, "_queue_advance_if_idle", lambda: calls.append(1))
    app._schedule_advance_fallback(0.05)
    time.sleep(0.5)
    assert calls == [1]


def test_advance_fallback_swallows_errors(iso, monkeypatch):
    def boom():
        raise RuntimeError("x")
    monkeypatch.setattr(app, "_queue_advance_if_idle", boom)
    app._schedule_advance_fallback(0.05)
    time.sleep(0.5)                                           # 不抛异常即通过


# ------------------------------------------------ duration 写入

def test_mark_queue_state_records_duration(iso, client, songs_dir):
    client.post("/api/queue", json={"action": "add", "songs": ["a.mp3"], "mode": "full"})
    app._mark_queue_state("a.mp3", "done", duration=95)
    q = client.get("/api/queue").get_json()
    item = next(it for it in q["items"] if it["song"] == "a.mp3")
    assert item["state"] == "done" and item["duration"] == 95


# ------------------------------------------------ add 入参清洗

@pytest.fixture
def songs_dir(tmp_path, monkeypatch):
    d = tmp_path / "songs"
    d.mkdir()
    (d / "a.mp3").write_bytes(b"x")
    monkeypatch.setattr(app, "songs_dir", lambda: str(d))
    return d


def test_queue_add_sanitizes_mode_and_bpm(iso, client, songs_dir):
    r = client.post("/api/queue", json={"action": "add", "songs": ["a.mp3"],
                                        "mode": "bogus", "bpm": 99999})
    assert r.status_code == 200
    item = client.get("/api/queue").get_json()["items"][0]
    assert item["mode"] == "full" and item["bpm"] is None     # 非法回退，脏值不入队


def test_queue_add_keeps_valid_mode_and_bpm(iso, client, songs_dir):
    client.post("/api/queue", json={"action": "add", "songs": ["a.mp3"], "mode": "score", "bpm": 96})
    item = client.get("/api/queue").get_json()["items"][0]
    assert item["mode"] == "score" and item["bpm"] == 96


# ------------------------------------------------ only_intermediate 清理

def _fake_artifacts(monkeypatch, tmp_path, files):
    """files: [(kind, 相对名)] → 在 tmp_path 落真实文件 + monkeypatch _artifact_list。"""
    items = []
    for kind, rel in files:
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "wb") as f:
            f.write(b"x")
        items.append({"kind": kind, "name": rel.replace("/", "\\"), "path": str(p), "size": 1})
    monkeypatch.setattr(app, "_artifact_list", lambda base: items)
    return items


def test_delete_only_intermediate_keeps_products(iso, client, tmp_path, monkeypatch):
    _fake_artifacts(monkeypatch, tmp_path, [("pdf", "a_鼓谱.pdf"), ("xml", "a.musicxml"),
                                            ("audio", "a_鼓谱音频.mp3"), ("stem", "htdemucs/a/drums.wav"),
                                            ("midi", "a.beats_al.mid"), ("source", "a.mp3")])
    r = client.post("/api/delete", json={"song": "a.mp3", "keep_source": True,
                                         "only_intermediate": True})
    assert r.status_code == 200
    removed = r.get_json()["removed"]
    # 只删中间产物；最终 MIDI 成品（.beats_al.mid，与下载端点同口径）必须保留
    assert set(removed) == {"htdemucs\\a\\drums.wav"}
    for keep in ("a_鼓谱.pdf", "a.musicxml", "a_鼓谱音频.mp3", "a.beats_al.mid", "a.mp3"):
        assert (tmp_path / keep).exists()                     # 成品与源音频保留


def test_delete_only_intermediate_removes_raw_and_intermediate_midi(iso, client, tmp_path, monkeypatch):
    _fake_artifacts(monkeypatch, tmp_path, [("midi", "a.mid"), ("midi", "a.beats.mid"),
                                            ("midi", "a._pp.mid"), ("stem", "htdemucs/a/drums.wav")])
    r = client.post("/api/delete", json={"song": "a.mp3", "keep_source": True,
                                         "only_intermediate": True})
    assert r.status_code == 200
    assert set(r.get_json()["removed"]) == {"a.mid", "a.beats.mid", "a._pp.mid",
                                            "htdemucs\\a\\drums.wav"}


def test_delete_without_flag_still_removes_all(iso, client, tmp_path, monkeypatch):
    _fake_artifacts(monkeypatch, tmp_path, [("pdf", "a_鼓谱.pdf"), ("stem", "htdemucs/a/drums.wav"),
                                            ("source", "a.mp3")])
    r = client.post("/api/delete", json={"song": "a.mp3", "keep_source": True})
    assert r.status_code == 200
    assert (tmp_path / "htdemucs/a/drums.wav").exists() is False
    assert (tmp_path / "a_鼓谱.pdf").exists() is False
    assert (tmp_path / "a.mp3").exists() is True              # 源音频默认保留


# ------------------------------------------------ device 写入时序

def test_run_job_without_device_keeps_override(iso, tmp_path, monkeypatch):
    app.STATE["device_override"] = "cpu"                      # 用户手动选的
    monkeypatch.setattr(app, "_try_acquire_job", lambda *a, **k: True)
    captured = {}
    monkeypatch.setattr(app, "_run_job_body",
                        lambda *a, **k: captured.setdefault("device", k.get("device")) or True)
    assert app.run_job(r"C:\songs\a.wav", 120, "full", device=None) is True
    assert app.STATE["device_override"] == "cpu"              # 队列项无 device：不覆盖手动选择
    assert captured["device"] is None                         # cmd 构造沿用 STATE


def test_run_job_with_device_writes_override(iso, tmp_path, monkeypatch):
    app.STATE["device_override"] = None
    monkeypatch.setattr(app, "_try_acquire_job", lambda *a, **k: True)
    monkeypatch.setattr(app, "_run_job_body", lambda *a, **k: True)
    assert app.run_job(r"C:\songs\a.wav", 120, "full", device="cuda") is True
    assert app.STATE["device_override"] == "cuda"
