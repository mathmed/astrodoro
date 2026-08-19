"""Thread de captura, calibração, empilhamento, foco e gravação.

A GUI só renderiza o que sai daqui. Mudanças de parâmetro entram por um
dicionário protegido por lock e são aplicadas entre frames — não por slots do
Qt, porque o laço fica bloqueado dentro do SVBGetVideoData durante toda a
exposição e o event loop desta thread não rodaria.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from PySide6.QtCore import QObject, Signal

from octans import debayer
from octans.cooling import CoolerController
from octans.focus import FocusMeter, brightest_usable, loupe
from octans.platform import PlatformMonitor
from octans.recorder import Recorder, read_fits
from octans.source import CameraSource, FrameMeta, ReplaySource
from octans.stacker import LiveStacker
from octans.stars import detect
from svbony.sdk import SVBError


@dataclass
class Config:
    # O modo decide o que fazer com os frames, não só o que exibir:
    #   frame/focus -> captura + detecção de estrelas (para HFR). Sem empilhar,
    #                  sem gravar, sem amostrar rotação.
    #   stack/adjust-> PERMITEM integrar, mas não iniciam sozinhos. Quem inicia
    #                  é o usuário, explicitamente.
    mode: str = "frame"
    # fonte
    source: str = "camera"            # "camera" | "replay"
    camera_index: int = 0
    replay_folder: str = ""
    replay_speed: float = 0.0
    replay_loop: bool = False
    # câmera
    bin: int = 2
    exposure: float = 5.0
    gain: int = 250
    offset: int = 20
    target_temp: float | None = None
    # processamento
    dark_path: str | None = None
    flat_path: str | None = None
    sigma_clip: float | None = 3.0
    ref_refresh: int = 10
    min_ref_stars: int = 10
    # gravação
    record: bool = True
    record_root: str = "sessions"
    target_name: str = ""
    record_every: int = 1
    compress: bool = True
    quality_weighting: bool = True
    rigor: str = "normal"
    cooling_ramp: float = 2.0
    warm_on_finish: bool = True
    # plataforma
    platform_minutes: float = 60.0


class CaptureWorker(QObject):
    frame = Signal(object, object, dict)    # live_rgb, stack|None, stats
    focus = Signal(object, dict)            # loupe|None, dados de foco
    log = Signal(str)
    failed = Signal(str)
    opened = Signal(dict)
    paused = Signal(bool)
    cooling = Signal(dict)
    dark_saved = Signal(str)
    finished = Signal()

    def __init__(self, cfg: Config):
        super().__init__()
        self.cfg = cfg
        self._stop = threading.Event()
        self._pause = threading.Event()
        self._lock = threading.Lock()
        self._pending: dict = {}
        self._flags: set[str] = set()

        self.src = None
        self.stacker: LiveStacker | None = None
        self.focus_meter = FocusMeter()
        self.platform = PlatformMonitor(limit_minutes=cfg.platform_minutes)
        # Integrar é um compromisso, não uma vista: começa a acumular, escreve
        # subs no disco e consome curso da plataforma. Trocar de modo não pode
        # disparar isso sozinho — quem inicia é o usuário.
        self.integrating = False
        self.cool = CoolerController(ramp_c_per_min=cfg.cooling_ramp)
        self._t_cool = 0.0
        self._dark_temp: float | None = None
        self._n_dark = 20
        self.recorder: Recorder | None = None
        self.info: dict = {}
        self._dark: np.ndarray | None = None
        self._flat: np.ndarray | None = None
        self._hot: np.ndarray | None = None
        self._last_lum: np.ndarray | None = None
        self._loupe_xy: tuple[float, float] | None = None

    # ---------------------------------------------------- comandos (thread GUI)
    def request(self, **kw) -> None:
        with self._lock:
            self._pending.update(kw)

    def flag(self, name: str) -> None:
        with self._lock:
            self._flags.add(name)

    def stop(self) -> None:
        self._stop.set()

    def set_paused(self, on: bool) -> None:
        """Pausa preserva tudo: câmera aberta, stack, gravação e plataforma
        intactos. O que para é só a leitura de frames."""
        if on:
            self._pause.set()
        else:
            self._pause.clear()

    @property
    def is_paused(self) -> bool:
        return self._pause.is_set()

    def set_loupe(self, xy) -> None:
        self._loupe_xy = xy

    def last_luminance(self) -> np.ndarray | None:
        return self._last_lum

    # ---------------------------------------------------- laço (thread própria)
    def run(self) -> None:
        try:
            self._open()
        except Exception as e:                                     # noqa: BLE001
            self.failed.emit(f"{type(e).__name__}: {e}")
            self.finished.emit()
            return
        try:
            self.src.start()
            streaming = True
            falhas = 0
            while not self._stop.is_set():
                self._apply_pending()

                self._atualizar_tec()

                if self._pause.is_set():
                    if streaming:
                        self.src.stop()
                        streaming = False
                        self.paused.emit(True)
                        self.log.emit("pausado — câmera aberta, stack e gravação "
                                      "preservados")
                    time.sleep(0.15)
                    continue
                if not streaming:
                    self.src.resume()
                    streaming = True
                    self.paused.emit(False)
                    self.log.emit("retomado")

                try:
                    got = self.src.read()
                except SVBError as e:
                    if self._stop.is_set():
                        break
                    self.log.emit(f"captura: {e}")
                    continue
                if got is None:
                    self.log.emit("fim dos frames (replay)")
                    break
                raw, meta = got
                # Um erro ao processar um frame não pode custar a sessão. No
                # campo, perder a câmera e o acumulador por causa de um caso não
                # previsto é bem pior que pular um frame. Falhas seguidas ainda
                # abortam, para não mascarar problema persistente.
                try:
                    self._process(raw, meta)
                    falhas = 0
                except Exception as e:                             # noqa: BLE001
                    falhas += 1
                    if falhas == 1:
                        import traceback
                        self.log.emit("erro ao processar frame "
                                      f"{meta.index}: {type(e).__name__}: {e}")
                        for linha in traceback.format_exc().strip().splitlines()[-4:]:
                            self.log.emit("  " + linha)
                    else:
                        self.log.emit(f"erro no frame {meta.index} "
                                      f"({falhas} seguidos): {type(e).__name__}")
                    if falhas >= 10:
                        raise
        except Exception as e:                                     # noqa: BLE001
            self.failed.emit(f"{type(e).__name__}: {e}")
        finally:
            self._finish()

    def _finish(self) -> None:
        """Encerra sempre gravando o stack.

        Antes, parar descartava o acumulador junto com o worker — quarenta
        minutos de integração perdidos num clique. Se não havia gravador (subs
        desligados, ou replay), cria um na hora só para salvar o resultado.
        """
        try:
            self.src.stop()
        except Exception:
            pass

        # reaquecimento antes de desligar: sensor frio com o TEC cortado condensa
        cam = getattr(self.src, "cam", None)
        if (self.cfg.warm_on_finish and cam is not None
                and getattr(cam, "supports_cooler", False)
                and self.cool.target is not None):
            self.cool.set_target(None)
            limite = time.time() + 180.0
            while time.time() < limite:
                self._atualizar_tec(force=True)
                if self.cool.state.phase == "off":
                    break
                time.sleep(2.0)
            else:
                self.log.emit("reaquecimento interrompido no limite de 3 min")
            try:
                cam.cooler = False
            except SVBError:
                pass

        st = self.stacker
        if st is not None and st.started:
            rec = self.recorder
            if rec is None:
                try:
                    rec = Recorder(root=self.cfg.record_root,
                                   target=self.cfg.target_name, save_subs=False)
                    rec.begin(self.info, {"mode": self.cfg.mode,
                                          "origem": self.cfg.source})
                except Exception as e:                             # noqa: BLE001
                    self.log.emit(f"não foi possível criar pasta de saída: {e}")
                    rec = None
            if rec is not None:
                try:
                    snap = self._stats_snapshot()
                    rec.write_stack(st.result(), snap, final=True)
                    rec.end(snap)
                    self.log.emit(f"stack final gravado em {rec.session_dir}")
                except Exception as e:                             # noqa: BLE001
                    self.log.emit(f"erro ao gravar o stack final: {e}")
        try:
            self.src.close()
        except Exception:
            pass
        self.finished.emit()

    # ---------------------------------------------------- interno
    def _open(self) -> None:
        c = self.cfg
        if c.source == "replay":
            if not c.replay_folder:
                raise RuntimeError("nenhuma pasta de replay selecionada")
            self.src = ReplaySource(c.replay_folder, speed=c.replay_speed,
                                    loop=c.replay_loop)
        else:
            self.src = CameraSource(index=c.camera_index, bin=c.bin,
                                    exposure=c.exposure, gain=c.gain,
                                    offset=c.offset, target_temp=c.target_temp)
        self.info = self.src.open()
        self._load_dark()
        self._load_flat()
        self._new_stacker()

        if c.record and c.source == "camera":
            self.recorder = Recorder(root=c.record_root, target=c.target_name,
                                     save_subs=True, compress=c.compress,
                                     every=c.record_every)
            d = self.recorder.begin(self.info, {
                "exposure": c.exposure, "gain": c.gain, "offset": c.offset,
                "bin": c.bin, "dark": c.dark_path, "sigma_clip": c.sigma_clip,
            })
            self.log.emit(f"gravando em {d}")

        if self.cfg.target_temp is not None and self.info.get("cooler"):
            self._pedir_tec(True)

        self.opened.emit(self.info)
        if self.info.get("forced"):
            self.log.emit("pipeline linearizado: " + ", ".join(self.info["forced"]))
        if self.info.get("live") and self.info.get("port") != "USB3.0":
            self.log.emit(f"USB em {self.info['port']}: ~475 ms de tempo morto por "
                          f"frame (irrelevante em subs longos, lento ao focar)")

    def _load_dark(self) -> None:
        self._dark = self._hot = None
        if not self.cfg.dark_path:
            return
        p = Path(self.cfg.dark_path)
        try:
            d, _ = read_fits(p)
        except Exception as e:                                     # noqa: BLE001
            self.log.emit(f"dark ilegível: {e}")
            return
        d = d.astype(np.float32)
        shape = (self.info.get("height"), self.info.get("width"))
        if shape[0] and d.shape != shape:
            self.log.emit(f"dark ignorado: {d.shape} != {shape} (bin diferente?)")
            return
        self._dark = d
        self.log.emit(f"dark: {p.name} (mediana {np.median(d):.0f})")
        # dark só serve na temperatura em que foi tirado: o sinal térmico dobra a
        # cada ~6 °C, então 3 °C de diferença já deixa resíduo visível
        try:
            _, hdr = read_fits(p)
            if "CCD-TEMP" in hdr:
                self._dark_temp = float(hdr["CCD-TEMP"])
                cam = getattr(self.src, "cam", None)
                if cam is not None and cam.supports_cooler:
                    d_t = cam.temperature - self._dark_temp
                    if abs(d_t) > 2.0:
                        self.log.emit(
                            f"atenção: dark tirado a {self._dark_temp:+.1f} °C, "
                            f"sensor agora a {cam.temperature:+.1f} °C "
                            f"({d_t:+.1f} °C de diferença)")
        except Exception:
            pass
        hp = p.with_name(p.name.replace(".fits", "_hot.npy"))
        if hp.exists():
            h = np.load(hp)
            if h.shape == d.shape:
                self._hot = h
                self.log.emit(f"mapa de pixels quentes: {int(h.sum())} px")

    # ------------------------------------------------------------- refrigeração
    def _pedir_tec(self, ligar: bool) -> None:
        cam = getattr(self.src, "cam", None)
        if cam is None or not cam.supports_cooler:
            return
        atual = cam.temperature
        if ligar and self.cfg.target_temp is not None:
            self.cool.set_target(self.cfg.target_temp, current=atual)
            self.log.emit(f"TEC: rampa de {atual:+.1f} para "
                          f"{self.cfg.target_temp:+.1f} °C a "
                          f"{self.cfg.cooling_ramp:.1f} °C/min")
        else:
            self.cool.set_target(None)
            self.log.emit("TEC: reaquecendo antes de desligar")

    def _atualizar_tec(self, force: bool = False) -> None:
        """Um passo do controlador. Chamado do laço, inclusive na pausa —
        resfriar leva quinze a vinte minutos e você quer que isso ande enquanto
        enquadra e foca."""
        cam = getattr(self.src, "cam", None)
        if cam is None or not cam.supports_cooler:
            return
        agora = time.time()
        if not force and agora - self._t_cool < 2.0:
            return
        self._t_cool = agora
        try:
            atual, pot = cam.temperature, cam.cooler_power
            sp, ligar = self.cool.update(atual, pot, agora)
            if sp is not None:
                cam.target_temperature = float(sp)
            if cam.cooler != ligar:
                cam.cooler = ligar
        except SVBError as e:
            self.log.emit(f"TEC: {e}")
            return
        st = self.cool.state
        self.cooling.emit({
            "phase": st.phase, "current": st.current, "target": st.target,
            "setpoint": st.setpoint, "power": st.power, "stable": st.stable,
            "eta_s": st.eta_s, "ambient": st.ambient, "message": st.message,
            "dark_delta": (None if self._dark_temp is None
                           else st.current - self._dark_temp),
        })

    def _gravar_dark(self, n: int) -> None:
        """Grava um master dark com os parâmetros da sessão em curso.

        Fazer isso aqui, e não num comando separado, é o ponto: o dark precisa
        casar exposição, ganho, offset, bin E temperatura com os lights. No meio
        da sessão esses cinco já estão certos por construção — rodar um comando
        à parte depois obriga a reproduzir tudo, e a temperatura é a que mais
        escapa.
        """
        cam = getattr(self.src, "cam", None)
        if cam is None:
            self.log.emit("gravar dark só funciona com a câmera, não em replay")
            return
        exp, gain, off = cam.exposure, cam.gain, cam.offset
        g = cam.geometry
        temp = cam.temperature if cam.supports_cooler else None
        self.log.emit(f"gravando dark: {n} frames de {exp:.2f}s, ganho {gain}, "
                      f"bin{g.bin}" + (f", {temp:+.1f} °C" if temp is not None else ""))

        frames = []
        try:
            for k in range(n + 1):
                if self._stop.is_set():
                    self.log.emit("dark cancelado")
                    return
                got = self.src.read()
                if got is None:
                    break
                if k == 0:
                    continue                      # primeiro vem stale
                frames.append(got[0].astype(np.float32))
                if len(frames) % 5 == 0 or len(frames) == n:
                    self.log.emit(f"  dark {len(frames)}/{n}")
        except SVBError as e:
            self.log.emit(f"dark interrompido: {e}")
        if len(frames) < 3:
            self.log.emit("frames insuficientes para o dark")
            return

        master = np.median(np.stack(frames), axis=0).astype(np.float32)
        pasta = Path("darks")
        pasta.mkdir(parents=True, exist_ok=True)
        nome = (f"dark_g{gain}_o{off}_e{exp:.2f}s_bin{g.bin}"
                + (f"_{temp:+.0f}C" if temp is not None else "") + ".fits")
        caminho = pasta / nome

        from astropy.io import fits
        hdr = fits.Header()
        hdr["IMAGETYP"] = "DARK"
        hdr["EXPTIME"] = exp
        hdr["GAIN"] = gain
        hdr["OFFSET"] = off
        hdr["XBINNING"] = g.bin
        hdr["NCOMBINE"] = len(frames)
        hdr["BAYERPAT"] = cam.bayer.fits_name
        hdr["FULLSCAL"] = cam.full_scale
        if temp is not None:
            hdr["CCD-TEMP"] = round(temp, 2)
        fits.PrimaryHDU(master, hdr).writeto(caminho, overwrite=True)

        med = float(np.median(master))
        mad = float(np.median(np.abs(master - med))) * 1.4826
        quentes = master > med + 12 * max(mad, 1.0)
        np.save(caminho.with_name(caminho.name.replace(".fits", "_hot.npy")), quentes)
        self.log.emit(f"master dark -> {caminho}  ({int(quentes.sum())} pixels "
                      f"quentes, {100*quentes.mean():.4f}%)")

        self.cfg.dark_path = str(caminho)
        self._load_dark()
        self.dark_saved.emit(str(caminho))

    def _load_flat(self) -> None:
        """Master flat, normalizado para média 1.

        Newtoniano rápido com sensor 4/3" tem vinheteamento visível, mais poeira
        na janela do sensor. Sem flat os cantos ficam escuros e o autostretch
        denuncia. Num sensor colorido o flat é aplicado no mosaico cru, o que
        corrige de uma vez o vinheteamento e a diferença de resposta entre os
        canais — é o comportamento certo para OSC.
        """
        self._flat = None
        if not self.cfg.flat_path:
            return
        p = Path(self.cfg.flat_path)
        try:
            d, _ = read_fits(p)
        except Exception as e:                                     # noqa: BLE001
            self.log.emit(f"flat ilegível: {e}")
            return
        d = d.astype(np.float32)
        shape = (self.info.get("height"), self.info.get("width"))
        if shape[0] and d.shape != shape:
            self.log.emit(f"flat ignorado: {d.shape} != {shape} (bin diferente?)")
            return
        m = float(np.median(d))
        if m <= 0:
            self.log.emit("flat inválido (mediana <= 0)")
            return
        # normaliza e limita o divisor: pixel de flat muito baixo amplificaria
        # ruído sem limite no canto
        self._flat = np.clip(d / m, 0.15, 4.0).astype(np.float32)
        self.log.emit(f"flat: {p.name} (vinheteamento no canto "
                      f"{self._flat.min()*100:.0f}% do centro)")

    def _new_stacker(self) -> None:
        h, w = self.info["height"], self.info["width"]
        c = self.cfg
        self.stacker = LiveStacker((h, w), channels=3, sigma_clip=c.sigma_clip,
                                   saturation=debayer.LUM_SUM,
                                   ref_refresh=c.ref_refresh,
                                   min_ref_stars=c.min_ref_stars,
                                   quality_weighting=c.quality_weighting)
        self.stacker.set_rigor(c.rigor)
        self.platform.clear()

    def _apply_pending(self) -> None:
        with self._lock:
            pend, flags = self._pending, self._flags
            self._pending, self._flags = {}, set()

        # refrigeração passa pelo controlador, não direto para a câmera
        tec = {k: pend.pop(k) for k in list(pend) if k in ("target_temp", "cooler")}
        if "target_temp" in tec:
            self.cfg.target_temp = float(tec["target_temp"])
        if "cooler" in tec:
            self._pedir_tec(bool(tec["cooler"]))
        elif "target_temp" in tec and self.cool.target is not None:
            self._pedir_tec(True)

        proc = {k: pend.pop(k) for k in list(pend)
                if k in ("dark_path", "flat_path", "sigma_clip", "rigor",
                         "dark_frames",
                         "platform_minutes", "record_every", "target_name",
                         "mode")}
        if pend:
            for m in self.src.apply(**pend):
                self.log.emit(m)
            if "bin" in pend:
                self.info.update(width=self.src.geometry.width,
                                 height=self.src.geometry.height,
                                 bin=self.src.geometry.bin)
                self._load_dark()
                self._load_flat()
                self._new_stacker()
                self.log.emit("geometria mudou: stack zerado")

        for k, v in proc.items():
            if k == "dark_path":
                self.cfg.dark_path = v or None
                self._load_dark()
            elif k == "dark_frames":
                self._n_dark = max(int(v), 3)
            elif k == "rigor":
                if self.stacker:
                    self.stacker.set_rigor(str(v))
                self.cfg.rigor = str(v)
                self.log.emit(f"rigor de rejeição: {v}")
            elif k == "sigma_clip":
                self.cfg.sigma_clip = v
                self._new_stacker()
                self.log.emit("sigma clip alterado: stack zerado")
            elif k == "platform_minutes":
                self.platform.limit_s = float(v) * 60.0
            elif k == "record_every" and self.recorder:
                self.recorder.every = int(v)
            elif k == "target_name":
                self.cfg.target_name = str(v)
                if self.recorder:
                    self.recorder.target = str(v)
            elif k == "mode":
                self._set_mode(str(v))

        if "new_segment" in flags and self.stacker:
            self.stacker.new_segment()
            self.log.emit("novo segmento: limiares relaxados, referência será "
                          "reextraída do stack")
        if "platform_reset" in flags:
            self.platform.note_reset()
            if self.stacker:
                self.stacker.new_segment()
            self.log.emit("plataforma resetada: curso reiniciado, stack preservado")
        if "reset" in flags:
            self._new_stacker()
            self.focus_meter = FocusMeter()
            self.log.emit("stack zerado")
        if "integrar_on" in flags:
            self.set_integrating(True)
        if "integrar_off" in flags:
            self.set_integrating(False)
        if "gravar_dark" in flags:
            self._gravar_dark(self._n_dark)
        if "reset_focus_best" in flags:
            self.focus_meter.reset_best()
            self.log.emit("melhor foco da sessão zerado")

    @property
    def pode_integrar(self) -> bool:
        return self.cfg.mode in ("stack", "adjust")

    @property
    def stacking(self) -> bool:
        """Empilha só se o usuário mandou E o modo permite."""
        return self.integrating and self.pode_integrar

    def set_integrating(self, on: bool) -> None:
        antes = self.stacking
        self.integrating = bool(on)
        if self.stacking and not antes:
            if self.platform.run_start is None:
                self.platform.clear()
                self.platform.start_run()
                self.log.emit("integrando: empilhando, gravando e medindo a "
                              "plataforma; cronômetro de curso iniciado")
            else:
                self.log.emit("integração retomada no acumulador existente")
        elif antes and not self.stacking:
            self.log.emit("integração parada — captura segue, stack preservado")

    def _set_mode(self, mode: str) -> None:
        antes = self.stacking
        self.cfg.mode = mode
        agora = self.stacking
        if antes and not agora:
            self.log.emit(f"{mode}: integração em espera, stack preservado")
        elif agora and not antes:
            self.log.emit("integração retomada")

    def _process(self, raw: np.ndarray, meta: FrameMeta) -> None:
        st = self.stacker
        t0 = time.perf_counter()

        f = raw.astype(np.float32)
        if self._dark is not None and self._dark.shape == f.shape:
            f -= self._dark
            np.maximum(f, 0.0, out=f)
        if self._flat is not None and self._flat.shape == f.shape:
            # ordem obrigatória: (bruto - dark) / flat
            f /= self._flat
        if self._hot is not None and self._hot.shape == f.shape:
            f = _fix_hot_bayer(f, self._hot)
        f /= max(meta.full_scale, 1)
        np.clip(f, 0.0, 1.0, out=f)

        rgb = debayer.to_rgb((f * 65535).astype(np.uint16), meta.bayer,
                             quality="linear").astype(np.float32) / 65535.0
        lum = debayer.cfa_to_luminance(f)
        self._last_lum = lum

        if not self.stacking:
            # caminho leve: só detecta estrelas, para HFR e contagem. Nada toca
            # o acumulador, o disco ou o monitor da plataforma.
            # saturação na escala da LUMINÂNCIA, que é a soma de 4 pixels da
            # quadra Bayer e vai até 4.0 num frame normalizado. Passar 1.0 aqui
            # descartava como saturada toda estrela acima de ~24% da escala do
            # sensor — ou seja, exatamente as estrelas que sustentam o registro.
            sf = detect(lum, scale=2.0, max_stars=60, central=0.70,
                        saturation=debayer.LUM_SUM)
            sample = self.focus_meter.add(sf, meta.temperature) if len(sf) >= 3 else None
            stats = self._stats_snapshot(None, None, meta, t0)
            stats.update(accepted=None, reason="", n_stars=len(sf),
                         fwhm=sf.median_fwhm, matched=0, rms=float("nan"),
                         rotation=0.0, frame_ms=0.0)
            self.frame.emit(rgb, st.result() if st.started else None, stats)
            self._emit_focus(sample, sf, lum, o=None)
            return

        o = st.add(rgb, lum, exposure=meta.exposure, lum_scale=2.0)
        al = o.alignment

        if al is not None and al.ok and o.accepted:
            self.platform.add(al.rotation_deg, *st.drift(), o.fwhm)

        fs = self.focus_meter
        sample = fs.add(_StarProxy(o), meta.temperature) if o.n_stars >= 3 else None

        if self.recorder:
            try:
                self.recorder.write_sub(raw, meta)
            except Exception as e:                                 # noqa: BLE001
                self.log.emit(f"erro ao gravar sub: {e}")

        stats = self._stats_snapshot(o, al, meta, t0)
        self.frame.emit(rgb, st.result() if st.started else None, stats)
        self._emit_focus(sample, None, lum, o=o)

    def _emit_focus(self, sample, starfield, lum, o) -> None:
        fs = self.focus_meter
        xy = self._loupe_xy
        if xy is None:
            if o is not None:
                xy = o.brightest
            elif starfield is not None and len(starfield):
                i = int(np.argmax(starfield.flux))
                xy = (float(starfield.xy[i, 0]), float(starfield.xy[i, 1]))
        crop = loupe(lum, (xy[0] / 2.0, xy[1] / 2.0), half=28, zoom=5) if xy else None
        self.focus.emit(crop, {
            "hfr": sample.hfr if sample else float("nan"),
            "fwhm": sample.fwhm if sample else float("nan"),
            "n_stars": len(starfield) if starfield is not None
            else (o.n_stars if o else 0),
            "trend": fs.trend(), "ratio": fs.ratio_to_best(),
            "best": fs.best.hfr if fs.best else float("nan"),
            "verdict": fs.verdict(), "series": fs.series(),
        })

    def _stats_snapshot(self, o=None, al=None, meta=None, t0=None) -> dict:
        st = self.stacker
        shape = (self.info.get("height", 1), self.info.get("width", 1))
        rep = self.platform.report(shape)
        s = {
            "mode": self.cfg.mode,
            "stacking": self.stacking,
            "integrating": self.integrating,
            "pode_integrar": self.pode_integrar,
            "n_stacked": st.n_stacked if st else 0,
            "n_rejected": st.n_rejected if st else 0,
            "rejections": dict(st.rejections) if st else {},
            "integration": st.total_exposure if st else 0.0,
            "integration_eff": st.weighted_exposure if st else 0.0,
            "overlap": st.overlap_fraction() if st else 0.0,
            "drift": st.drift() if st else (0.0, 0.0),
            "platform": rep,
            "platform_advice": self.platform.advice(shape),
            "recording": self.recorder.disk_report() if self.recorder else "",
        }
        if o is not None:
            s.update(accepted=o.accepted, reason=o.reason, n_stars=o.n_stars,
                     fwhm=o.fwhm, frame_ms=o.elapsed_ms,
                     proc_ms=(time.perf_counter() - t0) * 1e3 if t0 else 0.0,
                     matched=al.n_matched if al else 0,
                     rms=al.rms if al else float("nan"),
                     rotation=al.rotation_deg if al else 0.0,
                     weight=o.weight, score=o.score)
        if meta is not None:
            s.update(temp=meta.temperature, cooler_power=meta.cooler_power,
                     exposure=meta.exposure, gain=meta.gain, frame_index=meta.index,
                     origin=meta.origin)
        if isinstance(self.src, ReplaySource):
            i, n = self.src.progress
            s["replay_progress"] = (i, n)
        return s


class _StarProxy:
    """Adapta o resultado do stacker à interface que o FocusMeter espera.

    O stacker já detectou as estrelas; redetectar só para medir foco custaria o
    dobro do tempo de processamento por frame.
    """

    def __init__(self, outcome):
        self._o = outcome

    @property
    def median_hfr(self) -> float:
        return getattr(self._o, "hfr", float("nan"))

    @property
    def median_fwhm(self) -> float:
        return self._o.fwhm

    @property
    def peak(self):
        return np.empty(0)

    def __len__(self) -> int:
        return self._o.n_stars


def _fix_hot_bayer(f: np.ndarray, hot: np.ndarray) -> np.ndarray:
    """Pixels quentes trocados pela mediana de vizinhos de MESMA cor — usar
    vizinhos imediatos misturaria canais do mosaico e criaria artefato de cor."""
    import cv2
    out = f.copy()
    for dy in (0, 1):
        for dx in (0, 1):
            ph_hot = hot[dy::2, dx::2]
            if not ph_hot.any():
                continue
            ph = np.ascontiguousarray(f[dy::2, dx::2])
            ph_med = cv2.medianBlur(ph, 3)
            sub = out[dy::2, dx::2]
            sub[ph_hot] = ph_med[ph_hot]
            out[dy::2, dx::2] = sub
    return out
