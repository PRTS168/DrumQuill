# -*- mode: python ; coding: utf-8 -*-
# DrumQuill 打包配置（onedir）。
#   pyinstaller --noconfirm --clean build_spec\DrumQuill.spec
# 路径全部相对本文件推导，可在任意目录下构建。
import os

from PyInstaller.utils.hooks import collect_all

HERE = os.path.dirname(os.path.abspath(SPEC))          # noqa: F821  (SPEC 由 PyInstaller 注入)
ROOT = os.path.dirname(HERE)
APP = os.path.join(ROOT, "app")
ICON = os.path.join(APP, "drumquill.ico")

datas = []
binaries = []
hiddenimports = ["clr_loader", "webview.platforms.winforms"]
tmp_ret = collect_all("pythonnet")
datas += tmp_ret[0]
binaries += tmp_ret[1]
hiddenimports += tmp_ret[2]

a = Analysis(
    [os.path.join(APP, "launcher.py")],
    pathex=[ROOT],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="DrumQuill",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=[ICON] if os.path.isfile(ICON) else None,
)
# 发行说明：本产物是「exe + loose app\ 源码目录」结构——app.py / installer.py /
# gpu_detect.py / models_registry.py 以源码形式随包分发，不进 bundle；拷贝时必须
# 连同 app\、bootstrap\、scripts\、.venv\、runtime\ 一起分发，单独拷 exe 无法工作。
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    # upx=True 会显著提高杀软误报率（本地工具被 UPX 压缩是常见启发式特征）；
    # 体积换误报不值得，默认关。需要更小体积时手动改回 True。
    upx=False,
    upx_exclude=[],
    name="DrumQuill",
)
