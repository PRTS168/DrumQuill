# -*- coding: utf-8 -*-
"""v2.1.2 尾巴清偿回归（app/app.py，不碰队列部分——队列见 test_app_queue_owner.py）：

① status 一轮里 job.log 只读一次（按 (mtime_ns, size) 键的全文缓存）；
② /api/scorelayout：pymupdf Document with 包裹（旧 P3-9）+ (mtime,size) 结果缓存上限 8（旧 P3#10/43）；
③ reveal/open/_in_data_dirs 改 realpath 白名单 + 目录分隔符前缀（旧 P2#4 + P3-10）；
④ _load_state 对 service_pid 坏值防御（旧 P3#8）。
"""
import json
import os
import subprocess
import sys

import pytest

import app


# ------------------------------------------------ ① job.log 单读缓存

@pytest.fixture
def log_file(tmp_path, monkeypatch):
    p = tmp_path / "job.log"
    monkeypatch.setattr(app, "LOG_FILE", str(p))
    app._log_cache_invalidate()
    yield p
    app._log_cache_invalidate()


def test_log_text_cached_reuses_until_stat_changes(log_file):
    log_file.write_text("v1\n", encoding="utf-8")
    assert app._log_text_cached() == "v1\n"
    st = os.stat(log_file)
    # 同 size + 同 mtime_ns 的改写：按 (路径, mtime_ns, size) 键命中缓存，不重读
    log_file.write_text("v2\n", encoding="utf-8")
    os.utime(log_file, ns=(st.st_mtime_ns, st.st_mtime_ns))
    if os.stat(log_file).st_mtime_ns != st.st_mtime_ns:
        pytest.skip("文件系统不能精确复原 mtime_ns，跳过缓存命中断言")
    assert app._log_text_cached() == "v1\n"
    # mtime 变了才重读
    later = st.st_mtime_ns + 10 ** 9
    os.utime(log_file, ns=(later, later))
    assert app._log_text_cached() == "v2\n"


def test_log_text_missing_file_and_mtime(log_file):
    assert app._log_text_cached() == ""
    assert app._log_mtime_cached() is None
    log_file.write_text("x\n", encoding="utf-8")
    assert app._log_text_cached() == "x\n"
    mt = app._log_mtime_cached()
    assert isinstance(mt, float)
    assert abs(mt - os.path.getmtime(log_file)) < 1.0


def test_write_log_appends_and_invalidates(log_file):
    app.write_log("l1")
    app.write_log("l2")
    assert app._log_text_cached() == "l1\nl2\n"
    assert app.tail_log(1) == "l2\n"


def test_tail_log_last_n_lines(log_file):
    log_file.write_text("".join("l%d\n" % i for i in range(1, 6)), encoding="utf-8")
    assert app.tail_log(2) == "l4\nl5\n"
    assert app.tail_log(99) == "".join("l%d\n" % i for i in range(1, 6))


def test_status_log_consumers_share_one_cache(log_file, monkeypatch):
    """tail_log/_last_device/_detected_bpm/_hints 走同一份缓存文本，输出语义不变。"""
    log_file.write_text(
        "[*] BPM = 148\n[2/6] 分离鼓轨（Demucs · GPU）\n未找到分离模型\n",
        encoding="utf-8")
    monkeypatch.setitem(app.STATE, "running", False)
    monkeypatch.setitem(app.STATE, "error", "boom")
    monkeypatch.setitem(app.STATE, "mode", "full")
    assert app._last_device() == "GPU"
    assert app._detected_bpm() == 148
    hints = app._hints()
    assert hints and "分离模型缺失" in hints[0]["text"]


# ------------------------------------------------ ② scorelayout 句柄 + 结果缓存

@pytest.fixture
def layout_env(tmp_path, monkeypatch):
    out = tmp_path / "output"
    out.mkdir()
    monkeypatch.setattr(app, "out_dir", lambda: str(out))
    app._layout_cache.clear()
    yield out
    app._layout_cache.clear()


def test_scorelayout_caches_by_mtime_and_size(layout_env, monkeypatch):
    calls = {"n": 0}

    def _fake_layout(_path):
        calls["n"] += 1
        return [{"page": 1, "w": 100.0, "h": 80.0, "systems": []}]

    monkeypatch.setattr(app, "_pdf_layout", _fake_layout)
    pdf = layout_env / "a_鼓谱.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    client = app.app.test_client()
    rv1 = client.get("/api/scorelayout?song=a.wav")
    assert rv1.status_code == 200 and rv1.get_json()["ok"] is True
    rv2 = client.get("/api/scorelayout?song=a.wav")
    assert calls["n"] == 1                    # 第二次命中缓存，不再解析
    assert rv2.get_json()["pages"] == rv1.get_json()["pages"]
    m = os.stat(pdf).st_mtime_ns + 10 ** 9
    os.utime(pdf, ns=(m, m))
    rv3 = client.get("/api/scorelayout?song=a.wav")
    assert rv3.status_code == 200
    assert calls["n"] == 2                    # 文件变了 → 重新解析


def test_scorelayout_cache_capped_at_8(layout_env, monkeypatch):
    monkeypatch.setattr(app, "_pdf_layout", lambda _p: [])
    client = app.app.test_client()
    for i in range(12):
        (layout_env / ("s%d_鼓谱.pdf" % i)).write_bytes(b"x")
        assert client.get("/api/scorelayout?song=s%d.wav" % i).status_code == 200
    assert len(app._layout_cache) == 8


