"""Prova de ponta a ponta do stacker, sem câmera.

Simula um campo estelar com deriva e rotação residual (plataforma equatorial
mal alinhada), ruído de Poisson, um satélite atravessando, e um salto grande de
campo no meio da sequência — o que acontece quando você reseta a plataforma ou
encosta no tubo de um dobsoniano sem goto.
"""
import sys
from pathlib import Path

# relativo ao próprio arquivo: caminho absoluto quebra ao renomear o projeto
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
from octans.stacker import LiveStacker
from octans import stretch

H = W = 500
RNG = np.random.default_rng(7)
STARS = np.column_stack([RNG.uniform(20, W - 20, 130), RNG.uniform(20, H - 20, 130)])
# fluxos numa faixa ampla: as fracas são as que só o stack revela
FLUX = 10 ** RNG.uniform(1.5, 3.4, len(STARS))
SKY = 120.0
SIGMA_PSF = 1.9


def render(shift, rot_deg, satellite=False):
    th = np.deg2rad(rot_deg)
    R = np.array([[np.cos(th), -np.sin(th)], [np.sin(th), np.cos(th)]])
    c = np.array([W / 2, H / 2])
    xy = (STARS - c) @ R.T + c + np.asarray(shift, dtype=float)

    yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
    img = np.full((H, W), SKY, dtype=np.float32)
    for (x, y), f in zip(xy, FLUX):
        if -10 < x < W + 10 and -10 < y < H + 10:
            img += f * np.exp(-((xx - x) ** 2 + (yy - y) ** 2) / (2 * SIGMA_PSF ** 2))
    if satellite:
        t = np.linspace(0, 1, 900)
        for u in t:
            px, py = int(30 + u * 440), int(470 - u * 430)
            img[max(py - 1, 0):py + 2, max(px - 1, 0):px + 2] += 900.0
    return RNG.poisson(np.clip(img, 0, None)).astype(np.float32)


def measure_snr(img, xy_true):
    """SNR mediano das estrelas fracas: pico acima do fundo / ruído do fundo."""
    bg_patch = img[5:60, 5:60]
    noise = float(bg_patch.std())
    bg = float(np.median(img))
    faint = np.argsort(FLUX)[:15]
    snrs = []
    for i in faint:
        x, y = xy_true[i]
        xi, yi = int(round(x)), int(round(y))
        if 6 < xi < W - 6 and 6 < yi < H - 6:
            peak = float(img[yi - 2:yi + 3, xi - 2:xi + 3].max())
            snrs.append((peak - bg) / max(noise, 1e-6))
    return float(np.median(snrs)), noise


def run():
    N = 24
    # deriva lenta + rotação residual, e um salto grande no frame 12
    frames = []
    for k in range(N):
        drift = np.array([k * 0.8, -k * 0.55])
        rot = k * 0.045
        if k >= 12:
            drift = drift + np.array([180.0, -140.0])   # reset da plataforma
            rot += 1.3
        frames.append((render(drift, rot, satellite=(k == 7)), drift, rot))

    single = frames[0][0]
    snr1, noise1 = measure_snr(single, STARS)

    st = LiveStacker((H, W), channels=1, ref_refresh=6, min_matched=8,
                     max_rms=2.0, sigma_clip=3.0)

    print(f"{'frame':>5} {'estrelas':>9} {'FWHM':>6} {'pares':>6} {'rms':>6} "
          f"{'rot°':>7} {'dx,dy':>16}  situação")
    reset_flagged = False
    for k, (img, drift, rot) in enumerate(frames):
        if k == 12:
            st.new_segment()
            reset_flagged = True
        rgb = img[:, :, None]
        lum = img            # mono: luminância é a própria imagem, escala 1:1
        o = st.add(rgb, lum, exposure=5.0, lum_scale=1.0)
        al = o.alignment
        pares = al.n_matched if al else 0
        rms = f"{al.rms:.2f}" if al and np.isfinite(al.rms) else "-"
        rotd = f"{al.rotation_deg:+.3f}" if al else "-"
        sh = f"{al.shift[0]:+7.1f},{al.shift[1]:+6.1f}" if al else "-"
        mark = "  <-- RESET" if k == 12 else ""
        print(f"{k:5d} {o.n_stars:9d} {o.fwhm:6.2f} {pares:6d} {rms:>6} {rotd:>7} {sh:>16}  "
              f"{'aceito' if o.accepted else 'REJEITADO: ' + o.reason}{mark}")

    stack = st.result()[:, :, 0]
    # renormaliza para a mesma escala do frame único, para comparar SNR
    stack_adu = stack * (st.accum / np.maximum(st.weight, 1e-6)[..., None]).max()
    snrN, noiseN = measure_snr(stack_adu, STARS)

    print(f"\nempilhados {st.n_stacked}/{N}, rejeitados {st.n_rejected}, "
          f"integração {st.total_exposure:.0f}s")
    print(f"sobreposição >=90%: {st.overlap_fraction()*100:.1f}% do quadro")
    print(f"\nSNR mediano das 15 estrelas mais fracas:")
    print(f"  frame único : {snr1:7.2f}   (ruído de fundo {noise1:.2f})")
    print(f"  stack       : {snrN:7.2f}   (ruído de fundo {noiseN:.2f})")
    print(f"  ganho medido: {snrN/snr1:7.2f}x   esperado ~sqrt({st.n_stacked}) = "
          f"{np.sqrt(st.n_stacked):.2f}x")

    # o satélite deve ter sido removido pelo sigma clipping
    diag = np.array([stack_adu[470 - int(u * 430), 30 + int(u * 440)]
                     for u in np.linspace(0.15, 0.85, 60)])
    bg = float(np.median(stack_adu))
    print(f"\ntraço do satélite: mediana na diagonal = {np.median(diag):.1f} "
          f"vs fundo {bg:.1f}  -> {'removido' if np.median(diag) < bg * 1.35 else 'AINDA VISÍVEL'}")

    assert st.n_stacked >= N - 2, f"empilhou só {st.n_stacked}"
    assert reset_flagged and st.n_stacked > 12, "não sobreviveu ao reset da plataforma"
    assert snrN / snr1 > np.sqrt(st.n_stacked) * 0.55, "ganho de SNR abaixo do esperado"
    print("\nOK")


if __name__ == "__main__":
    run()
