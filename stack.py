#!/usr/bin/env python3
"""Live stacking para EAA — captura e empilha em tempo real.

  # master dark (tampe o sensor!)
  ./stack.py dark --exp 5 --gain 250 --frames 20 --out darks/

  # sessão de live stacking
  ./stack.py run --exp 5 --gain 250 --dark darks/dark_g250_e5.0s_-10C.fits

Durante a sessão: Ctrl-C encerra e salva. Enter em branco marca um novo
segmento (use depois de resetar a plataforma equatorial ou recentrar o tubo).
"""
from __future__ import annotations

import argparse
import queue
import select
import sys
import threading
import time
from pathlib import Path

import cv2
import numpy as np
from astropy.io import fits

sys.path.insert(0, str(Path(__file__).resolve().parent))
from octans import debayer, stretch                      # noqa: E402
from octans.stacker import LiveStacker                   # noqa: E402
from svbony.camera import Camera, list_cameras              # noqa: E402
from svbony.sdk import ImgType, SVBError                    # noqa: E402


# --------------------------------------------------------------------- câmera
def setup(args) -> Camera:
    cams = list_cameras()
    if not cams:
        sys.exit("nenhuma câmera encontrada (AstroDMx aberto? Parallels com o USB?)")
    cam = Camera(cams[0]).open()
    print(f"{cam.id.name}  fw {cam.firmware}  porta {cam.id.port}")
    if cam.id.port != "USB3.0":
        print(f"  aviso: negociado em {cam.id.port} — ~475 ms de tempo morto por "
              f"frame. Troque o cabo se puder.")
    if cam.forced_linear:
        print(f"  pipeline linearizado: {', '.join(cam.forced_linear)}")

    cam.image_type = ImgType.RAW16
    g = cam.set_roi(bin=args.bin)
    cam.gain = args.gain
    cam.exposure = args.exp
    cam.offset = args.offset

    if args.target_temp is not None and cam.supports_cooler:
        cam.target_temperature = args.target_temp
        cam.cooler = True
        print(f"  TEC ligado, alvo {args.target_temp:+.1f} °C "
              f"(sensor a {cam.temperature:+.1f} °C agora)")

    print(f"  ROI {g.width}x{g.height} bin{g.bin} RAW16 | ganho {cam.gain} | "
          f"exp {cam.exposure:.2f}s | offset {cam.offset} | escala 0..{cam.full_scale}")
    return cam


def capture_loop(cam: Camera, q: queue.Queue, stop: threading.Event) -> None:
    """Thread de captura. ctypes libera a GIL no SVBGetVideoData, então isto
    não bloqueia o empilhamento."""
    cam.start_video()
    first = True
    while not stop.is_set():
        try:
            f = cam.read_frame(timeout=cam.exposure * 3 + 8)
        except SVBError as e:
            if stop.is_set():
                break
            print(f"\n  captura: {e}")
            continue
        if first:                    # o primeiro frame após start vem stale
            first = False
            continue
        try:
            q.put_nowait((f, time.time()))
        except queue.Full:
            try:                     # descarta o mais antigo: preferimos frame novo
                q.get_nowait()
                q.put_nowait((f, time.time()))
            except queue.Empty:
                pass
    try:
        cam.stop_video()
    except SVBError:
        pass


