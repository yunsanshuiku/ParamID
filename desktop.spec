# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = Path(SPEC).resolve().parent
datas = [(str(ROOT / "web"), "web"), (str(ROOT / "examples"), "examples"), (str(ROOT / "docs"), "docs")]
datas += collect_data_files("webview")
hiddenimports = ["webview.platforms.edgechromium", "webview.platforms.winforms"] + collect_submodules("paramid")

a = Analysis([str(ROOT / "desktop.py")], pathex=[str(ROOT)], binaries=[], datas=datas,
             hiddenimports=hiddenimports, hookspath=[], hooksconfig={}, runtime_hooks=[],
             excludes=["tkinter.test", "torch", "torchvision", "torchaudio", "tensorflow", "jax", "pandas", "matplotlib", "IPython", "pytest", "sympy"], noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="ParamID", debug=False,
          bootloader_ignore_signals=False, strip=False, upx=False, console=False, icon=str(ROOT / "installer" / "ParamID.ico"))
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="ParamID")

