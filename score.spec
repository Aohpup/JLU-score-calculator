# -*- mode: python ; coding: utf-8 -*-

a = Analysis(
    ['score.py'],
    pathex=[],
    binaries=[],
    datas=[('index.html', '.'), ('jlu_transcript_explanation.jpg', '.'), ('LICENSE', '.'), ('NOTICE', '.'), ('THIRD_PARTY_NOTICES.md', '.')],
    hiddenimports=['openpyxl', 'xlrd'],
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
    name='成绩与绩点计算器',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
)