# --------------------------------------------------------------------- dark
def cmd_dark(args):
    out = Path(args.out); out.mkdir(parents=True, exist_ok=True)
    cam = setup(args)
    if cam.supports_cooler and args.target_temp is not None:
        # mesma rampa da interface: mandar o alvo de uma vez faz o TEC puxar
        # 100% e a temperatura despencar
        from octans.cooling import CoolerController
        ctrl = CoolerController(ramp_c_per_min=2.0)
        ctrl.set_target(args.target_temp, current=cam.temperature)
        print(f"\nrampa até {args.target_temp:+.1f} °C a 2 °C/min...")
        limite = time.time() + 45 * 60
        while time.time() < limite:
            sp, ligar = ctrl.update(cam.temperature, cam.cooler_power, time.time())
            if sp is not None:
                cam.target_temperature = float(sp)
            cam.cooler = ligar
            st = ctrl.state
            print(f"  {st.message[:74]}   ", end="\r")
            if st.phase == "stable":
                break
            if st.phase == "saturated":
                print(f"\n  {st.message}")
                break
            time.sleep(3)
        print(f"\n  sensor a {cam.temperature:+.1f} °C, TEC {cam.cooler_power}%")

    print(f"\nTAMPE O SENSOR. capturando {args.frames} darks de {cam.exposure:.1f}s...")
    input("Enter quando estiver tampado: ")

    cam.start_video()
    frames = []
    for k in range(args.frames + 1):
        try:
            f = cam.read_frame(timeout=cam.exposure * 3 + 8)
        except SVBError as e:
            print(f"  frame {k}: {e}"); continue
        if k == 0:
            continue
        frames.append(f)
        print(f"  {len(frames)}/{args.frames}  mediana={np.median(f):.0f} "
              f"max={f.max()}", end="\r")
    cam.stop_video()
    print()

    stack = np.median(np.stack(frames), axis=0).astype(np.float32)
    temp = cam.temperature
    name = f"dark_g{cam.gain}_e{cam.exposure:.1f}s_{temp:+.0f}C.fits"
    hdu = fits.PrimaryHDU(stack)
    hdu.header["IMAGETYP"] = "DARK"
    hdu.header["EXPTIME"] = cam.exposure
    hdu.header["GAIN"] = cam.gain
    hdu.header["OFFSET"] = cam.offset
    hdu.header["XBINNING"] = cam.geometry.bin
    hdu.header["CCD-TEMP"] = round(temp, 1)
    hdu.header["NCOMBINE"] = len(frames)
    hdu.writeto(out / name, overwrite=True)
    print(f"master dark -> {out / name}")
    print(f"  mediana {np.median(stack):.1f}  sigma {stack.std():.1f}  max {stack.max():.0f}")

    # mapa de pixels quentes: persistentemente muito acima do fundo
    med, mad = np.median(stack), np.median(np.abs(stack - np.median(stack))) * 1.4826
    hot = stack > med + 12 * max(mad, 1.0)
    np.save(out / name.replace(".fits", "_hot.npy"), hot)
    print(f"  mapa de pixels quentes: {hot.sum()} px ({100*hot.mean():.4f}%) -> "
          f"{name.replace('.fits', '_hot.npy')}")
    cam.close()


