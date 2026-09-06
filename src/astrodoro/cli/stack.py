"""Headless capture: calibration masters, a live stacking session, and replay.

    astrodoro bias --gain 250 --frames 30
    astrodoro dark --exp 5 --gain 250 --frames 20
    astrodoro flat --gain 250 --exp 0.05 --bias ~/Astrodoro/bias/bias_g250_o20_bin2.fits
    astrodoro run  --exp 5 --gain 250 --dark ~/Astrodoro/darks/dark_g250.fits
    astrodoro replay ~/Astrodoro/sessions/2026-08-18/2130_M8

During a run: Ctrl-C ends and saves. A blank Enter marks a new segment (use it
after resetting the equatorial platform or recentring the tube).
"""
from __future__ import annotations

import queue
import select
import sys
import threading
import time
from pathlib import Path

import cv2
import numpy as np
from astropy.io import fits

from ..core import debayer, masters, stretch
from ..core.calibration import calibrate, flat_master, hot_pixel_map, prepare_flat
from ..core.recorder import read_fits
from ..core.stacker import LiveStacker
from ..drivers.svbony import Camera, list_cameras
from ..drivers.svbony.sdk import ImgType, SVBError
from ..settings import Settings


# --------------------------------------------------------------------- camera
def setup(args) -> Camera:
    cams = list_cameras()
    if not cams:
        sys.exit("no camera found (is another capture program holding it?)")
    cam = Camera(cams[0]).open()
    print(f"{cam.id.name}  fw {cam.firmware}  port {cam.id.port}")
    if cam.id.port != "USB3.0":
        print(f"  warning: negotiated at {cam.id.port} — ~475 ms of dead time "
              f"per frame. Change the cable if you can.")
    if cam.forced_linear:
        print(f"  pipeline linearised: {', '.join(cam.forced_linear)}")

    cam.image_type = ImgType.RAW16
    g = cam.set_roi(bin=args.bin)
    cam.gain = args.gain
    cam.exposure = args.exp
    cam.offset = args.offset

    if args.target_temp is not None and cam.supports_cooler:
        cam.target_temperature = args.target_temp
        cam.cooler = True
        print(f"  cooler on, target {args.target_temp:+.1f} °C "
              f"(sensor at {cam.temperature:+.1f} °C now)")

    print(f"  ROI {g.width}x{g.height} bin{g.bin} RAW16 | gain {cam.gain} | "
          f"exp {cam.exposure:.2f}s | offset {cam.offset} | "
          f"scale 0..{cam.full_scale}")
    return cam


def capture_loop(cam: Camera, q: queue.Queue, stop: threading.Event) -> None:
    """Capture thread. ctypes releases the GIL inside SVBGetVideoData, so this
    does not block the stacking."""
    cam.start_video()
    first = True
    while not stop.is_set():
        try:
            f = cam.read_frame(timeout=cam.exposure * 3 + 8)
        except SVBError as e:
            if stop.is_set():
                break
            print(f"\n  capture: {e}")
            continue
        if first:                    # the first frame after start is stale
            first = False
            continue
        try:
            q.put_nowait((f, time.time()))
        except queue.Full:
            try:                     # drop the oldest: a fresh frame is better
                q.get_nowait()
                q.put_nowait((f, time.time()))
            except queue.Empty:
                pass
    try:
        cam.stop_video()
    except SVBError:
        pass


class _Meta:
    """The one field `calibrate` needs from a live camera frame."""

    def __init__(self, full_scale: int):
        self.full_scale = full_scale


def grab(cam, n: int, report=None) -> list:
    """N frames for a master, dropping the first: it was already being exposed
    when the parameters were set, so it belongs to the previous state."""
    cam.start_video()
    frames = []
    for k in range(n + 1):
        try:
            f = cam.read_frame(timeout=cam.exposure * 3 + 8)
        except SVBError as e:
            print(f"  frame {k}: {e}")
            continue
        if k == 0:
            continue
        frames.append(f)
        if report is not None:
            report(f, len(frames))
        else:
            print(f"  {len(frames)}/{n}  median={np.median(f):.0f} "
                  f"max={f.max()}", end="\r")
    cam.stop_video()
    print()
    return frames


