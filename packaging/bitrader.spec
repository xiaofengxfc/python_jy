# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the local backend executable (release shape).

The Electron shell owns the window; this executable is only the Python side:
it serves the API, the market WebSocket and the bundled frontend, then walks
its own process tree down when the shell exits.

``console=True`` is deliberate. Windows windowed builds get ``sys.stdout``
set to ``None``, and the shell learns the OS-assigned port by reading the
``BITRADER_READY {json}`` handshake line. A parent spawning this binary with
``windowsHide: true`` keeps the console window off screen while the pipe
stays usable.

Build with:
    .venv\\Scripts\\python.exe -m PyInstaller packaging\\bitrader.spec --noconfirm
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
# uvicorn picks its loop/protocol implementation dynamically.
hiddenimports += collect_submodules("uvicorn")

datas = collect_data_files("certifi")
datas += collect_data_files("ccxt")
# Frontend assets are served by the backend from disk.
datas += [
    (str(ROOT / "src" / "btrader" / "web" / "static"), "btrader/web/static"),
]
datas += [
    (str(path), "btrader/assets/fonts")
    for path in (ROOT / "src" / "btrader" / "assets" / "fonts").glob("*.ttf")
]

a = Analysis(
    [str(ROOT / "run_backend.py")],
    pathex=[str(ROOT / "src")],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "test", "unittest", "pydoc_data", "PySide6", "pyqtgraph"],
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
    name="biTrader-backend",
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
    name="biTrader-backend",
)