# --------------------------------------------------------------------- sessão
def cmd_run(args):
    out = Path(args.out); out.mkdir(parents=True, exist_ok=True)
    cam = setup(args)

    dark = hot = None
    if args.dark:
        dark = fits.getdata(args.dark).astype(np.float32)
        print(f"  dark: {args.dark} (mediana {np.median(dark):.0f})")
        hp = Path(str(args.dark).replace(".fits", "_hot.npy"))
        if hp.exists():
            hot = np.load(hp)
            print(f"  mapa de pixels quentes: {hot.sum()} px")

    g = cam.geometry
    st = LiveStacker((g.height, g.width), channels=3,
                     sigma_clip=args.sigma_clip,
                     # saturação na escala da luminância, que soma a quadra 2x2
                     saturation=debayer.LUM_SUM,
                     ref_refresh=args.ref_refresh,
                     min_ref_stars=args.min_ref_stars)

    q: queue.Queue = queue.Queue(maxsize=2)
    stop = threading.Event()
    th = threading.Thread(target=capture_loop, args=(cam, q, stop), daemon=True)
    th.start()

    print(f"\nempilhando. Ctrl-C encerra; Enter marca novo segmento.\n")
    print(f"{'#':>4} {'estr':>5} {'FWHM':>6} {'par':>4} {'rms':>5} {'rot°':>7} "
          f"{'deriva':>14} {'cob%':>5} {'integr':>8} {'ms':>6}  situação")
    t_start = time.time()
    try:
        while True:
            # só olha o teclado se houver terminal: com a saída redirecionada,
            # select reporta stdin pronto em EOF e isto dispararia novo segmento
            # a cada frame
            if sys.stdin.isatty() and select.select([sys.stdin], [], [], 0)[0]:
                sys.stdin.readline()
                st.new_segment()
                print("  --- novo segmento: limiares relaxados, referência será "
                      "reextraída do stack")
            try:
                raw, ts = q.get(timeout=1.0)
            except queue.Empty:
                continue

            f = raw.astype(np.float32)
            if dark is not None:
                f -= dark
                np.maximum(f, 0.0, out=f)
            if hot is not None:
                f = _fix_hot(f, hot)
            f /= cam.full_scale

            rgb = debayer.to_rgb((np.clip(f, 0, 1) * 65535).astype(np.uint16),
                                 cam.bayer, quality="linear").astype(np.float32) / 65535.0
            lum = debayer.cfa_to_luminance(f)

            o = st.add(rgb, lum, exposure=cam.exposure, lum_scale=2.0)
            al = o.alignment
            dx, dy = st.drift()
            print(f"{st.n_stacked:4d} {o.n_stars:5d} {o.fwhm:6.2f} "
                  f"{(al.n_matched if al else 0):4d} "
                  f"{(f'{al.rms:.2f}' if al and np.isfinite(al.rms) else '-'):>5} "
                  f"{(f'{al.rotation_deg:+.3f}' if al else '-'):>7} "
                  f"{f'{dx:+6.1f},{dy:+6.1f}':>14} "
                  f"{st.overlap_fraction()*100:5.1f} "
                  f"{st.total_exposure:7.0f}s {o.elapsed_ms:6.0f}  "
                  f"{'ok' if o.accepted else 'rej: ' + o.reason}")

            if o.accepted and st.n_stacked % args.preview_every == 0:
                _write_preview(st, out, args)
            if not o.accepted:
                # Frame rejeitado ou stack ainda não iniciado: você continua
                # precisando ver a imagem ao vivo para enquadrar, focar e
                # entender por que está sendo rejeitado.
                _write_frame_preview(rgb, out, args)
            if args.max_frames and len(st.history) >= args.max_frames:
                print("  --- limite de frames atingido")
                break
    except KeyboardInterrupt:
        print("\nencerrando...")
    finally:
        stop.set(); th.join(timeout=5)
        if st.n_stacked:
            _write_preview(st, out, args, final=True)
            print(f"\n{st.n_stacked} frames, {st.total_exposure:.0f}s de integração, "
                  f"{st.n_rejected} rejeitados, {time.time()-t_start:.0f}s de sessão")
        cam.close()


def _fix_hot(f: np.ndarray, hot: np.ndarray) -> np.ndarray:
    """Substitui pixels quentes pela mediana dos vizinhos de MESMA cor.

    Usar vizinhos imediatos misturaria canais do mosaico Bayer e criaria
    artefatos de cor no lugar do pixel quente.
    """
    out = f.copy()
    med = cv2.medianBlur(f, 3)
    for dy in (0, 1):
        for dx in (0, 1):
            ph_hot = hot[dy::2, dx::2]
            ph = f[dy::2, dx::2]
            ph_med = cv2.medianBlur(np.ascontiguousarray(ph), 3)
            sub = out[dy::2, dx::2]
            sub[ph_hot] = ph_med[ph_hot]
            out[dy::2, dx::2] = sub
    return out


def _write_frame_preview(rgb: np.ndarray, out: Path, args) -> None:
    disp = stretch.autostretch(rgb, target_bg=args.target_bg, linked=args.linked)
    cv2.imwrite(str(out / "live.png"),
                cv2.cvtColor(stretch.to_uint8(disp), cv2.COLOR_RGB2BGR))


def _write_preview(st: LiveStacker, out: Path, args, final: bool = False) -> None:
    img = st.result()
    disp = stretch.autostretch(img, target_bg=args.target_bg, linked=args.linked)
    u8 = stretch.to_uint8(disp)
    name = "stack_final.png" if final else "stack.png"
    cv2.imwrite(str(out / name), cv2.cvtColor(u8, cv2.COLOR_RGB2BGR))
    if final:
        hdu = fits.PrimaryHDU(img.transpose(2, 0, 1).astype(np.float32))
        hdu.header["NCOMBINE"] = st.n_stacked
        hdu.header["EXPTOTAL"] = st.total_exposure
        hdu.writeto(out / "stack_final.fits", overwrite=True)
        print(f"  -> {out/name}  e  {out/'stack_final.fits'}")