def load_masters(args, shape=None) -> tuple:
    """(bias, dark, flat, hot) from whatever the command line offered.

    The bias is loaded even when a dark is: `calibrate` decides which of the two
    to subtract, and saying here that a bias was given and then dropping it
    would hide that decision from the one place it is visible.
    """
    bias = dark = flat = hot = None
    if getattr(args, "bias", None):
        bias = read_fits(args.bias)[0].astype(np.float32)
        print(f"  bias: {args.bias} (median {np.median(bias):.0f})")
    if getattr(args, "dark", None):
        dark = read_fits(args.dark)[0].astype(np.float32)
        print(f"  dark: {args.dark} (median {np.median(dark):.0f})")
        if bias is not None:
            print("  (the dark already contains the bias; the bias is not "
                  "subtracted twice)")
        hot_path = Path(str(args.dark).replace(".fits", "_hot.npy"))
        if hot_path.exists():
            hot = np.load(hot_path)
            print(f"  hot pixel map: {hot.sum()} px")
    if getattr(args, "flat", None):
        raw = read_fits(args.flat)[0].astype(np.float32)
        flat = prepare_flat(raw)
        if flat is None:
            print(f"  flat ignored (median <= 0): {args.flat}")
        else:
            print(f"  flat: {args.flat} (corner at {flat.min()*100:.0f}% of "
                  f"the centre)")
    if shape is not None:
        for label, m in (("bias", bias), ("dark", dark), ("flat", flat)):
            if m is not None and m.shape != shape:
                sys.exit(f"{label} is {m.shape}, the frames are {shape} "
                         f"(different bin?)")
    return bias, dark, flat, hot


# ----------------------------------------------------------------------- bias
def cmd_bias(args) -> None:
    """Master bias: the offset pedestal, at the shortest exposure the camera does.

    It is what a flat has to have subtracted, and the pedestal to use when no
    dark of the right exposure exists. Gain, offset and bin must match the
    frames it will correct — the exposure is the one thing that must not.
    """
    out = Path(args.out).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    cam = setup(args)
    cam.exposure = cam.min_exposure
    setup_ = masters.Setup.from_camera(cam)
    print(f"\ncapturing {args.frames} bias frames of "
          f"{setup_.exposure*1e6:.0f} us at gain {setup_.gain}, "
          f"offset {setup_.offset}, bin{setup_.bin}")
    print("CAP THE SENSOR and make sure the room is dark.")
    input("Enter when it is capped: ")

    frames = grab(cam, args.frames)
    if len(frames) < 3:
        sys.exit("not enough frames for a bias")
    master = masters.combine(frames)
    path = masters.write(out, "bias", master, setup_, len(frames))
    print(f"master bias -> {path}")
    print(f"  median {np.median(master):.1f} ADU  sigma {master.std():.2f} ADU"
          f"  min {master.min():.0f}  max {master.max():.0f}")
    if master.min() <= 0:
        print("  warning: pixels at zero — the offset is truncating the left "
              "tail of the noise; raise it (astrodoro sensor measures it)")
    cam.close()


# ----------------------------------------------------------------------- dark
def cmd_dark(args) -> None:
    out = Path(args.out).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    cam = setup(args)
    if cam.supports_cooler and args.target_temp is not None:
        # Same ramp as the interface: sending the target in one go makes the TEC
        # pull 100% and the temperature plunge.
        from ..core.cooling import CoolerController
        ctrl = CoolerController(ramp_c_per_min=2.0)
        ctrl.set_target(args.target_temp, current=cam.temperature)
        print(f"\nramping to {args.target_temp:+.1f} °C at 2 °C/min...")
        deadline = time.time() + 45 * 60
        while time.time() < deadline:
            sp, enable = ctrl.update(cam.temperature, cam.cooler_power,
                                     time.time())
            if sp is not None:
                cam.target_temperature = float(sp)
            cam.cooler = enable
            st = ctrl.state
            print(f"  {st.message[:74]}   ", end="\r")
            if st.phase == "stable":
                break
            if st.phase == "saturated":
                print(f"\n  {st.message}")
                break
            time.sleep(3)
        print(f"\n  sensor at {cam.temperature:+.1f} °C, "
              f"TEC {cam.cooler_power}%")

    print(f"\nCAP THE SENSOR. capturing {args.frames} darks of "
          f"{cam.exposure:.1f}s...")
    input("Enter when it is capped: ")

    frames = grab(cam, args.frames)
    if len(frames) < 3:
        sys.exit("not enough frames for a dark")

    master = masters.combine(frames)
    path = masters.write(out, "dark", master, masters.Setup.from_camera(cam),
                         len(frames))
    print(f"master dark -> {path}")
    print(f"  median {np.median(master):.1f}  sigma {master.std():.1f}  "
          f"max {master.max():.0f}")

    hot = hot_pixel_map(master)
    np.save(path.with_name(path.name.replace(".fits", "_hot.npy")), hot)
    print(f"  hot pixel map: {hot.sum()} px ({100*hot.mean():.4f}%)")
    cam.close()


