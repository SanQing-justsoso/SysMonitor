# -*- mode: python ; coding: utf-8 -*-

import os


SPEC_DIR = os.path.dirname(os.path.abspath(__file__))
datas = [(os.path.join(SPEC_DIR, 'LibreHardwareMonitor'), 'LibreHardwareMonitor')]
_presentmon = os.path.join(SPEC_DIR, 'PresentMon.exe')
if os.path.exists(_presentmon):
    datas.append((_presentmon, '.'))


a = Analysis(
    [os.path.join(SPEC_DIR, 'taskbar_monitor.py')],
    pathex=[SPEC_DIR],
    binaries=[],
    datas=datas,
    hiddenimports=['pystray._win32'],
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
    name='SysMonitor',
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
    icon=['icon.ico'],
)