def cmd_sensor(args):
    """Análise de sensor: encontra o salto de ganho de conversão e o offset mínimo.

    O IMX294 tem ganho de conversão dupla: em algum ponto da escala o ruído de
    leitura cai de forma abrupta. Ficar logo acima desse ponto é a decisão mais
    importante da câmera, e o ponto exato depende de como o fabricante mapeou a
    escala — não dá para assumir que é igual ao de outra câmera com o mesmo
    sensor.

    Método: pares de frames de bias (exposição mínima, sensor tampado). O ruído
    de leitura sai do desvio padrão da diferença dividido por raiz de dois, o que
    cancela o padrão fixo e deixa só o ruído aleatório.

    O valor em ADU cresce com o ganho por construção, então ele é normalizado
    pelo fator de ganho linear (10^(g/200), assumindo passos de 0,1 dB). Nessa
    escala normalizada o salto aparece como um degrau para baixo.

    Precisa do sensor TAMPADO e de escuro razoável. Leva poucos minutos.
    """
    cam = setup(args)
    cam.image_type = ImgType.RAW16
    g = cam.set_roi(bin=args.bin)
    cam.offset = args.offset
    exp_min = 0.0001
    cam.exposure = exp_min
    exp_min = cam.exposure
    print(f"\nROI {g.width}x{g.height} bin{g.bin} | offset {cam.offset} | "
          f"exposição mínima {exp_min*1e6:.0f} us")
    print("TAMPE O SENSOR e garanta escuro no ambiente.")
    input("Enter quando estiver pronto: ")

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

    ganhos = [int(x) for x in args.gains.split(",")]
    print(f"\n{'ganho':>6} {'nível':>8} {'mín':>6} {'ruído ADU':>10} "
          f"{'normalizado':>12} {'clip':>7}")
    linhas = []
    for gain in ganhos:
        try:
            cam.gain = gain
        except ValueError as e:
            print(f"{gain:6d}  {e}")
            continue
        fr = bias(args.frames)
        if len(fr) < 2:
            print(f"{gain:6d}  sem frames")
            continue
        difs = [fr[i + 1] - fr[i] for i in range(len(fr) - 1)]
        sigma = float(np.mean([d.std() for d in difs]) / np.sqrt(2.0))
        nivel = float(np.mean([f.mean() for f in fr]))
        minimo = float(min(f.min() for f in fr))
        clip = float(np.mean([(f <= 0).mean() for f in fr]) * 100)
        fator = 10 ** (gain / 200.0)
        norm = sigma / fator
        linhas.append((gain, nivel, minimo, sigma, norm, clip))
        print(f"{gain:6d} {nivel:8.0f} {minimo:6.0f} {sigma:10.2f} "
              f"{norm:12.3f} {clip:6.2f}%")

    if len(linhas) >= 3:
        quedas = [(linhas[i][4] / linhas[i - 1][4], linhas[i - 1][0], linhas[i][0])
                  for i in range(1, len(linhas)) if linhas[i - 1][4] > 0]
        razao, antes, depois = min(quedas, key=lambda q: q[0])
        print()
        if razao < 0.75:
            print(f"SALTO detectado entre ganho {antes} e {depois}: ruído "
                  f"normalizado caiu {(1-razao)*100:.0f}%")
            print(f"  -> preset de EAA: ganho {depois} ou pouco acima")
            print(f"     (abaixo disso você paga ruído de leitura sem ganhar nada)")
        else:
            print(f"nenhum salto claro; maior queda foi {(1-razao)*100:.0f}% "
                  f"entre {antes} e {depois}")
            print("  -> escala provavelmente sem ganho de conversão dupla exposto;")
            print("     use o menor ganho que ainda deixe o céu acima do ruído")

    # ------------------------------------------------------------ offset
    print(f"\n=== offset no ganho {args.offset_gain} ===")
    print("o offset existe para a cauda esquerda do ruído não bater em zero;")
    print("truncada, ela enviesa a mediana e estraga a subtração do dark\n")
    cam.gain = args.offset_gain
    print(f"{'offset':>7} {'nível':>8} {'mín':>6} {'em zero':>9}  situação")
    melhor = None
    for off in [int(x) for x in args.offsets.split(",")]:
        cam.offset = off
        fr = bias(2)
        if not fr:
            continue
        nivel = float(np.mean([f.mean() for f in fr]))
        minimo = float(min(f.min() for f in fr))
        zeros = float(np.mean([(f <= 0).mean() for f in fr]) * 100)
        ok = zeros < 0.0001 and minimo > 0
        if ok and melhor is None:
            melhor = off
        print(f"{off:7d} {nivel:8.0f} {minimo:6.0f} {zeros:8.4f}%  "
              f"{'ok' if ok else 'TRUNCANDO'}")
    if melhor is not None:
        print(f"\n  -> menor offset sem truncar: {melhor}; use {melhor + 5} "
              f"por margem")
    else:
        print("\n  -> nenhum offset testado evitou o truncamento; suba a faixa")
    cam.close()


