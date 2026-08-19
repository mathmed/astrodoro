#!/usr/bin/env python3
"""Diagnostico e captura de teste da camera SVBony.

  python3 probe.py info
  python3 probe.py grab --bin 2 --exp 2.0 --gain 250 --frames 3 --out ./captures
  python3 probe.py bench --bin 2 --exp 0.1 --frames 20
  python3 probe.py tec --target -10
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from svbony.camera import Camera, list_cameras          # noqa: E402
from svbony.sdk import Control, ImgType, SVBError, TIMEOUT  # noqa: E402


def pick() -> Camera:
    cams = list_cameras()
    if not cams:
        sys.exit("nenhuma camera encontrada (AstroDMx aberto? Parallels com o USB?)")
    for c in cams:
        print(c)
    return Camera(cams[0])


def stats(a: np.ndarray, full: int = 16383) -> str:
    sat = 100.0 * np.count_nonzero(a >= full * 0.99) / a.size
    return (f"min={a.min()} max={a.max()} mediana={np.median(a):.0f} "
            f"media={a.mean():.1f} sigma={a.std():.1f} saturado={sat:.3f}%")


def cmd_info(args):
    with pick() as cam:
        print(cam.describe())


def cmd_grab(args):
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    with pick() as cam:
        cam.image_type = ImgType.RAW16
        g = cam.set_roi(bin=args.bin)
        cam.gain = args.gain
        cam.exposure = args.exp
        if args.offset is not None:
            cam.offset = args.offset
        print(f"\nROI {g.width}x{g.height} bin{g.bin} RAW16 | ganho {cam.gain} "
              f"| exp {cam.exposure:.3f}s | offset {cam.offset}")
        if cam.supports_cooler:
            print(f"sensor a {cam.temperature:.1f} C, TEC {'on' if cam.cooler else 'off'} "
                  f"({cam.cooler_power}%)")

        cam.start_video()
        # o primeiro frame apos start/mudanca de parametro pode vir stale
        try:
            cam.read_frame(timeout=cam.exposure * 2 + 2)
            print("frame de descarte ok")
        except SVBError as e:
            print(f"descarte falhou ({e}) — seguindo")

        for n in range(args.frames):
            t0 = time.perf_counter()
            frame = cam.read_frame(timeout=cam.exposure * 3 + 5)
            dt = time.perf_counter() - t0
            print(f"  frame {n+1}/{args.frames}  {dt*1000:7.1f} ms  {stats(frame, cam.full_scale)}")
            path = out / f"frame_{n:03d}.fits"
            save_fits(path, frame, cam)
            print(f"    -> {path}")
        print(f"descartados pela SDK: {cam.dropped_frames}")
        cam.stop_video()


def save_fits(path: Path, frame: np.ndarray, cam: Camera) -> None:
    try:
        from astropy.io import fits
    except ImportError:
        np.save(path.with_suffix(".npy"), frame)
        return
    hdu = fits.PrimaryHDU(frame)
    h = hdu.header
    g = cam.geometry
    h["INSTRUME"] = cam.id.name
    h["EXPTIME"] = (cam.exposure, "s")
    h["GAIN"] = cam.gain
    h["OFFSET"] = cam.offset
    h["XBINNING"] = g.bin
    h["YBINNING"] = g.bin
    h["XPIXSZ"] = (cam.pixel_size_um * g.bin, "um, efetivo")
    if cam.is_color:
        h["BAYERPAT"] = cam.bayer.name + "GB" if False else cam.bayer.name
    if cam.supports_cooler:
        h["CCD-TEMP"] = round(cam.temperature, 1)
        h["SET-TEMP"] = round(cam.target_temperature, 1)
    h["BITPIX14"] = (cam.bit_depth, "bits reais do ADC; dados alinhados a direita")
    h["DATE-OBS"] = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
    hdu.writeto(path, overwrite=True)


def cmd_bench(args):
    """Mede throughput real -- expõe se o USB esta' limitando."""
    with pick() as cam:
        cam.image_type = ImgType.RAW16
        g = cam.set_roi(bin=args.bin)
        cam.gain = args.gain
        cam.exposure = args.exp
        mb = g.width * g.height * 2 / 1e6
        print(f"\nROI {g.width}x{g.height} bin{g.bin} = {mb:.1f} MB/frame, exp {cam.exposure*1000:.0f} ms")
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
                print("erro:", e)
                break
            times.append(time.perf_counter() - t0)
        cam.stop_video()
        if times:
            t = np.array(times)
            print(f"n={len(t)}  medio={t.mean()*1000:.1f} ms  min={t.min()*1000:.1f}  max={t.max()*1000:.1f}")
            print(f"taxa efetiva: {1/t.mean():.2f} fps  ->  {mb/t.mean():.1f} MB/s")
            overhead = t.mean() - cam.exposure
            print(f"overhead de leitura (fora da exposicao): {overhead*1000:.1f} ms")