# ----------------------------------------------------------------------- flat
def cmd_flat(args) -> None:
    """Master flat: an evenly illuminated surface, the median of N frames.

    Pass `--bias`, or a `--dark` of the flat's own exposure: the sensor's offset
    pedestal would otherwise enter a multiplicative correction and flatten the
    vignetting curve everywhere. A bias is the honest choice — a flat is
    milliseconds long, and a dark of the session's exposure carries thermal
    signal this frame never collected.
    """
    out = Path(args.out).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    cam = setup(args)
    pedestal = None
    if args.bias:
        pedestal = read_fits(args.bias)[0].astype(np.float32)
        print(f"flat bias: {args.bias} (median {np.median(pedestal):.0f})")
    elif args.dark:
        pedestal = read_fits(args.dark)[0].astype(np.float32)
        print(f"flat dark: {args.dark} (median {np.median(pedestal):.0f})")
    else:
        print("warning: without --bias or --dark, the offset pedestal is baked "
              "into the flat")

    print(f"\ncapturing {args.frames} flats of {cam.exposure:.3f}s...")

    def report(f, k):
        med = np.median(f)
        print(f"  {k}/{args.frames}  median={med:.0f} "
              f"({med/cam.full_scale*100:.0f}% of scale)"
              + ("  SATURATING" if f.max() >= cam.full_scale * 0.98 else ""),
              end="\r")

    frames = grab(cam, args.frames, report=report)
    if len(frames) < 3:
        sys.exit("not enough frames for a flat")

    master = flat_master(frames, pedestal)
    m = float(np.median(master))
    if m <= 0:
        sys.exit("invalid flat (median <= 0)")
    path = masters.write(out, "flat", master, masters.Setup.from_camera(cam),
                         len(frames))
    norm = master / m
    print(f"master flat -> {path}")
    print(f"  median {m:.0f} ({m/cam.full_scale*100:.0f}% of scale)")
    print(f"  vignetting: corner at {norm.min()*100:.0f}% of the centre, "
          f"range {norm.min()*100:.0f}-{norm.max()*100:.0f}%")
    cam.close()


# -------------------------------------------------------------------- session
HEADER = (f"{'#':>4} {'stars':>5} {'FWHM':>6} {'pair':>4} {'rms':>5} "
          f"{'rot°':>7} {'drift':>14} {'cov%':>5} {'integ':>8} {'ms':>6}  status")


def _row(st: LiveStacker, o, extra: str = "") -> str:
    al = o.alignment
    dx, dy = st.drift()
    return (f"{st.n_stacked:4d} {o.n_stars:5d} {o.fwhm:6.2f} "
            f"{(al.n_matched if al else 0):4d} "
            f"{(f'{al.rms:.2f}' if al and np.isfinite(al.rms) else '-'):>5} "
            f"{(f'{al.rotation_deg:+.3f}' if al else '-'):>7} "
            f"{f'{dx:+6.1f},{dy:+6.1f}':>14} "
            f"{st.overlap_fraction()*100:5.1f} "
            f"{st.total_exposure:7.0f}s {o.elapsed_ms:6.0f}  "
            f"{'ok' if o.accepted else 'rej: ' + o.reason}{extra}")


