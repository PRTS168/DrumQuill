# -*- coding: utf-8 -*-
"""复审分片 2（前端 UI）回归。

前端是 JS，本测试不启浏览器，做两类验证：

  A. 静态契约：关键修复点确实落到了对应文件里，且旧的缺陷写法不再存在。
  B. 逻辑等价（Python 复刻）：
       1. stripExt 归一后，"刚扒的歌"（菜单传 base，无扩展名）能正确识别为
          st.jobSong（带扩展名），不再被误判成历史歌；不同歌曲仍正确区分。
       2. 时间线契约三处口径：前端 checkAlign 的比较对象 |a.duration-(span+t0)|
          与后端 run_pipeline.ps1 的 |durFinal-(spanR+t0v)| 同口径（off1），
          前端另存 off2=|a.duration-span| 仅作 t0 歧义兜底；阈值差异（0.25 vs 2s）
          已由前端消费后端权威 aligned 标志补齐，不再静默。
"""
import io
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, "app")
MAIN = os.path.join(APP, "static", "app-main.js")
QUEUE = os.path.join(APP, "static", "app-queue.js")
SCORE = os.path.join(APP, "static", "app-score.js")
CSS = os.path.join(APP, "static", "app.css")
PIPELINE = os.path.join(ROOT, "run_pipeline.ps1")
APP_PY = os.path.join(APP, "app.py")


def _read(p):
    with io.open(p, "r", encoding="utf-8") as f:
        return f.read()


# ---------------------------------------------------------------------------
# B.1 stripExt / "当前歌"识别 逻辑等价
# ---------------------------------------------------------------------------
def strip_ext(n):
    return re.sub(r"\.[a-z0-9]+$", "", str(n or ""))


def test_current_song_matches_despite_extension():
    """刚扒的歌：菜单/历史给 base(无扩展名)，st.jobSong 带 .mp3。"""
    job_song = "我的歌.mp3"          # /api/status.song 带扩展名
    opened = "我的歌"                 # 曲目菜单 data-song 去扩展名
    assert strip_ext(opened) == strip_ext(job_song)   # 旧代码直接 != 会判错


def test_different_song_still_distinguished():
    job_song = "A.mp3"
    opened = "B"
    assert strip_ext(opened) != strip_ext(job_song)


def test_history_flag_follows_base_equality():
    """setHist(name !== st.jobSong) 归一后：打开本次结果 → 不显示'回到历史'按钮。"""
    st_job_song = "SongX.wav"
    # 旧：opened base "SongX" !== "SongX.wav" → 误判历史(True)
    old_is_history = ("SongX" != st_job_song)
    new_is_history = (strip_ext("SongX") != strip_ext(st_job_song))
    assert old_is_history is True          # 旧行为确实错
    assert new_is_history is False         # 新行为正确


# ---------------------------------------------------------------------------
# B.2 时间线契约三处口径
# ---------------------------------------------------------------------------
def test_checkalign_compares_same_object_as_backend():
    score = _read(SCORE)
    pipe = _read(PIPELINE)
    app_py = _read(APP_PY)

    # 前端 off1 = |a.duration - (span + t0)|
    assert re.search(r"Math\.abs\(a\.duration\s*-\s*\(st\.span\s*\+\s*t0\)\)", score)
    # 后端 run_pipeline.ps1 aligned 判定：|durFinal - (spanR + t0v)|
    assert re.search(r"Abs\(\$durFinal\s*-\s*\(\$spanR\s*\+\s*\$t0v\)\)", pipe)
    # 后端 /api/scoremap 把 render.aligned / duration 透传出来
    assert '"aligned"' in app_py and '"audio_duration"' in app_py
    # 前端已消费权威 aligned 标志（不再只靠 2s 粗阈值兜底）
    assert "st.aligned === false" in score


# ---------------------------------------------------------------------------
# A. 静态修复点锁定
# ---------------------------------------------------------------------------
def test_queue_ops_report_failures_and_added_count():
    q = _read(QUEUE)
    # 加入队列读取 added，并在 !ok 时提示（不再 fire-and-forget）
    assert "j.added" in q
    assert re.search(r"if\s*\(!j\s*\|\|\s*!j\.ok\)", q)
    # pause/clear/remove 都过了 ok 检查
    assert q.count("if (!j || !j.ok)") >= 4


def test_opensetup_no_infinite_recursion():
    m = _read(MAIN)
    assert "setupLoadTries" in m
    # 失败后带退避与上限，不再 loadSetupStatus(cb 直递归)
    assert "openSetup(manual, resume)" in m
    assert "900" in m and "重试" in m


def test_devswitch_reverts_on_rejection():
    m = _read(MAIN)
    # 不再裸 .catch(function(){}) 吞掉；读响应并回滚
    assert "devSwitch" in m
    assert "var prev = devPref" in m
    assert "设备切换未生效" in m


def test_savesettings_rejects_nan_number():
    m = _read(MAIN)
    assert "badNum" in m and "isFinite(Number(v))" in m


def test_delete_unloads_current_score():
    m = _read(MAIN)
    assert "window.dqHidePlayer" in m
    assert "stripExt(cur) === stripExt(base)" in m


def test_renderdone_skips_drum_swap_when_practicing_other():
    m = _read(MAIN)
    assert "busyOther" in m
    assert "ps.practice || ps.playing" in m


def test_pollgpu_long_tail_retry():
    m = _read(MAIN)
    assert "attempt < 15 ? attempt + 1 : 15" in m


def test_poll_shows_disconnect_banner():
    m = _read(MAIN)
    assert "pollFails" in m
    assert "dqSetDisconnect" in m
    assert "连接已断开" in m


def test_score_consumes_aligned_and_pauses_countin():
    s = _read(SCORE)
    assert "function stripExt(n)" in s
    assert "stripExt(name) === stripExt(st.jobSong)" in s
    assert "st.aligned === false" in s
    # countIn 开头暂停当前播放，避免咔哒叠加
    assert "if (cur && !cur.paused)" in s and "cur.pause()" in s


def test_css_toast_no_duplicate_zindex():
    css = _read(CSS)
    block = re.search(r"\.toast\{(.*?)\}", css, re.S).group(1)
    assert block.count("z-index") == 1