def cmd_flat(args):
    """Master flat: superfície uniformemente iluminada, mediana de N frames.

    Ilumine uniformemente a abertura (céu do crepúsculo, painel de flat, camiseta
    branca esticada). Ajuste a exposição para o histograma ficar por volta de
    metade da escala — flat saturado é inútil, e flat escuro demais carrega ruído
    para dentro dos seus dados.

    Passe --dark de mesma exposição e ganho: o pedestal de offset do sensor
    entraria como erro multiplicativo no flat.
    """
    out = Path(args.out); out.mkdir(parents=True, exist_ok=True)
    cam = setup(args)
    dark = None
    if args.dark:
        from octans.recorder import read_fits
        dark, _ = read_fits(args.dark)
        dark = dark.astype(np.float32)
        print(f"dark do flat: {args.dark} (mediana {np.median(dark):.0f})")
    else:
        print("aviso: sem --dark, o pedestal de offset fica embutido no flat")

    print(f"\ncapturando {args.frames} flats de {cam.exposure:.3f}s...")
    cam.start_video()
    frames = []
    for k in range(args.frames + 1):
        try:
            f = cam.read_frame(timeout=cam.exposure * 3 + 8)
        except SVBError as e:
            print(f"  frame {k}: {e}"); continue
        if k == 0:
            continue
        frames.append(f.astype(np.float32))
        med = np.median(frames[-1])
        print(f"  {len(frames)}/{args.frames}  mediana={med:.0f} "
              f"({med/cam.full_scale*100:.0f}% da escala)"
              + ("  SATURANDO" if f.max() >= cam.full_scale * 0.98 else ""), end="\r")
    cam.stop_video()
    print()
    if not frames:
        sys.exit("nenhum flat capturado")

    stack = np.median(np.stack(frames), axis=0)
    if dark is not None and dark.shape == stack.shape:
        stack = np.maximum(stack - dark, 1.0)
    m = float(np.median(stack))
    name = f"flat_g{cam.gain}_bin{cam.geometry.bin}.fits"
    hdu = fits.PrimaryHDU(stack.astype(np.float32))
    hdu.header["IMAGETYP"] = "FLAT"
    hdu.header["EXPTIME"] = cam.exposure
    hdu.header["GAIN"] = cam.gain
    hdu.header["XBINNING"] = cam.geometry.bin
    hdu.header["NCOMBINE"] = len(frames)
    hdu.header["BAYERPAT"] = cam.bayer.fits_name
    hdu.writeto(out / name, overwrite=True)
    norm = stack / m
    print(f"master flat -> {out / name}")
    print(f"  mediana {m:.0f} ({m/cam.full_scale*100:.0f}% da escala)")
    print(f"  vinheteamento: canto a {norm.min()*100:.0f}% do centro, "
          f"variação {norm.min()*100:.0f}-{norm.max()*100:.0f}%")
    cam.close()