def cmd_run(args) -> None:
    out = Path(args.out).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    cam = setup(args)

    g = cam.geometry
    bias, dark, flat, hot = load_masters(args, shape=(g.height, g.width))
    for label, path in (("bias", args.bias), ("dark", args.dark),
                        ("flat", args.flat)):
        if not path:
            continue
        try:
            for warning in masters.mismatch(label, read_fits(path)[1],
                                            masters.Setup.from_camera(cam)):
                print(f"  warning: {warning}")
        except (SVBError, OSError, ValueError):
            pass

    st = LiveStacker((g.height, g.width), channels=3,
                     sigma_clip=args.sigma_clip,
                     # Saturation on the luminance scale, which sums the 2x2 quad.
                     saturation=debayer.LUM_SUM,
                     ref_refresh=args.ref_refresh,
                     min_ref_stars=args.min_ref_stars)
    st.set_strictness(args.strictness)

    q: queue.Queue = queue.Queue(maxsize=2)
    stop = threading.Event()
    th = threading.Thread(target=capture_loop, args=(cam, q, stop), daemon=True)
    th.start()

    meta = _Meta(cam.full_scale)
    print("\nstacking. Ctrl-C ends; Enter marks a new segment.\n")
    print(HEADER)
    t_start = time.time()
    try:
        while True:
            # Only look at the keyboard if there is a terminal: with the output
            # redirected, select reports stdin ready at EOF and this would fire
            # a new segment on every frame.
            if sys.stdin.isatty() and select.select([sys.stdin], [], [], 0)[0]:
                sys.stdin.readline()
                st.new_segment()
                print("  --- new segment: thresholds relaxed, the reference "
                      "will be re-extracted from the stack")
            try:
                raw, _ts = q.get(timeout=1.0)
            except queue.Empty:
                continue

            f = calibrate(raw, meta, dark=dark, flat=flat, hot=hot, bias=bias)
            rgb = debayer.to_rgb((f * 65535).astype(np.uint16), cam.bayer,
                                 quality="linear").astype(np.float32) / 65535.0
            lum = debayer.cfa_to_luminance(f)

            o = st.add(rgb, lum, exposure=cam.exposure, lum_scale=2.0)
            print(_row(st, o))

            if o.accepted and st.n_stacked % args.preview_every == 0:
                _write_preview(st, out, args)
            if not o.accepted:
                # Rejected frame, or the stack has not started: you still need
                # the live image to frame, focus and understand the rejections.
                _write_frame_preview(rgb, out, args)
            if args.max_frames and len(st.history) >= args.max_frames:
                print("  --- frame limit reached")
                break
    except KeyboardInterrupt:
        print("\nfinishing...")
    finally:
        stop.set()
        th.join(timeout=5)
        if st.n_stacked:
            _write_preview(st, out, args, final=True)
            print(f"\n{st.n_stacked} frames, {st.total_exposure:.0f}s of "
                  f"integration, {st.n_rejected} rejected, "
                  f"{time.time()-t_start:.0f}s of session")
        cam.close()


def cmd_replay(args) -> None:
    """Replay a recorded session through the same pipeline as the camera.

    This is what allows tuning thresholds in daylight: one night of capture
    becomes as many iterations as you want.
    """
    from ..core.source import ReplaySource

    out = Path(args.out).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    src = ReplaySource(args.folder, speed=args.speed, loop=False)
    info = src.open()
    print(f"{info['name']}  {info['width']}x{info['height']} bin{info['bin']}  "
          f"Bayer {info['bayer']}  scale 0..{info['full_scale']}  "
          f"{info['n_frames']} frames")

    bias, dark, flat, _hot = load_masters(
        args, shape=(info["height"], info["width"]))

    st = LiveStacker((info["height"], info["width"]), channels=3,
                     sigma_clip=args.sigma_clip, saturation=debayer.LUM_SUM)
    st.set_strictness(args.strictness)
    src.start()
    print("\n" + HEADER)
    while True:
        got = src.read()
        if got is None:
            break
        raw, meta = got
        f = calibrate(raw, meta, dark=dark, flat=flat, bias=bias)
        rgb = debayer.to_rgb((f * 65535).astype(np.uint16), meta.bayer,
                             quality="linear").astype(np.float32) / 65535.0
        o = st.add(rgb, debayer.cfa_to_luminance(f), exposure=meta.exposure,
                   lum_scale=2.0)
        print(_row(st, o))
    if st.n_stacked:
        _write_preview(st, out, args, final=True)
        print(f"\n{st.n_stacked} frames, {st.total_exposure:.0f}s, "
              f"{st.n_rejected} rejected")


def _write_frame_preview(rgb: np.ndarray, out: Path, args) -> None:
    disp = stretch.autostretch(rgb, target_bg=args.target_bg,
                               linked=args.linked)
    cv2.imwrite(str(out / "live.png"),
                cv2.cvtColor(stretch.to_uint8(disp), cv2.COLOR_RGB2BGR))


