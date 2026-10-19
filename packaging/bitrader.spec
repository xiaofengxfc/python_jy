# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the Windows desktop build.

Build with:
    .venv\\Scripts\\pyinstaller.exe packaging\\bitrader.spec --noconfirm
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
# The window icon is loaded at runtime from the bundle, not just embedded in
# the executable's resource section.
datas += [(str(ROOT / "packaging" / "bitrader.ico"), "packaging")]
# Bundled UI font (Inter) so every machine renders the interface identically.
datas += [
    (str(path), "btrader/assets/fonts")
    for path in (ROOT / "src" / "btrader" / "assets" / "fonts").glob("*.ttf")
]

a = Analysis(
    # `run.py` is the entry point: it adds src/ to sys.path and imports the
    # package absolutely, so frozen builds have a real package context.
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
    name="biTrader",
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
    name="biTrader",
)
