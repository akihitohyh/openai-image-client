# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path


ROOT = Path(SPECPATH)


a = Analysis(
    [str(ROOT / "web_app.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[(str(ROOT / "web"), "web")],
    # web_app 现在从 image_client_core 导入共享逻辑；PyInstaller 能顺着 import 找到它，
    # 这里显式声明以防将来改成延迟导入后被漏掉。
    hiddenimports=[
        "image_client_core",
        "image_client_core.endpoints",
        "image_client_core.models",
        "image_client_core.responses",
    ],
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
    a.binaries,
    a.datas,
    [],
    name="OpenAIImageClient",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
