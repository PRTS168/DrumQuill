# -*- coding: utf-8 -*-
"""任务/队列缺陷回归（P1 取消即复活 / P1 pid=None 启动窗口 / P2 拍点产物 / P3 状态落盘 / P2 history 聚合）。

对应 E2E 实测缺陷：14:29:22 cancel taskkill 后同一秒队列重启同一任务。
所有文件读写都指向 tmp，不触碰工作区真实 queue.json / task_state.json。
"""
import json
import os
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
    """队列/状态文件指向 tmp，并复位“每进程一次”的重启恢复标记。"""
    monkeypatch.setattr(app, "QUEUE_FILE", str(tmp_path / "queue.json"))
    monkeypatch.setattr(app, "STATE_FILE", str(tmp_path / "task_state.json"))
    monkeypatch.setattr(app, "_QUEUE_RESET_DONE", False)
    return tmp_path


def _write_queue(items, paused=False):
    with open(app.QUEUE_FILE, "w", encoding="utf-8") as f:
        json.dump({"items": items, "paused": paused}, f, ensure_ascii=False)


def _read_queue():
    with open(app.QUEUE_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def _acquire_running(song="a.wav"):
    """不经过 Popen，直接把任务槽置为“刚启动、pid 还没出来”的窗口态。"""
    assert app._try_acquire_job(song, None, "full") is True
    assert app.STATE["pid"] is None


# ---------------------------------------------------------------- ① 取消即复活

def test_queue_list_resets_running_only_once(iso):
    """重启恢复只在每进程首次读取执行一次：之后的 running 项是真实任务，不得复活。"""
    _reset_state()
    _write_queue([{"song": "a.wav", "state": "running"}])
    # 第一次读取：模拟服务重启后遗留的 running 项 → 退回 pending 并落盘
    q = app._queue_list()
    assert q["items"][0]["state"] == "pending"
    assert _read_queue()["items"][0]["state"] == "pending"
    # 第二次写入 running 再读：不得再重置（旧代码在此把刚取消的任务复活成 pending）
    _write_queue([{"song": "a.wav", "state": "running"}])
    q2 = app._queue_list()
    assert q2["items"][0]["state"] == "running"
    assert _read_queue()["items"][0]["state"] == "running"


def test_queue_list_keeps_running_item_while_job_alive(iso):
    _reset_state()
    app.STATE["running"] = True
    _write_queue([{"song": "a.wav", "state": "running"}])
    assert app._queue_list()["items"][0]["state"] == "running"


def test_mark_queue_state_marks_running_item(iso):
    """加锁后的读-改-写路径行为不变。"""
    _reset_state()
    _write_queue([{"song": "a.wav", "state": "running"}])
    app._mark_queue_state("a.wav", "failed")
    assert _read_queue()["items"][0]["state"] == "failed"


def test_advance_body_falls_back_to_pending_when_acquire_fails(iso, monkeypatch):
    _reset_state()
    songs = iso / "songs"
    songs.mkdir()
    (songs / "a.wav").write_bytes(b"x")
    monkeypatch.setattr(app, "songs_dir", lambda: str(songs))
    monkeypatch.setattr(app, "run_job", lambda *a, **k: False)  # 手动 start 抢先
    app.STATE["device_override"] = "gpu"  # 证明 device 更新确实执行过
    _write_queue([{"song": "a.wav", "mode": "full", "bpm": None, "state": "pending"}])
    app._advance_body()
    assert _read_queue()["items"][0]["state"] == "pending"
    assert app.STATE["device_override"] is None


def test_advance_body_marks_missing_song(iso, monkeypatch):
    _reset_state()
    monkeypatch.setattr(app, "songs_dir", lambda: str(iso / "nope"))
    _write_queue([{"song": "ghost.wav", "mode": "full", "bpm": None, "state": "pending"}])
    app._advance_body()
    assert _read_queue()["items"][0]["state"] == "missing"


def test_advance_body_respects_paused(iso):
    _reset_state()
    _write_queue([{"song": "a.wav", "mode": "full", "bpm": None, "state": "pending"}],
                 paused=True)
    app._advance_body()
    assert _read_queue()["items"][0]["state"] == "pending"


# ------------------------------------------------------- ② pid=None 启动窗口

def test_cancel_returns_409_during_startup_window(iso):
    """pid 还没出来且刚启动：cancel 必须 409 且绝不改状态（旧代码在此误杀→双开）。"""
    _reset_state()
    _acquire_running()
    rv = app.app.test_client().post("/api/cancel")
    assert rv.status_code == 409
    assert "任务正在启动" in rv.get_json()["error"]
    assert app.STATE["running"] is True
    assert app.STATE["stage_name"] == "starting"
    assert app.STATE["finished"] is None
    assert app.STATE["error"] is None


def test_cancel_flips_zombie_after_startup_grace(iso):
    """宽限已过且 pid 仍为 None：按僵尸处理，翻转状态（保持原有的自愈能力）。"""
    _reset_state()
    assert app._try_acquire_job("a.wav", None, "full") is True
    old = time.time() - app._STARTUP_GRACE_SEC - 5
    app.STATE["started_at"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(old))
    rv = app.app.test_client().post("/api/cancel")
    assert rv.status_code == 400
    assert app.STATE["running"] is False
    assert app.STATE["stage_name"] == "任务中断"


def test_status_guard_respects_startup_window(iso, monkeypatch):
    _reset_state()
    _acquire_running()
    advanced = {"n": 0}
    monkeypatch.setattr(app, "_queue_advance_if_idle",
                        lambda: advanced.__setitem__("n", advanced["n"] + 1))
    rv = app.app.test_client().get("/api/status")
    assert rv.status_code == 200
    # 宽限内不得判死，队列照常推进
    assert app.STATE["running"] is True
    assert app.STATE["stage_name"] == "starting"
    assert advanced["n"] == 1


def test_status_guard_flips_after_grace_and_skips_advance(iso, monkeypatch):
    """宽限外才翻转；翻转的那次请求跳过队列推进、并把队列项标 failed。"""
    _reset_state()
    assert app._try_acquire_job("a.wav", None, "full") is True
    old = time.time() - app._STARTUP_GRACE_SEC - 5
    app.STATE["started_at"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(old))
    _write_queue([{"song": "a.wav", "mode": "full", "bpm": None, "state": "running"}])
    advanced = {"n": 0}
    monkeypatch.setattr(app, "_queue_advance_if_idle",
                        lambda: advanced.__setitem__("n", advanced["n"] + 1))
    marked = {}
    real_mark = app._mark_queue_state

    def _spy(song, state):
        marked["args"] = (song, state)
        real_mark(song, state)

    monkeypatch.setattr(app, "_mark_queue_state", _spy)
    rv = app.app.test_client().get("/api/status")
    assert rv.status_code == 200
    assert app.STATE["running"] is False
    assert app.STATE["stage_name"] == "任务中断"
    assert marked["args"] == ("a.wav", "failed")
    assert _read_queue()["items"][0]["state"] == "failed"
    # 判死的这次请求不得立刻推进队列（防止收尾瞬间重拉）
    assert advanced["n"] == 0


# --------------------------------------------- ③ _artifact_list 补拍点文件

def _patch_song_dirs(monkeypatch, iso):
    out = iso / "output"
    midis = iso / "midis"
    songs = iso / "songs"
    for d in (out, midis, songs):
        d.mkdir(exist_ok=True)
    monkeypatch.setattr(app, "out_dir", lambda: str(out))
    monkeypatch.setattr(app, "midis_dir", lambda: str(midis))
    monkeypatch.setattr(app, "songs_dir", lambda: str(songs))
    monkeypatch.setattr(app, "_stem_dirs", lambda base: [str(iso / "stems")])
    return out, midis, songs


def test_artifact_list_includes_beats_files(iso, monkeypatch):
    _reset_state()
    _, midis, _ = _patch_song_dirs(monkeypatch, iso)
    (midis / "a.beats.json").write_text("{}", encoding="utf-8")
    (midis / "a.beats_plan.json").write_text("{}", encoding="utf-8")
    names = {it["name"] for it in app._artifact_list("a")}
    assert "a.beats.json" in names
    assert "a.beats_plan.json" in names


def test_delete_all_removes_beats_files(iso, monkeypatch):
    """删除全部产物时拍点文件一并清掉（beat_source 不再误报）。"""
    _reset_state()
    _, midis, _ = _patch_song_dirs(monkeypatch, iso)
    beats = midis / "a.beats.json"
    plan = midis / "a.beats_plan.json"
    beats.write_text("{}", encoding="utf-8")
    plan.write_text("{}", encoding="utf-8")
    rv = app.app.test_client().post("/api/delete", json={"song": "a.wav"})
    assert rv.status_code == 200
    body = rv.get_json()
    assert "a.beats.json" in body["removed"]
    assert "a.beats_plan.json" in body["removed"]
    assert not beats.exists()
    assert not plan.exists()


# ------------------------------------------------------------- ④ 失败落盘

def test_run_job_prep_failure_saves_state(iso, monkeypatch):
    _reset_state()
    state_file = iso / "task_state.json"

    def _boom(_p):
        raise RuntimeError("audio probe failed")

    monkeypatch.setattr(app, "_audio_seconds", _boom)
    # run_job 内部自己抢占任务槽，随后执行体在准备阶段抛错
    with pytest.raises(RuntimeError):
        app.run_job("a.wav", 100, "full")
    assert app.STATE["running"] is False
    d = json.loads(state_file.read_text(encoding="utf-8"))
    assert d["running"] is False
    assert "audio probe failed" in (d["error"] or "")


# --------------------------------------------------------- ⑤ history 聚合

def test_history_prefers_pdf_over_stem_row(iso, monkeypatch):
    """后跑 stems 让 _鼓声.mp3 更新，但同一 base 必须保留 _鼓谱.pdf 行。"""
    out, _, _ = _patch_song_dirs(monkeypatch, iso)
    pdf = out / "a_鼓谱.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    (out / "a_鼓声.mp3").write_bytes(b"x")
    old = time.time() - 3600
    os.utime(pdf, (old, old))  # PDF 故意更旧
    rv = app.app.test_client().get("/api/history")
    assert rv.status_code == 200
    rows = rv.get_json()
    assert len(rows) == 1
    assert rows[0]["name"] == "a_鼓谱.pdf"
    assert rows[0]["base"] == "a"


def test_history_keeps_stem_row_when_no_pdf(iso, monkeypatch):
    out, _, _ = _patch_song_dirs(monkeypatch, iso)
    (out / "b_鼓声.wav").write_bytes(b"x")
    rows = app.app.test_client().get("/api/history").get_json()
    assert [r["name"] for r in rows] == ["b_鼓声.wav"]
