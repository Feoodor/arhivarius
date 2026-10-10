# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['app.py'],
    pathex=[],
    binaries=[],
    datas=[('ui/guide.txt', 'ui')],
    hiddenimports=['core.stages', 'core.audit', 'core.refine', 'core.lessons', 'core.routing',
                   'core.folders_ops', 'core.folder_router', 'core.prerecognize', 'core.fewshot',
                   'core.photodupes', 'core.embeds', 'ui.folders_window', 'ui.i18n'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['pandas', 'numba', 'llvmlite', 'scipy', 'matplotlib', 'pyarrow'],
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
    name='Архивариус',
    version='version_info.txt',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['logo.ico'],
)
