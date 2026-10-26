# -*- coding: utf-8 -*-
"""Beat This 权重下载超时 + install.ps1 DEMUCS 链回归。

覆盖本次安装链修复（复审明细-4 任务①②）：
  1) installer.py 的 beats_w 下载超时必须 >=3600s（cloud.cp.jku.at 慢源，
     77MB 在 1800s 内常下不完；与 install.ps1 的 -MaxTimeSec 3600 同口径）；
  2) install.ps1 的 Beat This 下载同样 -MaxTimeSec >=3600；
  3) install.ps1 的 $DEMUCS_URLS 只留官方 dl.fbaipublicfiles.com 主源：
     hf-mirror.com/facebook/htdemucs 已 404（2026-10-26 curl -I 实测
     X-Error-Code: RepoNotFound），不得回到链里；离线指引与失败指引
     都必须引用 $DEMUCS_URLS 本身，避免出现第二份硬编码 URL；
  4) 三个 bootstrap .ps1 保持 UTF-8 BOM（PS5.1 无 BOM 会按 ANSI 解析，
     中文注释变乱码直接碎掉整个脚本）。

不联网：Python 侧用 monkeypatch 捕获 _download 调用参数；PS 侧只做
静态文本断言（解析级检查在回归流程里用 Parser::ParseFile 另行执行）。
"""
import os
import re

import installer as ins

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BOOTSTRAP = os.path.join(ROOT, "bootstrap")
PS_INSTALL = os.path.join(BOOTSTRAP, "install.ps1")

DEAD_HF_MIRROR = "hf-mirror"
OFFICIAL_DEMUCS = "https://dl.fbaipublicfiles.com/demucs/hybrid_transformer/955717e8-8726e21a.th"


def _ps_install_text():
    with open(PS_INSTALL, "rb") as f:
        raw = f.read()
    return raw.decode("utf-8-sig")


# ---------- 1) installer.py：beats_w 下载超时 ----------

def test_beats_w_download_uses_long_timeout(tmp_path, monkeypatch):
    got = {}

    def fake_download(urls, out_path, expect=0, max_sec=1800, label=""):
        got.update(urls=list(urls), out_path=out_path, expect=expect, max_sec=max_sec)
        return True

    monkeypatch.setattr(ins, "_download", fake_download)
    # 替身接管权重探测，避免读到本机 ~/.cache/torch 里的真实缓存/残片，
    # 也避免残片清理循环碰用户目录。
    monkeypatch.setattr(ins, "_beats_ckpt", lambda *a, **k: (None, "未下载"))
    monkeypatch.setattr(ins, "_beats_ckpt_candidates", lambda *a, **k: [])

    assert ins._install_one("beats_w", str(tmp_path), {}, None, dry_run=False) is True
    assert got["max_sec"] >= 3600
    assert got["urls"] == list(ins.BEATS_URLS)
    assert got["expect"] == ins.SIZE_BEATS_CKPT
    assert got["out_path"].endswith(
        os.path.join("torch", "hub", "checkpoints", ins.BEATS_CKPT_NAME))


# ---------- 2/3) install.ps1 静态断言 ----------

def test_ps_install_demucs_chain_official_only():
    text = _ps_install_text()
    m = re.search(r"\$DEMUCS_URLS\s*=\s*@\(([\s\S]*?)\)", text)
    assert m, "install.ps1 找不到 $DEMUCS_URLS 定义"
    chain = m.group(1)
    assert OFFICIAL_DEMUCS in chain, "官方主源 dl.fbaipublicfiles.com 必须保留"
    assert DEAD_HF_MIRROR not in chain, "404 死链 hf-mirror 不得留在 URL 链"
    assert chain.count('"') == 2, "链里只应有官方这一个 URL"


def test_ps_install_manual_guides_reference_demucs_chain():
    text = _ps_install_text()
    # 离线指引与下载失败指引都必须吃 $DEMUCS_URLS（链更新后指引自动同步）
    assert re.search(r"SayOfflineManual -Title \"Demucs[^\r\n]*-Urls \$DEMUCS_URLS", text)
    assert re.search(r"Invoke-DrumDownload -UrlChain \$DEMUCS_URLS", text)


def test_ps_install_beats_download_long_timeout():
    text = _ps_install_text()
    m = re.search(
        r"Invoke-DrumDownload -UrlChain \$BEATS_URLS[\s\S]{0,200}?-MaxTimeSec\s+(\d+)",
        text)
    assert m, "install.ps1 找不到 Beat This 下载调用"
    assert int(m.group(1)) >= 3600


# ---------- 4) bootstrap .ps1 的 UTF-8 BOM ----------

def test_bootstrap_ps1_keep_utf8_bom():
    for name in ("install.ps1", "download.ps1", "ensure.ps1"):
        with open(os.path.join(BOOTSTRAP, name), "rb") as f:
            head = f.read(3)
        assert head == b"\xef\xbb\xbf", "%s 丢了 UTF-8 BOM（前3字节=%r）" % (name, head)