SPEED = {0: "low (1.5 Mbps)", 1: "full (12 Mbps)", 2: "high — USB 2.0 (480 Mbps)",
         3: "super — USB 3.0 (5 Gbps)", 4: "super+ — USB 3.1 (10 Gbps)"}


def cmd_usb(args):
    """Diagnostica o caminho USB. Um hub USB 2.0 no meio limita tudo atras dele
    silenciosamente, e o conector USB3 Type-B da camera aceita um plugue USB2
    Type-B sem reclamar. As duas coisas custam ~475 ms de tempo morto por frame."""
    import re
    import subprocess

    print("=== arvore USB do sistema ===")
    tree = subprocess.run(["ioreg", "-p", "IOUSB", "-w0"], capture_output=True, text=True).stdout
    print(re.sub(r"<class.*", "", tree).rstrip() or "  (vazia)")

    print("\n=== velocidade negociada por dispositivo ===")
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
                flag = "   <== HUB USB 2.0: limita tudo atras dele"
                hubs.append(name)
            print(f"  {name:<32} {SPEED.get(sp, sp)}{flag}")
            name = None

    print("\n=== o que a SDK reporta ===")
    cams = list_cameras()
    if not cams:
        print("  nenhuma camera conectada")
        if hubs:
            print(f"\n  atencao: ha {len(hubs)} hub USB 2.0 na maquina. Plugue a camera")
            print("  DIRETO numa porta USB-C/Thunderbolt do Mac, sem passar por dongle.")
        return
    for c in cams:
        print(f"  {c}")
        if c.port != "USB3.0":
            print(f"    negociado em {c.port} -> ~475 ms de tempo morto por frame.")
            print("    causas, em ordem de probabilidade:")
            print("      1. dongle/hub USB 2.0 no caminho (veja a arvore acima)")
            print("      2. cabo USB2 Type-B — encaixa no conector USB3 da camera")
            print("         e cai para 480 Mbps sem avisar")
            print("      3. porta USB-A de adaptador nao classificado como USB3")
        else:
            print("    USB 3.0 ok — rode `probe.py bench --bin 2` para medir o ganho")


def cmd_tec(args):
    with pick() as cam:
        if not cam.supports_cooler:
            sys.exit("esta camera nao reporta suporte a TEC")
        cam.target_temperature = args.target
        cam.cooler = True
        print(f"alvo {cam.target_temperature:.1f} C — monitorando (ctrl-C para sair)")
        try:
            while True:
                print(f"  {cam.temperature:6.1f} C   TEC {cam.cooler_power:3d}%")
                time.sleep(args.interval)
        except KeyboardInterrupt:
            print("\n(TEC continua ligado; use --off para desligar)")
            if args.off:
                cam.cooler = False
                print("TEC desligado")


def main():
    p = argparse.ArgumentParser(description="diagnostico da camera SVBony")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("info").set_defaults(fn=cmd_info)
    sub.add_parser("usb").set_defaults(fn=cmd_usb)

    g = sub.add_parser("grab")
    g.add_argument("--bin", type=int, default=2)
    g.add_argument("--exp", type=float, default=1.0, help="segundos")
    g.add_argument("--gain", type=int, default=250)
    g.add_argument("--offset", type=int, default=None)
    g.add_argument("--frames", type=int, default=3)
    g.add_argument("--out", default="./captures")
    g.set_defaults(fn=cmd_grab)

    b = sub.add_parser("bench")
    b.add_argument("--bin", type=int, default=2)
    b.add_argument("--exp", type=float, default=0.05)
    b.add_argument("--gain", type=int, default=100)
    b.add_argument("--frames", type=int, default=20)
    b.set_defaults(fn=cmd_bench)

    t = sub.add_parser("tec")
    t.add_argument("--target", type=float, default=-10)
    t.add_argument("--interval", type=float, default=3)
    t.add_argument("--off", action="store_true")
    t.set_defaults(fn=cmd_tec)

    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
