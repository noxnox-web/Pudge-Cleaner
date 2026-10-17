# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['run.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('pudge_gaming_manager/app/gui/assets', 'pudge_gaming_manager/app/gui/assets'),
        # The «Ядра GPU» tab's test scene and frame recorder, with their licences.
        ('pudge_gaming_manager/vendor/gpu_affinity', 'pudge_gaming_manager/vendor/gpu_affinity'),
    ],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # A widgets-only program: nothing here uses QML/Quick, PDF, OpenGL, Qt's
    # network stack or pywin32's MFC layer. They were pulled in by plugins
    # (virtual keyboard -> Quick/QML, TLS and TUIO -> Network) and cost
    # launch time, because a one-file exe unpacks everything on every start.
    excludes=[
        "PySide6.QtNetwork", "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtPdf",
        "PySide6.QtOpenGL", "win32ui", "pythonwin", "tkinter", "unittest",
    ],
    noarchive=False,
    optimize=0,
)

#: Files dropped from the bundle by name, each unused by a widgets program.
_UNUSED = (
    "opengl32sw.dll",                   # software OpenGL fallback, 20 MB
    "qt6quick", "qt6qml", "qt6pdf", "qt6opengl", "qt6network", "qt6virtualkeyboard",
    "libcrypto-3-x64.dll", "libssl-3-x64.dll",   # Qt's own OpenSSL, for TLS
    "\\tls\\", "\\networkinformation\\", "\\platforminputcontexts\\",
    "\\generic\\", "qdirect2d.dll", "qpdf.dll", "win32ui",
)


def _keep(entry):
    name = entry[0].lower()
    return not any(token in name for token in _UNUSED)


a.binaries = [e for e in a.binaries if _keep(e)]
a.datas = [e for e in a.datas if _keep(e)]
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='PudgeCleaner',
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
    uac_admin=True,
    icon=['pudge_gaming_manager/app/gui/assets/logo.ico'],
)
