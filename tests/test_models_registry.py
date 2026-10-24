# -*- coding: utf-8 -*-
"""models_registry.py 测试：模型注册表、阈值与完整性。"""
import os

import models_registry as mr


# ---------- thresholds_for ----------

def test_thresholds_from_config():
    cfg = {"adtofThresholds": "0.1,0.2,0.3,0.4,0.5"}
    assert mr.thresholds_for("htdemucs", cfg) == [0.1, 0.2, 0.3, 0.4, 0.5]

def test_thresholds_reject_invalid_config():
    cfg = {"adtofThresholds": "0.1,0.2,0.3"}          # 数量不对
    assert mr.thresholds_for("htdemucs", cfg) == [0.22, 0.20, 0.40, 0.22, 0.34]
    cfg2 = {"adtofThresholds": "0.1,0.2,0.3,0.4,1.5"}  # 越界
    assert mr.thresholds_for("htdemucs", cfg2) == [0.22, 0.20, 0.40, 0.22, 0.34]

def test_thresholds_model_specific():
    assert mr.thresholds_for("htdemucs_ft") == [0.22, 0.22, 0.36, 0.22, 0.38]
    assert mr.thresholds_for("mdx_extra") == [0.22, 0.20, 0.38, 0.24, 0.48]

def test_thresholds_unknown_model_falls_back():
    assert mr.thresholds_for("no_such_model") == [0.22, 0.20, 0.40, 0.22, 0.34]


# ---------- _model_ok ----------

def test_model_ok_with_complete_files(tmp_path):
    d = tmp_path / "models"
    d.mkdir()
    (d / "htdemucs.yaml").write_text("models: ['955717e8']\n", encoding="utf-8")
    with open(d / "955717e8-8726e21a.th", "wb") as f:
        f.write(b"\0" * (11 * 1024 * 1024))   # > 10MB
    ok, missing = mr._model_ok(str(d), "htdemucs", mr.BUILTIN["htdemucs"])
    assert ok is True
    assert missing == []

def test_model_ok_missing_weights(tmp_path):
    d = tmp_path / "models"
    d.mkdir()
    (d / "htdemucs.yaml").write_text("models: ['955717e8']\n", encoding="utf-8")
    ok, missing = mr._model_ok(str(d), "htdemucs", mr.BUILTIN["htdemucs"])
    assert ok is False
    assert missing == ["955717e8-8726e21a.th"]

def test_model_ok_no_yaml_lists_all(tmp_path):
    d = tmp_path / "models"
    d.mkdir()
    ok, missing = mr._model_ok(str(d), "htdemucs", mr.BUILTIN["htdemucs"])
    assert ok is False
    assert "955717e8-8726e21a.th" in missing


# ---------- _guess_size ----------

def test_guess_size_known_and_unknown():
    assert mr._guess_size("955717e8-8726e21a.th") == 84 * 1024 * 1024
    assert mr._guess_size("unknown-file.th") == 70 * 1024 * 1024


# ---------- models_dir ----------

def test_models_dir_uses_config():
    assert mr.models_dir("ROOT", {"modelsDir": "X"}) == os.path.join("X", "demucs_local")
    assert mr.models_dir("ROOT", {}) == os.path.join("ROOT", "models", "demucs_local")


# ---------- BUILTIN 完整性（防止以后改注册表时漏字段） ----------

def test_builtin_entries_complete():
    required = {"label", "desc", "quality", "files", "yaml", "root",
                "adtof_thresholds", "measured", "best_for"}
    for name, meta in mr.BUILTIN.items():
        missing = required - set(meta)
        assert not missing, "%s 缺字段: %s" % (name, sorted(missing))
        assert len(meta["files"]) >= 1
        assert "models:" in meta["yaml"]
        assert len(meta["adtof_thresholds"]) == 5
        assert all(0.0 < v < 1.0 for v in meta["adtof_thresholds"])

def test_builtin_has_default_and_recommended():
    assert "htdemucs" in mr.BUILTIN
    assert mr.BUILTIN["htdemucs"]["recommended"] is True