def _write_preview(st: LiveStacker, out: Path, args,
                   final: bool = False) -> None:
    img = st.result()
    disp = stretch.autostretch(img, target_bg=args.target_bg,
                               linked=args.linked)
    name = "stack_final.png" if final else "stack.png"
    cv2.imwrite(str(out / name),
                cv2.cvtColor(stretch.to_uint8(disp), cv2.COLOR_RGB2BGR))
    if final:
        hdu = fits.PrimaryHDU(img.transpose(2, 0, 1).astype(np.float32))
        hdu.header["NCOMBINE"] = st.n_stacked
        hdu.header["EXPTOTAL"] = st.total_exposure
        hdu.writeto(out / "stack_final.fits", overwrite=True)
        print(f"  -> {out/name}  and  {out/'stack_final.fits'}")


# ------------------------------------------------------------------ lucky stack
def cmd_lucky(args) -> None:
    """Stack a burst of the Moon or a planet: rank by sharpness, align, average.

    Offline and not live because lucky imaging picks frames by comparing them
    against each other — the sharpest frame of the burst may be the last one.
    See `core/lucky_stack.py` for why none of the deep-sky path applies.
    """
    from ..core import lucky_stack

    folder = Path(args.folder).expanduser()
    paths = lucky_stack.subs(folder)
    if not paths:
        print(f"no FITS files in {folder}")
        return

    bias, dark, flat, _hot = load_masters(args)
    if dark is None:
        # The lucky path never had a dark of its own exposure to offer: a burst
        # runs at milliseconds, and the bias is the pedestal at any exposure.
        dark = bias

    print(f"{len(paths)} frames in {folder}")
    ranked = lucky_stack.rank(paths)
    measured = sum(1 for f in ranked if f.measured)
    if measured:
        # A burst this program recorded carries SHARPNS; anything else does not,
        # and then ranking has to read every pixel of every frame.
        print(f"  {measured} frames had no SHARPNS — measured them")
    print(f"  sharpness {ranked[-1].sharpness:.1f} .. {ranked[0].sharpness:.1f}"
          f"   best: {ranked[0].path.name}")

    def show(done: int, total: int, name: str) -> None:
        end = "\n" if done == total else "\r"
        print(f"  aligning {done}/{total}  {name}", end=end, flush=True)

    result = lucky_stack.stack(folder, best=args.best / 100.0,
                               dark=dark, flat=flat,
                               subtract_background=not args.no_background,
                               sharpen_amount=args.sharpen,
                               crop=_crop_arg(args.crop), progress=show)
    out = Path(args.out).expanduser() if args.out else folder / "stack_lucky.fits"
    lucky_stack.write(result, out, target=args.target)
    if result.crop is not None:
        print(f"  stacked a {result.crop.w}x{result.crop.h} px window on the "
              f"body, not the whole frame")

    png = out.with_suffix(".png")
    disp = np.clip(result.stack / max(float(result.stack.max()), 1e-6), 0, 1)
    disp = disp ** args.gamma
    # Saturation on the PNG only: the FITS is what another program will work
    # from, and it stays linear and untouched.
    if abs(args.saturation - 1.0) > 1e-3:
        disp = stretch.saturate(disp, args.saturation)
    cv2.imwrite(str(png), cv2.cvtColor(stretch.to_uint8(disp),
                                       cv2.COLOR_RGB2BGR))

    if result.background is not None:
        pedestal = np.atleast_1d(result.background)
        print("  background removed: "
              + "  ".join(f"{c}={v * 100:.2f}%"
                          for c, v in zip("RGB", pedestal, strict=False)))
    print(f"\n{result.n_used} of {result.n_total} frames "
          f"({args.best:.0f}%), worst shift {result.max_shift:.1f} px, "
          f"{result.seconds:.1f} s")
    print(f"  -> {out}  and  {png}")