def test_pdf_layout_closes_document(monkeypatch):
    """pymupdf.open 必须 with 包裹：函数返回前 Document 一定被关闭（旧 P3-9 句柄泄漏）。"""
    closed = {"n": 0}

    class _FakeDoc:
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            closed["n"] += 1
            return False

        def __iter__(self):
            return iter(())

    class _FakePymupdf:
        @staticmethod
        def open(_path):
            return _FakeDoc()

    monkeypatch.setitem(sys.modules, "pymupdf", _FakePymupdf)
    assert app._pdf_layout("x.pdf") == []
    assert closed["n"] == 1


# ------------------------------------------------ ③ realpath 白名单 + 分隔符前缀

@pytest.fixture
def data_dirs(tmp_path, monkeypatch):
    out = tmp_path / "out"
    for d in (out, tmp_path / "midis", tmp_path / "songs", tmp_path / "stems"):
        d.mkdir()
    monkeypatch.setattr(app, "out_dir", lambda: str(out))
    monkeypatch.setattr(app, "midis_dir", lambda: str(tmp_path / "midis"))
    monkeypatch.setattr(app, "songs_dir", lambda: str(tmp_path / "songs"))
    monkeypatch.setattr(app, "cfg_get",
                        lambda k, d=None: {"stemsDir": str(tmp_path / "stems")}.get(k, d))
    return tmp_path


def test_in_data_dirs_accepts_files_and_root(data_dirs):
    tmp_path = data_dirs
    f = tmp_path / "out" / "a.pdf"
    f.write_bytes(b"x")
    assert app._in_data_dirs(str(f)) is True
    roots = app._data_roots()
    assert app._within_roots(app._real_norm(str(tmp_path / "out")), roots) is True
    assert app._within_roots(app._real_norm(str(tmp_path)), roots) is False


def test_in_data_dirs_rejects_sibling_prefix_dir(data_dirs):
    """旧 P2#4：out-secret 与 out 共享字符串前缀，旧 startswith 判定会误放行。"""
    tmp_path = data_dirs
    evil = tmp_path / "out-secret"
    evil.mkdir()
    f = evil / "a.pdf"
    f.write_bytes(b"x")
    assert app._in_data_dirs(str(f)) is False


def _make_junction(link, target):
    r = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                       capture_output=True)
    return r.returncode == 0


def test_in_data_dirs_resolves_junction_escape(data_dirs):
    """数据目录内的目录联接指向外部：realpath 解析后必须拒绝（旧 P3-10）。"""
    tmp_path = data_dirs
    outside = tmp_path / "outside"
    outside.mkdir()
    link = tmp_path / "out" / "j_escape"
    if not _make_junction(link, outside):
        pytest.skip("无法创建目录联接（需要本地 NTFS）")
    try:
        f = outside / "x.pdf"
        f.write_bytes(b"x")
        assert app._in_data_dirs(str(link / "x.pdf")) is False
    finally:
        os.rmdir(link)


def test_in_data_dirs_resolves_junction_into_data_dir(data_dirs):
    """外部联接指进数据目录：realpath 解析后落在白名单内，照常放行。"""
    tmp_path = data_dirs
    out = tmp_path / "out"
    link = tmp_path / "j_in"
    if not _make_junction(link, out):
        pytest.skip("无法创建目录联接（需要本地 NTFS）")
    try:
        f = out / "y.pdf"
        f.write_bytes(b"x")
        assert app._in_data_dirs(str(link / "y.pdf")) is True
    finally:
        os.rmdir(link)


def test_reveal_route_allows_inside_and_blocks_sibling(data_dirs, monkeypatch):
    tmp_path = data_dirs
    opened = {}

    def _fake_popen(*a, **k):
        opened["args"] = a

    monkeypatch.setattr(app.subprocess, "Popen", _fake_popen)
    f = tmp_path / "out" / "a.pdf"
    f.write_bytes(b"x")
    rv = app.app.test_client().post("/api/reveal", json={"path": str(f)})
    assert rv.status_code == 200 and rv.get_json()["ok"] is True
    assert opened["args"][0][0] == "explorer"
    evil = tmp_path / "out-secret"
    evil.mkdir(exist_ok=True)
    ef = evil / "a.pdf"
    ef.write_bytes(b"x")
    rv2 = app.app.test_client().post("/api/reveal", json={"path": str(ef)})
    assert rv2.status_code == 403


def test_open_route_allows_inside_and_blocks_outside(data_dirs, monkeypatch):
    tmp_path = data_dirs
    started = {}
    monkeypatch.setattr(os, "startfile", lambda p: started.setdefault("p", p),
                        raising=False)
    f = tmp_path / "midis" / "b.mid"
    f.write_bytes(b"x")
    rv = app.app.test_client().post("/api/open", json={"path": str(f)})
    assert rv.status_code == 200
    assert started.get("p") == str(os.path.realpath(str(f)))
    evil_dir = tmp_path / "midis-secret"
    evil_dir.mkdir()
    ef = evil_dir / "c.mid"
    ef.write_bytes(b"x")
    rv2 = app.app.test_client().post("/api/open", json={"path": str(ef)})
    assert rv2.status_code == 403


# ------------------------------------------------ ④ service_pid 坏值防御

@pytest.fixture
def state_file(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "STATE_FILE", str(tmp_path / "task_state.json"))
    return tmp_path


@pytest.mark.parametrize("bad", ["abc", "", {"x": 1}, []])
def test_load_state_tolerates_corrupt_service_pid(state_file, bad):
    """旧 P3#8：int(owner) 未捕获 ValueError/TypeError，一行脏数据能让服务启动即崩。"""
    app.STATE.update(running=False, song=None, pid=None, finished=None, error=None,
                     stage_name="idle")
    with open(app.STATE_FILE, "w", encoding="utf-8") as f:
        json.dump({"running": True, "pid": None, "service_pid": bad,
                   "started_at": "2026-10-26 00:00:00"}, f)
    app._load_state()
    assert app.STATE["running"] is False
    assert app.STATE["stage_name"] == "任务中断"
