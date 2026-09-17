import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PLATE = ROOT.parent / "src" / "astrodoro" / "ui" / "assets" / "icon.png"
SIZES = (16, 32, 64, 128, 256, 512)


def icns() -> None:
    iconset = ROOT / "astrodoro.iconset"
    iconset.mkdir(exist_ok=True)
    for size in SIZES + (1024,):
        for name, px in ((f"icon_{size}x{size}.png", size),
                         (f"icon_{size // 2}x{size // 2}@2x.png", size)):
            if px <= 1024:
                subprocess.run(
                    ["sips", "-z", str(px), str(px), str(PLATE), "--out",
                     str(iconset / name)], check=True, capture_output=True,
                )
    subprocess.run(
        ["iconutil", "-c", "icns", str(iconset), "-o", str(ROOT / "astrodoro.icns")],
        check=True,
    )


def ico() -> None:
    from PIL import Image

    Image.open(PLATE).save(
        ROOT / "astrodoro.ico", sizes=[(s, s) for s in SIZES]
    )


if __name__ == "__main__":
    (icns if sys.platform == "darwin" else ico)()