# ------------------------------------------------------------- post-process
def cmd_post(args) -> None:
    """Post-process a linear stack: gradient, colour, optics, stretch.

    Offline and by hand, the same way `moon` is: this runs once on a finished
    `stack_final.fits`, never per frame. See `core/postprocess.py`.
    """
    from ..core import postprocess

    fits_path = Path(args.fits).expanduser()
    rgb = postprocess.load_linear(fits_path)
    print(f"stack {rgb.shape[1]}x{rgb.shape[0]}")

    img = postprocess.process(
        rgb, crop=args.crop, mode=args.mode, tiles=args.tiles,
        align=not args.no_align, match_psf_widths=args.match_psf,
        color_method=args.color_method, color_aperture=args.color_aperture,
        deconv_iters=args.deconv, target_bg=args.target_bg,
        chroma_denoise=args.chroma_denoise, denoise=args.denoise,
        saturation=args.saturation, log=lambda msg: print(f"  {msg}"),
    )

    out = (Path(args.out).expanduser() if args.out
          else fits_path.with_name(fits_path.stem + "_post.png"))
    out.parent.mkdir(parents=True, exist_ok=True)
    encoded = (np.clip(img, 0, 1) * 65535.0 + 0.5).astype(np.uint16)
    cv2.imwrite(str(out), encoded[:, :, ::-1])
    jpg = out.with_suffix(".jpg")
    cv2.imwrite(str(jpg), stretch.to_uint8(img)[:, :, ::-1],
               [cv2.IMWRITE_JPEG_QUALITY, 95])
    print(f"wrote {out}  and  {jpg}")


# --------------------------------------------------------------- sensor survey
def cmd_sensor(args) -> None:
    """Sensor survey: find the conversion gain step and the minimum offset.

    The IMX294 has dual conversion gain: somewhere on the scale the read noise
    drops abruptly. Sitting just above that point is the camera's most important
    decision, and the exact point depends on how the manufacturer mapped the
    scale — it cannot be assumed equal to another camera with the same sensor.

    Method: pairs of bias frames (minimum exposure, sensor capped). The read
    noise comes from the standard deviation of the difference divided by root
    two, which cancels the fixed pattern and leaves only random noise.

    The ADU value grows with gain by construction, so it is normalised by the
    linear gain factor (10^(g/200), assuming 0.1 dB steps). On that normalised
    scale the step appears as a downward jump.

    Needs the sensor CAPPED and a reasonably dark room. Takes a few minutes.
    """
    cam = setup(args)
    cam.image_type = ImgType.RAW16
    g = cam.set_roi(bin=args.bin)
    cam.offset = args.offset
    cam.exposure = 0.0001
    exp_min = cam.exposure
    print(f"\nROI {g.width}x{g.height} bin{g.bin} | offset {cam.offset} | "
          f"minimum exposure {exp_min*1e6:.0f} us")
    print("CAP THE SENSOR and make sure the room is dark.")
    input("Enter when ready: ")

    def bias(n):
        out = []
        cam.start_video()
        for k in range(n + 1):
            try:
                f = cam.read_frame(timeout=5)
            except SVBError:
                continue
            if k:
                out.append(f.astype(np.float64))
        cam.stop_video()
        return out

    gains = [int(x) for x in args.gains.split(",")]
    print(f"\n{'gain':>6} {'level':>8} {'min':>6} {'noise ADU':>10} "
          f"{'normalised':>12} {'clip':>7}")
    rows = []
    for gain in gains:
        try:
            cam.gain = gain
        except ValueError as e:
            print(f"{gain:6d}  {e}")
            continue
        fr = bias(args.frames)
        if len(fr) < 2:
            print(f"{gain:6d}  no frames")
            continue
        diffs = [fr[i + 1] - fr[i] for i in range(len(fr) - 1)]
        sigma = float(np.mean([d.std() for d in diffs]) / np.sqrt(2.0))
        level = float(np.mean([f.mean() for f in fr]))
        minimum = float(min(f.min() for f in fr))
        clip = float(np.mean([(f <= 0).mean() for f in fr]) * 100)
        norm = sigma / 10 ** (gain / 200.0)
        rows.append((gain, level, minimum, sigma, norm, clip))
        print(f"{gain:6d} {level:8.0f} {minimum:6.0f} {sigma:10.2f} "
              f"{norm:12.3f} {clip:6.2f}%")

    if len(rows) >= 3:
        drops = [(rows[i][4] / rows[i - 1][4], rows[i - 1][0], rows[i][0])
                 for i in range(1, len(rows)) if rows[i - 1][4] > 0]
        ratio, before, after = min(drops, key=lambda q: q[0])
        print()
        if ratio < 0.75:
            print(f"STEP detected between gain {before} and {after}: normalised "
                  f"noise fell {(1-ratio)*100:.0f}%")
            print(f"  -> EAA preset: gain {after} or just above")
            print("     (below that you pay read noise and gain nothing)")
        else:
            print(f"no clear step; the largest drop was {(1-ratio)*100:.0f}% "
                  f"between {before} and {after}")
            print("  -> the scale probably does not expose dual conversion gain;")
            print("     use the lowest gain that still keeps the sky above the noise")

    print(f"\n=== offset at gain {args.offset_gain} ===")
    print("the offset exists so the left tail of the noise does not hit zero;")
    print("truncated, it biases the median and ruins dark subtraction\n")
    cam.gain = args.offset_gain
    print(f"{'offset':>7} {'level':>8} {'min':>6} {'at zero':>9}  status")
    best = None
    for off in [int(x) for x in args.offsets.split(",")]:
        cam.offset = off
        fr = bias(2)
        if not fr:
            continue
        level = float(np.mean([f.mean() for f in fr]))
        minimum = float(min(f.min() for f in fr))
        zeros = float(np.mean([(f <= 0).mean() for f in fr]) * 100)
        ok = zeros < 0.0001 and minimum > 0
        if ok and best is None:
            best = off
        print(f"{off:7d} {level:8.0f} {minimum:6.0f} {zeros:8.4f}%  "
              f"{'ok' if ok else 'TRUNCATING'}")
    if best is not None:
        print(f"\n  -> lowest offset without truncating: {best}; "
              f"use {best + 5} for margin")
    else:
        print("\n  -> no tested offset avoided truncation; raise the range")
    cam.close()


