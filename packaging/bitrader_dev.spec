# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the local test build.

Differences from ``bitrader.spec`` (the release build):

* ``console=True`` — the process keeps a console attached, so a Python
  traceback, a Qt plugin warning or a failed DLL load is visible on screen
  instead of only reaching the log file.
* Its own name and folder — a test build can never overwrite the release
  build in ``dist\\biTrader``.

The output is a folder, not an installer: run ``dist\\biTrader-dev\\biTrader-dev.exe``
directly. Nothing is registered, no shortcuts are written.

Build with:
    .venv\\Scripts\\python.exe -m PyInstaller packaging\\bitrader_dev.spec --noconfirm
"""

from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = Path(SPECPATH).resolve().parent

block_cipher = None

hiddenimports = [
    "keyring.backends.Windows",
    "keyring.backends.SecretService",
    "keyring.backends.kwallet",
]
hiddenimports += collect_submodules("ccxt")
hiddenimports += collect_submodules("btrader.strategy.builtin")

datas = collect_data_files("certifi")
datas += collect_data_files("ccxt")
datas += [(str(ROOT / "packaging" / "bitrader.ico"), "packaging")]
datas += [
    (str(path), "btrader/assets/fonts")
    for path in (ROOT / "src" / "btrader" / "assets" / "fonts").glob("*.ttf")
]

a = Analysis(
    [str(ROOT / "run.py")],
    pathex=[str(ROOT / "src")],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "test", "unittest", "pydoc_data"],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="biTrader-dev",
    # Test builds keep the console: an exception must be readable right where
    # it happened, without opening the error-log window after the fact.
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(ROOT / "packaging" / "bitrader.ico"),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="biTrader-dev",
)
