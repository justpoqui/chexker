# ============================================================
# PyInstaller build spec for Local Lead Finder
# WHY: a checked-in spec (rather than just a CLI one-liner) means
#      the exact same build config runs identically whether you
#      trigger it locally or via the GitHub Actions workflow at
#      .github/workflows/build-windows-exe.yml — see BUILDING.md.
# ============================================================

# -*- mode: python ; coding: utf-8 -*-

block_cipher = None

a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=[],
    datas=[],
    # tkinter/sqlite3/urllib.robotparser are stdlib but PyInstaller's static
    # analysis doesn't always follow every import path this app uses (e.g.
    # webbrowser's platform-specific submodules); listing them explicitly
    # avoids a working "python main.py" that fails only once packaged.
    hiddenimports=["tkinter", "sqlite3", "urllib.robotparser", "webbrowser"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="LocalLeadFinder",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,  # windowed app -- no console window behind the GUI
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
