# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['Pak-Rat\\pak_rat.py'],
    pathex=['Pak-Rat'],
    binaries=[],
    datas=[('Pak-Rat/Pak-Rat.ico', '.'), ('Pak-Rat/Pak-Rat.png', '.'), ('Pak-Rat/vendor', 'vendor')],
    hiddenimports=['core', 'cook', 'inject', 'PIL.Image'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['PySide6.QtQml', 'PySide6.QtQuick', 'PySide6.QtQuick3D', 'PySide6.QtQuickWidgets', 'PySide6.QtQuickControls2', 'PySide6.QtNetwork', 'PySide6.QtPdf', 'PySide6.QtPdfWidgets', 'PySide6.QtOpenGL', 'PySide6.QtOpenGLWidgets', 'PySide6.QtWebEngineCore', 'PySide6.QtWebEngineWidgets', 'PySide6.QtWebChannel', 'PySide6.QtWebSockets', 'PySide6.QtMultimedia', 'PySide6.QtMultimediaWidgets', 'PySide6.QtCharts', 'PySide6.QtDataVisualization', 'PySide6.QtSql', 'PySide6.QtTest', 'PySide6.QtSvg', 'PySide6.QtSvgWidgets', 'PySide6.QtPrintSupport', 'PySide6.QtXml', 'PySide6.QtConcurrent', 'PySide6.QtPositioning', 'PySide6.QtSensors', 'PySide6.QtSerialPort', 'PySide6.QtBluetooth', 'PySide6.QtNfc', 'PySide6.QtHelp', 'PySide6.QtUiTools', 'PySide6.QtDesigner', 'PySide6.QtScxml', 'PySide6.QtStateMachine', 'PySide6.QtRemoteObjects', 'PySide6.QtTextToSpeech', 'PySide6.QtSpatialAudio', 'PySide6.Qt3DCore', 'PySide6.Qt3DRender', 'PySide6.QtDBus', 'PySide6.QtNetworkAuth', 'PySide6.QtLocation', 'tkinter', 'numpy', 'unittest', 'pydoc', 'lib2to3', 'pdb'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='Pak-Rat',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['Pak-Rat\\Pak-Rat.ico'],
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='Pak-Rat',
)
