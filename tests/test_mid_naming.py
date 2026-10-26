# -*- coding: utf-8 -*-
"""最终 MIDI 的命名兼容回归：拍栅格路线产出 <base>.beats_al.mid（点分隔），
此前结果列表与下载接口只认 <base>_*.mid（下划线），导致默认设置下 UI 拿不到 MIDI。
覆盖：_result_files 与 /api/download?kind=mid 对两种命名的兼容、中间产物排除、最新优先。"""
import os
import time
import urllib.parse

import pytest

import app


@pytest.fixture
def midis(tmp_path, monkeypatch):
    d = tmp_path / "midis"
    d.mkdir()
    monkeypatch.setattr(app, "midis_dir", lambda: str(d))
    out = tmp_path / "out"
    out.mkdir()
    monkeypatch.setattr(app, "out_dir", lambda: str(out))
    return str(d)


def _mk(midis, name, age_seconds=0):
    p = os.path.join(midis, name)
    with open(p, "wb") as f:
        f.write(b"MThd\x00\x00\x00\x06")
    t = time.time() - age_seconds
    os.utime(p, (t, t))
    return p


def test_results_include_dotted_beats_al(midis):
    _mk(midis, "水手.beats_al.mid")
    res = app._result_files("水手")
    mids = [r for r in res if r["kind"] == "mid"]
    assert mids and mids[0]["name"] == "水手.beats_al.mid"


def test_results_include_legacy_underscore_naming(midis):
    _mk(midis, "水手_96.mid")
    res = app._result_files("水手")
    mids = [r for r in res if r["kind"] == "mid"]
    assert mids and mids[0]["name"] == "水手_96.mid"


def test_results_prefer_latest_final(midis):
    _mk(midis, "水手_96.mid", age_seconds=600)
    _mk(midis, "水手.beats_al.mid", age_seconds=0)
    res = app._result_files("水手")
    mids = [r for r in res if r["kind"] == "mid"]
    assert mids and mids[0]["name"] == "水手.beats_al.mid"


def test_results_exclude_intermediate_mids(midis):
    _mk(midis, "水手.mid")
    _mk(midis, "水手.beats.mid")
    _mk(midis, "水手._pp.mid")
    res = app._result_files("水手")
    assert not [r for r in res if r["kind"] == "mid"]


def test_download_kind_mid_serves_dotted_naming(midis, monkeypatch):
    _mk(midis, "水手.beats_al.mid")
    client = app.app.test_client()
    resp = client.get("/api/download?kind=mid&song=" + urllib.parse.quote("水手.mp3"))
    assert resp.status_code == 200
    assert ".beats_al.mid" in resp.headers.get("Content-Disposition", "")


def test_download_kind_mid_404_when_only_intermediate(midis):
    _mk(midis, "水手.mid")
    client = app.app.test_client()
    resp = client.get("/api/download?kind=mid&song=" + urllib.parse.quote("水手.mp3"))
    assert resp.status_code == 404
