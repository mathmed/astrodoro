"""Camera diagnostics and test capture.

    astrodoro info
    astrodoro grab --bin 2 --exp 2.0 --gain 250 --frames 3
    astrodoro bench --bin 2 --exp 0.1 --frames 20
    astrodoro usb
    astrodoro tec --target -10
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np

from ..drivers.svbony import Camera, list_cameras
from ..drivers.svbony.sdk import ImgType, SVBError
from ..settings import Settings


def pick() -> Camera:
    cams = list_cameras()
    if not cams:
        sys.exit("no camera found (is another capture program holding it?)")
    for c in cams:
        print(c)
    return Camera(cams[0])


def stats(a: np.ndarray, full: int = 16383) -> str:
    sat = 100.0 * np.count_nonzero(a >= full * 0.99) / a.size
    return (f"min={a.min()} max={a.max()} median={np.median(a):.0f} "
            f"mean={a.mean():.1f} sigma={a.std():.1f} saturated={sat:.3f}%")


def cmd_info(args) -> None:
    with pick() as cam:
        print(cam.describe())


def cmd_grab(args) -> None:
    out = Path(args.out).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    with pick() as cam:
        cam.image_type = ImgType.RAW16
        g = cam.set_roi(bin=args.bin)
        cam.gain = args.gain
        cam.exposure = args.exp
        if args.offset is not None:
            cam.offset = args.offset
        print(f"\nROI {g.width}x{g.height} bin{g.bin} RAW16 | gain {cam.gain} "
              f"| exp {cam.exposure:.3f}s | offset {cam.offset}")
        if cam.supports_cooler:
            print(f"sensor at {cam.temperature:.1f} C, "
                  f"cooler {'on' if cam.cooler else 'off'} "
                  f"({cam.cooler_power}%)")

        cam.start_video()
        # The first frame after start, or after a parameter change, may be stale.
        try:
            cam.read_frame(timeout=cam.exposure * 2 + 2)
            print("discard frame ok")
        except SVBError as e:
            print(f"discard failed ({e}) — carrying on")

        for n in range(args.frames):
            t0 = time.perf_counter()
            frame = cam.read_frame(timeout=cam.exposure * 3 + 5)
            dt = time.perf_counter() - t0
            print(f"  frame {n+1}/{args.frames}  {dt*1000:7.1f} ms  "
                  f"{stats(frame, cam.full_scale)}")
            path = out / f"frame_{n:03d}.fits"
            save_fits(path, frame, cam)
            print(f"    -> {path}")
        print(f"dropped by the SDK: {cam.dropped_frames}")
        cam.stop_video()


def save_fits(path: Path, frame: np.ndarray, cam: Camera) -> None:
    from astropy.io import fits

    hdu = fits.PrimaryHDU(frame)
    h = hdu.header
    g = cam.geometry
    h["INSTRUME"] = cam.id.name
    h["EXPTIME"] = (cam.exposure, "s")
    h["GAIN"] = cam.gain
    h["OFFSET"] = cam.offset
    h["XBINNING"] = g.bin
    h["YBINNING"] = g.bin
    h["XPIXSZ"] = (cam.pixel_size_um * g.bin, "um, effective")
    if cam.is_color:
        h["BAYERPAT"] = cam.bayer.fits_name
    if cam.supports_cooler:
        h["CCD-TEMP"] = round(cam.temperature, 1)
        h["SET-TEMP"] = round(cam.target_temperature, 1)
    h["FULLSCAL"] = (cam.full_scale, "data saturation value")
    h["BITDEPTH"] = (cam.bit_depth, "real ADC bits")
    h["DATE-OBS"] = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
    hdu.writeto(path, overwrite=True)


def cmd_bench(args) -> None:
    """Measure real throughput — exposes whether USB is the limit."""
    with pick() as cam:
        cam.image_type = ImgType.RAW16
        g = cam.set_roi(bin=args.bin)
        cam.gain = args.gain
        cam.exposure = args.exp
        mb = g.width * g.height * 2 / 1e6
        print(f"\nROI {g.width}x{g.height} bin{g.bin} = {mb:.1f} MB/frame, "
              f"exp {cam.exposure*1000:.0f} ms")
        cam.start_video()
        try:
            cam.read_frame(timeout=5)
        except SVBError:
            pass
        times = []
        for _ in range(args.frames):
            t0 = time.perf_counter()
            try:
                cam.read_frame(timeout=cam.exposure * 3 + 5)
            except SVBError as e:
                print("error:", e)
                break
            times.append(time.perf_counter() - t0)
        cam.stop_video()
        if times:
            t = np.array(times)
            print(f"n={len(t)}  mean={t.mean()*1000:.1f} ms  "
                  f"min={t.min()*1000:.1f}  max={t.max()*1000:.1f}")
            print(f"effective rate: {1/t.mean():.2f} fps  ->  "
                  f"{mb/t.mean():.1f} MB/s")
            print(f"read overhead (outside the exposure): "
                  f"{(t.mean() - cam.exposure)*1000:.1f} ms")


SPEED = {0: "low (1.5 Mbps)", 1: "full (12 Mbps)",
         2: "high — USB 2.0 (480 Mbps)", 3: "super — USB 3.0 (5 Gbps)",
         4: "super+ — USB 3.1 (10 Gbps)"}


def cmd_usb(args) -> None:
    """Diagnose the USB path.

    A USB 2.0 hub in the middle silently limits everything behind it, and the
    camera's USB3 Type-B connector accepts a USB2 Type-B plug without
    complaining. Either costs ~475 ms of dead time per frame.
    """
    import re
    import subprocess

    if sys.platform != "darwin":
        print("this diagnostic uses ioreg and only runs on macOS")
        return

    print("=== system USB tree ===")
    tree = subprocess.run(["ioreg", "-p", "IOUSB", "-w0"],
                          capture_output=True, text=True).stdout
    print(re.sub(r"<class.*", "", tree).rstrip() or "  (empty)")

    print("\n=== negotiated speed per device ===")
    devs = subprocess.run(["ioreg", "-rc", "IOUSBHostDevice", "-w0", "-l"],
                          capture_output=True, text=True).stdout
    name = None
    hubs = []
    for line in devs.splitlines():
        if "USB Product Name" in line:
            name = line.split("=", 1)[1].strip().strip('"')
        elif "Device Speed" in line and name:
            sp = int(line.split("=", 1)[1].strip())
            flag = ""
            if "hub" in name.lower() and sp <= 2:
                flag = "   <== USB 2.0 HUB: limits everything behind it"
                hubs.append(name)
            print(f"  {name:<32} {SPEED.get(sp, sp)}{flag}")
            name = None

    print("\n=== what the SDK reports ===")
    cams = list_cameras()
    if not cams:
        print("  no camera connected")
        if hubs:
            print(f"\n  note: there are {len(hubs)} USB 2.0 hubs on this "
                  f"machine. Plug the camera")
            print("  DIRECTLY into a USB-C/Thunderbolt port, with no dongle.")
        return
    for c in cams:
        print(f"  {c}")
        if c.port != "USB3.0":
            print(f"    negotiated at {c.port} -> ~475 ms of dead time per frame.")
            print("    causes, in order of likelihood:")
            print("      1. a USB 2.0 dongle/hub in the path (see the tree above)")
            print("      2. a USB2 Type-B cable — it fits the camera's USB3")
            print("         connector and drops to 480 Mbps without warning")
            print("      3. a USB-A port on an adapter not classed as USB3")
        else:
            print("    USB 3.0 ok — run `astrodoro bench --bin 2` to measure it")


def cmd_tec(args) -> None:
    with pick() as cam:
        if not cam.supports_cooler:
            sys.exit("this camera does not report cooler support")
        cam.target_temperature = args.target
        cam.cooler = True
        print(f"target {cam.target_temperature:.1f} C — monitoring "
              f"(ctrl-C to exit)")
        try:
            while True:
                print(f"  {cam.temperature:6.1f} C   "
                      f"TEC {cam.cooler_power:3d}%")
                time.sleep(args.interval)
        except KeyboardInterrupt:
            if args.off:
                cam.cooler = False
                print("\ncooler off")
            else:
                print("\n(the cooler stays on; use --off to switch it off)")


def add_parsers(sub) -> None:
    s = Settings.load()

    sub.add_parser("info", help="describe the camera").set_defaults(fn=cmd_info)
    sub.add_parser("usb", help="diagnose the USB path").set_defaults(fn=cmd_usb)

    g = sub.add_parser("grab", help="capture a few frames to FITS")
    g.add_argument("--bin", type=int, default=s.binning)
    g.add_argument("--exp", type=float, default=1.0, help="seconds")
    g.add_argument("--gain", type=int, default=s.gain)
    g.add_argument("--offset", type=int, default=None)
    g.add_argument("--frames", type=int, default=3)
    g.add_argument("--out", default=s.export_dir)
    g.set_defaults(fn=cmd_grab)

    b = sub.add_parser("bench", help="measure real throughput")
    b.add_argument("--bin", type=int, default=s.binning)
    b.add_argument("--exp", type=float, default=0.05)
    b.add_argument("--gain", type=int, default=100)
    b.add_argument("--frames", type=int, default=20)
    b.set_defaults(fn=cmd_bench)

    t = sub.add_parser("tec", help="monitor the cooler")
    t.add_argument("--target", type=float, default=-10)
    t.add_argument("--interval", type=float, default=3)
    t.add_argument("--off", action="store_true")
    t.set_defaults(fn=cmd_tec)