# ------------------------------------------------------------------- argparse
def _crop_arg(value: str) -> str | int:
    """"auto" and "full" mean themselves; anything else is a width in pixels."""
    if value in ("auto", "full"):
        return value
    try:
        return int(value)
    except ValueError:
        return "auto"


def lucky_stack_default() -> float:
    from ..core.lucky_stack import DEFAULT_BEST
    return DEFAULT_BEST * 100


def add_parsers(sub) -> None:
    s = Settings.load()

    def common(sp):
        sp.add_argument("--bin", type=int, default=s.binning,
                        help="2 is the only bin >1 without loss on this camera")
        sp.add_argument("--exp", type=float, default=s.exposure_s,
                        help="seconds")
        sp.add_argument("--gain", type=int, default=s.gain)
        sp.add_argument("--offset", type=int, default=s.offset,
                        help="offset 0 truncates the left tail of the noise")
        sp.add_argument("--target-temp", type=float, default=None)
        return sp

    def stacking(sp):
        sp.add_argument("--sigma-clip", type=float, default=3.0)
        sp.add_argument("--strictness", default="normal",
                        choices=list(LiveStacker.STRICTNESS),
                        help="how much a frame must be worth to be stacked")
        sp.add_argument("--target-bg", type=float, default=0.25)
        sp.add_argument("--linked", action="store_true",
                        help="stretch linked across channels (keeps colour ratios)")
        return sp

    b = common(sub.add_parser("bias", help="record a master bias"))
    b.add_argument("--frames", type=int, default=30,
                   help="more than a dark: a bias is cheap and its noise is "
                        "what the median has to average down")
    b.add_argument("--out", default=s.bias_dir)
    b.set_defaults(fn=cmd_bias)

    d = common(sub.add_parser("dark", help="record a master dark"))
    d.add_argument("--frames", type=int, default=20)
    d.add_argument("--out", default=s.dark_dir)
    d.set_defaults(fn=cmd_dark)

    fl = common(sub.add_parser("flat", help="record a master flat"))
    fl.add_argument("--frames", type=int, default=16)
    fl.add_argument("--bias", default=None,
                    help="master bias — the pedestal to remove from the flat")
    fl.add_argument("--dark", default=None,
                    help="a dark of the flat's own exposure, if you have one; "
                         "--bias is the usual answer")
    fl.add_argument("--out", default=s.flat_dir)
    fl.set_defaults(fn=cmd_flat)

    r = stacking(common(sub.add_parser("run", help="headless stacking session")))
    r.add_argument("--bias", default=None,
                   help="applied only when there is no --dark: a dark already "
                        "contains the bias")
    r.add_argument("--dark", default=None)
    r.add_argument("--flat", default=None,
                   help="corrects vignetting; one per bin serves every gain")
    r.add_argument("--out", default=s.export_dir)
    r.add_argument("--ref-refresh", type=int, default=10)
    r.add_argument("--min-ref-stars", type=int, default=10,
                   help="stars required to start the reference frame")
    r.add_argument("--preview-every", type=int, default=1)
    r.add_argument("--max-frames", type=int, default=0,
                   help="stop after N frames (0 = no limit)")
    r.set_defaults(fn=cmd_run)

    rp = stacking(sub.add_parser("replay", help="reprocess a recorded session"))
    rp.add_argument("folder")
    rp.add_argument("--speed", type=float, default=0.0,
                    help="0 = as fast as possible; 1 = real time")
    rp.add_argument("--bias", default=None)
    rp.add_argument("--dark", default=None)
    rp.add_argument("--flat", default=None)
    rp.add_argument("--out", default=s.export_dir)
    rp.set_defaults(fn=cmd_replay)

    mn = sub.add_parser("lucky",
                        help="stack a Moon or planet burst (sharpest frames, "
                             "aligned)")
    mn.add_argument("folder", help="a burst folder written by PLANETS")
    mn.add_argument("--best", type=float, default=lucky_stack_default(),
                    help="percent of the burst to keep, sharpest first")
    mn.add_argument("--bias", default=None,
                    help="stands in for the dark: a burst is milliseconds long")
    mn.add_argument("--dark", default=None)
    mn.add_argument("--flat", default=None,
                    help="corrects vignetting; one per bin serves every gain")
    mn.add_argument("--no-background", action="store_true",
                    help="keep the additive pedestal (offset plus scattered "
                         "light) instead of measuring and removing it")
    mn.add_argument("--sharpen", type=float, default=0.0,
                    help="unsharp mask amount, 0.5-1.0 is usual; off by "
                         "default because it is a choice, not a correction")
    mn.add_argument("--gamma", type=float, default=s.lucky_gamma,
                    help="display exponent of the PNG; the FITS stays linear")
    mn.add_argument("--saturation", type=float, default=1.0,
                    help="chroma of the PNG only; 2-3 is the mineral Moon")
    mn.add_argument("--crop", default="auto",
                    help="auto: a window around the body, unless the body is "
                         "most of the frame (the Moon); full: the whole frame; "
                         "a number: that width in px, centred on the body")
    mn.add_argument("--target", default="Moon")
    mn.add_argument("--out", default=None,
                    help="default: stack_lucky.fits inside the burst folder")
    mn.set_defaults(fn=cmd_lucky)

    pp = sub.add_parser("post",
                        help="post-process a linear stack (gradient, colour, "
                             "optics, stretch)")
    pp.add_argument("fits", help="a stack_final.fits from run/replay")
    pp.add_argument("--crop", type=int, default=-1,
                    help="fixed border in px; -1 measures it from the noise")
    pp.add_argument("--mode", choices=("divide", "subtract"), default="divide",
                    help="gradient correction: multiplicative or additive")
    pp.add_argument("--tiles", type=int, default=40)
    pp.add_argument("--no-align", action="store_true",
                    help="skip the atmospheric-dispersion channel alignment")
    pp.add_argument("--match-psf", action="store_true",
                    help="equalise the per-channel PSF width")
    pp.add_argument("--color-method", choices=("median", "flux"),
                    default="median",
                    help="robust median of per-star ratios, or summed flux")
    pp.add_argument("--color-aperture", type=int, default=3,
                    help="radius in px for the colour-calibration photometry")
    pp.add_argument("--deconv", type=int, default=0,
                    help="RL iterations on the luminance, 0 = off")
    pp.add_argument("--target-bg", type=float, default=0.25)
    pp.add_argument("--chroma-denoise", type=float, default=2.0)
    pp.add_argument("--denoise", type=float, default=0.0,
                    help="luminance denoise strength on the sky, 0 = off")
    pp.add_argument("--saturation", type=float, default=1.6)
    pp.add_argument("--out", default=None,
                    help="default: <fits>_post.png next to the input")
    pp.set_defaults(fn=cmd_post)

    sn = common(sub.add_parser("sensor",
                               help="find the gain step and minimum offset"))
    sn.add_argument("--gains",
                    default="0,60,100,110,120,130,140,180,240,300,400,500,570")
    sn.add_argument("--offsets", default="0,5,10,15,20,30,40")
    sn.add_argument("--offset-gain", type=int, default=120)
    sn.add_argument("--frames", type=int, default=6)
    sn.set_defaults(fn=cmd_sensor)
