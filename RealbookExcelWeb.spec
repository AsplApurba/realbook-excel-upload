# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the Flask web wrapper. Run:

    pyinstaller RealbookExcelWeb.spec

The resulting binary at dist/RealbookExcelWeb starts the server on
http://0.0.0.0:8000 — same as `python3 app.py`. Templates are bundled inside
the executable; logs and any uploaded temp files are written next to it.
"""

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

# Flask templates need to ship with the binary. The templates/ folder is
# referenced relative to app.py via Flask's default loader.
datas = [('templates', 'templates')]

# Selenium uses lazy imports for several drivers; collecting submodules
# avoids "module not found" surprises at runtime.
hiddenimports = ['NEWFILE'] + collect_submodules('selenium')

a = Analysis(
    ['app.py'],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['tkinter', 'ttkbootstrap', 'PIL'],  # web app doesn't need the desktop GUI deps
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
    name='RealbookExcelWeb',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,            # keep console so server logs are visible
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
