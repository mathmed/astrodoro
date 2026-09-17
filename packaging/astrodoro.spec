# PyInstaller build. One binary that opens the window with no arguments and
# runs the CLI with them, so a downloaded Astrodoro is also `astrodoro replay`.
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files

ROOT = Path(SPECPATH).parent
PKG = ROOT / "src" / "astrodoro"
VERSION = (PKG / "__init__.py").read_text().split('"')[1]

datas = [
    (str(PKG / "ui" / "web" / "handset.html"), "astrodoro/ui/web"),
    (str(PKG / "ui" / "assets" / "icon.png"), "astrodoro/ui/assets"),
]
for mo in sorted(PKG.glob("i18n/locale/*/LC_MESSAGES/*.mo")):
    datas.append((str(mo), f"astrodoro/i18n/locale/{mo.parents[1].name}/LC_MESSAGES"))
datas += collect_data_files("erfa")

# The vendor SDK is not redistributable, so it is never bundled: the driver
# looks for it at SVB_SDK_PATH, or where AstroDMx keeps it.
excludes = [
    "tkinter",
    "matplotlib",
    "PySide6.QtWebEngineCore",
    "PySide6.QtWebEngineWidgets",
    "PySide6.QtQuick",
    "PySide6.QtQml",
    "PySide6.Qt3DCore",
    "PySide6.QtCharts",
    "PySide6.QtDataVisualization",
    "PySide6.QtDesigner",
    "PySide6.QtBluetooth",
]

a = Analysis(
    [str(ROOT / "packaging" / "launcher.py")],
    pathex=[str(ROOT / "src")],
    datas=datas,
    hookspath=[str(ROOT / "packaging" / "hooks")],
    hiddenimports=["sep", "astroalign", "astrodoro.cli", "astrodoro.ui"],
    excludes=excludes,
    noarchive=False,
)
pyz = PYZ(a.pure)

ICNS = ROOT / "packaging" / "astrodoro.icns"
ICO = ROOT / "packaging" / "astrodoro.ico"

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="astrodoro",
    console=False,
    icon=str(ICO) if ICO.exists() else None,
)
coll = COLLECT(exe, a.binaries, a.datas, name="astrodoro")

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="Astrodoro.app",
        icon=str(ICNS) if ICNS.exists() else None,
        bundle_identifier="com.mathmed.astrodoro",
        version=VERSION,
        info_plist={
            "NSHighResolutionCapable": True,
            "LSMinimumSystemVersion": "12.0",
            "NSCameraUsageDescription": "Astrodoro reads frames from the camera.",
        },
    )