def cmd_replay(args):
    """Reproduz uma sessão gravada pelo mesmo pipeline da câmera.

    É o que permite desenvolver e ajustar limiares de dia: uma noite de captura
    vira quantas iterações você quiser.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from octans.source import ReplaySource
    from octans.stacker import LiveStacker
    from octans import debayer

    out = Path(args.out); out.mkdir(parents=True, exist_ok=True)
    src = ReplaySource(args.folder, speed=args.speed, loop=False)
    info = src.open()
    print(f"{info['name']}  {info['width']}x{info['height']} bin{info['bin']}  "
          f"Bayer {info['bayer']}  escala 0..{info['full_scale']}  "
          f"{info['n_frames']} frames")

    dark = None
    if args.dark:
        from octans.recorder import read_fits
        dark, _ = read_fits(args.dark)
        dark = dark.astype(np.float32)
        print(f"dark: {args.dark}")

    st = LiveStacker((info["height"], info["width"]), channels=3,
                     sigma_clip=args.sigma_clip, saturation=debayer.LUM_SUM)
    src.start()
    print(f"\n{'#':>4} {'estr':>5} {'HFR':>6} {'par':>4} {'rms':>5} {'rot°':>8} "
          f"{'integr':>8}  situação")
    while True:
        got = src.read()
        if got is None:
            break
        raw, meta = got
        f = raw.astype(np.float32)
        if dark is not None and dark.shape == f.shape:
            f -= dark
            np.maximum(f, 0.0, out=f)
        f /= max(meta.full_scale, 1)
        np.clip(f, 0.0, 1.0, out=f)
        rgb = debayer.to_rgb((f * 65535).astype(np.uint16), meta.bayer,
                             quality="linear").astype(np.float32) / 65535.0
        o = st.add(rgb, debayer.cfa_to_luminance(f), exposure=meta.exposure,
                   lum_scale=2.0)
        al = o.alignment
        print(f"{st.n_stacked:4d} {o.n_stars:5d} {o.hfr:6.2f} "
              f"{(al.n_matched if al else 0):4d} "
              f"{(f'{al.rms:.2f}' if al and np.isfinite(al.rms) else '-'):>5} "
              f"{(f'{al.rotation_deg:+.3f}' if al else '-'):>8} "
              f"{st.total_exposure:7.0f}s  {'ok' if o.accepted else 'rej: ' + o.reason}")
    if st.n_stacked:
        _write_preview(st, out, args, final=True)
        print(f"\n{st.n_stacked} frames, {st.total_exposure:.0f}s, "
              f"{st.n_rejected} rejeitados")


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp):
        sp.add_argument("--bin", type=int, default=2,
                        help="2 é o único bin >1 sem perda nesta câmera")
        sp.add_argument("--exp", type=float, default=5.0, help="segundos")
        sp.add_argument("--gain", type=int, default=250)
        sp.add_argument("--offset", type=int, default=20,
                        help="offset 0 trunca a cauda esquerda do ruído")
        sp.add_argument("--target-temp", type=float, default=None)
        return sp

    d = common(sub.add_parser("dark"))
    d.add_argument("--frames", type=int, default=20)
    d.add_argument("--out", default="./darks")
    d.set_defaults(fn=cmd_dark)

    r = common(sub.add_parser("run"))
    r.add_argument("--dark", default=None)
    r.add_argument("--out", default="./session")
    r.add_argument("--sigma-clip", type=float, default=3.0)
    r.add_argument("--ref-refresh", type=int, default=10)
    r.add_argument("--min-ref-stars", type=int, default=10,
                   help="estrelas mínimas para iniciar o referencial")
    r.add_argument("--preview-every", type=int, default=1)
    r.add_argument("--target-bg", type=float, default=0.25)
    r.add_argument("--max-frames", type=int, default=0,
                   help="encerra após N frames (0 = sem limite)")
    r.add_argument("--linked", action="store_true",
                   help="stretch ligado entre canais (preserva cor original)")
    r.set_defaults(fn=cmd_run)

    sn = common(sub.add_parser("sensor"))
    sn.add_argument("--gains", default="0,60,100,110,120,130,140,180,240,300,400,500,570")
    sn.add_argument("--offsets", default="0,5,10,15,20,30,40")
    sn.add_argument("--offset-gain", type=int, default=120)
    sn.add_argument("--frames", type=int, default=6)
    sn.set_defaults(fn=cmd_sensor)

    fl = common(sub.add_parser("flat"))
    fl.add_argument("--frames", type=int, default=16)
    fl.add_argument("--dark", default=None, help="dark de mesma exposição e ganho")
    fl.add_argument("--out", default="./flats")
    fl.set_defaults(fn=cmd_flat)

    rp = sub.add_parser("replay")
    rp.add_argument("folder")
    rp.add_argument("--speed", type=float, default=0.0,
                    help="0 = o mais rápido possível; 1 = tempo real")
    rp.add_argument("--dark", default=None)
    rp.add_argument("--out", default="./replay")
    rp.add_argument("--sigma-clip", type=float, default=3.0)
    rp.add_argument("--target-bg", type=float, default=0.25)
    rp.add_argument("--linked", action="store_true")
    rp.set_defaults(fn=cmd_replay)

    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
