"""Camada pythonica sobre o SDK da SVBony.

Uso tipico:

    from svbony.camera import list_cameras, Camera
    cams = list_cameras()
    with Camera(cams[0]) as cam:
        cam.set_roi(bin=2)
        cam.image_type = ImgType.RAW16
        cam.gain = 250
        cam.exposure = 5.0            # segundos
        cam.start_video()
        frame = cam.read_frame()      # ndarray (h, w) uint16
"""
from __future__ import annotations

import ctypes as C
from dataclasses import dataclass, field

import numpy as np

from . import sdk
from .sdk import Bayer, Control, ImgType, SVBError, check


@dataclass(frozen=True)
class CameraID:
    index: int
    camera_id: int
    name: str
    serial: str
    port: str

    def __str__(self) -> str:
        return f"[{self.index}] {self.name}  sn={self.serial or '?'}  ({self.port})"


def list_cameras() -> list[CameraID]:
    out = []
    for idx in range(sdk.GetNumOfConnectedCameras()):
        info = sdk.CameraInfo()
        check("GetCameraInfo", sdk.GetCameraInfo(C.byref(info), idx))
        out.append(
            CameraID(
                index=idx,
                camera_id=info.CameraID,
                name=info.FriendlyName.decode(errors="replace"),
                serial=info.CameraSN.decode(errors="replace"),
                port=info.PortType.decode(errors="replace"),
            )
        )
    return out


@dataclass
class ControlInfo:
    control: int
    name: str
    description: str
    min: int
    max: int
    default: int
    writable: bool
    auto_supported: bool

    @property
    def label(self) -> str:
        try:
            return Control(self.control).name
        except ValueError:
            return f"UNKNOWN_{self.control}"


@dataclass
class Geometry:
    x: int
    y: int
    width: int
    height: int
    bin: int


class Camera:
    """Uma camera aberta. Use como context manager."""

    def __init__(self, cam: CameraID):
        self.id = cam
        self._cid = cam.camera_id
        self._open = False
        self._buf: C.Array | None = None
        self._buf_geom: tuple | None = None
        self._streaming = False
        self.props: sdk.CameraProperty | None = None
        self.props_ex: sdk.CameraPropertyEx | None = None
        self.controls: dict[int, ControlInfo] = {}
        self.frames_read = 0

    # ------------------------------------------------------------ ciclo de vida
    def open(self) -> "Camera":
        # Quirk da SDK: depois de um SVBCloseCamera o CameraID so' volta a ser
        # aceito apos re-enumerar os dispositivos. Sem isso, SVBOpenCamera
        # devolve INVALID_INDEX na segunda abertura do processo.
        sdk.GetNumOfConnectedCameras()
        _info = sdk.CameraInfo()
        sdk.GetCameraInfo(C.byref(_info), self.id.index)

        check("OpenCamera", sdk.OpenCamera(self._cid))
        self._open = True
        self.props = sdk.CameraProperty()
        check("GetCameraProperty", sdk.GetCameraProperty(self._cid, C.byref(self.props)))
        self.props_ex = sdk.CameraPropertyEx()
        # nem toda build da SDK implementa; nao e' fatal
        if sdk.GetCameraPropertyEx(self._cid, C.byref(self.props_ex)) != 0:
            self.props_ex = None
        # A SDK, por padrão, persiste os parâmetros da câmera num arquivo
        # .bin no diretório de trabalho e os restaura na abertura seguinte. Foi
        # isso que fez o ganho falhar de forma intermitente no começo: a câmera
        # voltava com a exposição em modo automático de uma sessão anterior, e
        # com auto ligado SetControlValue(GAIN) devolve GENERAL_ERROR.
        #
        # Desligamos: reafirmamos tudo explicitamente logo abaixo, e estado
        # oculto que sobrevive ao processo só atrapalha a depuração. De quebra,
        # para de sujar o diretório com arquivos U3SM*_Cfg_*.bin.
        try:
            sdk.SetAutoSaveParam(self._cid, 0)
        except Exception:
            pass

        self._load_controls()
        self._leave_auto_mode()
        self._force_linear()
        return self

    def close(self) -> None:
        if self._streaming:
            try:
                self.stop_video()
            except SVBError:
                pass
        if self._open:
            sdk.CloseCamera(self._cid)
            self._open = False

    def __enter__(self) -> "Camera":
        return self.open()

    def __exit__(self, *exc) -> None:
        self.close()

    def _load_controls(self) -> None:
        n = C.c_int()
        check("GetNumOfControls", sdk.GetNumOfControls(self._cid, C.byref(n)))
        self.controls.clear()
        for k in range(n.value):
            caps = sdk.ControlCaps()
            check("GetControlCaps", sdk.GetControlCaps(self._cid, k, C.byref(caps)))
            self.controls[caps.ControlType] = ControlInfo(
                control=caps.ControlType,
                name=caps.Name.decode(errors="replace"),
                description=caps.Description.decode(errors="replace"),
                min=caps.MinValue,
                max=caps.MaxValue,
                default=caps.DefaultValue,
                writable=bool(caps.IsWritable),
                auto_supported=bool(caps.IsAutoSupported),
            )

    def _leave_auto_mode(self) -> None:
        """Sai do modo automatico de exposicao/ganho.

        Quirk da SDK: enquanto EXPOSURE esta' com auto=1, o laco de
        AUTO_TARGET_BRIGHTNESS controla o ganho, e qualquer
        SetControlValue(GAIN) devolve GENERAL_ERROR (16). A camera persiste
        esse flag entre sessoes, o que faz o sintoma parecer intermitente.
        Reafirmamos o valor corrente com auto=0 para assumir o controle.
        """
        for ctrl in (Control.EXPOSURE, Control.GAIN):
            info = self.controls.get(int(ctrl))
            if info is None or not info.writable:
                continue
            try:
                val, auto = self.get(ctrl)
            except SVBError:
                continue
            if auto:
                try:
                    self.set(ctrl, val, auto=False)
                except SVBError:
                    pass

    def _force_linear(self) -> None:
        """Neutraliza o processamento cosmetico da SDK para obter RAW linear.

        Descobertas medidas nesta camera:

        * WB_R/WB_G/WB_B sao aplicados MULTIPLICATIVAMENTE aos dados RAW16
          (com WB_R=400 a fase R subiu ~3.9x). Isso quebra a linearidade por
          canal, invalida flats e destroi a calibracao de cor. 128 == ganho 1.
        * GAMMA=100 e' linear (o passo de quantizacao permanece exatamente 4).
        * BAD_PIXEL_CORRECTION vem LIGADA de fabrica. Ela substitui pixels
          isolados acima de um limiar -- e uma estrela fraca ocupando poucos
          pixels e' exatamente isso. Desligamos e fazemos nosso proprio mapa
          de pixels quentes a partir dos darks.

        Os valores ficam persistidos na camera entre sessoes, entao nao da'
        para confiar no estado inicial: reafirmamos sempre.
        """
        neutral = {
            Control.WB_R: 128,
            Control.WB_G: 128,
            Control.WB_B: 128,
            Control.GAMMA: 100,
            Control.CONTRAST: 50,
            Control.BAD_PIXEL_CORRECTION_ENABLE: 0,
        }
        self.forced_linear: list[str] = []
        for ctrl, want in neutral.items():
            info = self.controls.get(int(ctrl))
            if info is None or not info.writable:
                continue
            try:
                cur, _ = self.get(ctrl)
            except SVBError:
                continue
            if cur != want:
                try:
                    self.set(ctrl, want)
                    self.forced_linear.append(f"{Control(ctrl).name} {cur}->{want}")
                except SVBError:
                    pass

    # ------------------------------------------------------------ metadados
    @property
    def sensor_size(self) -> tuple[int, int]:
        return int(self.props.MaxWidth), int(self.props.MaxHeight)

    @property
    def is_color(self) -> bool:
        return bool(self.props.IsColorCam)

    @property
    def bayer(self) -> Bayer:
        return Bayer(self.props.BayerPattern)

    @property
    def supported_bins(self) -> list[int]:
        return [b for b in self.props.SupportedBins if b != 0]

    @property
    def supported_formats(self) -> list[ImgType]:
        # a lista e' terminada por -1 em algumas builds; RAW8 == 0 e' valido,
        # entao filtramos apenas negativos e valores fora do enum
        out = []
        for v in self.props.SupportedVideoFormat:
            if v < 0:
                break
            try:
                out.append(ImgType(v))
            except ValueError:
                break
        return out

    @property
    def bit_depth(self) -> int:
        """Bits reais do ADC (14 no IMX294 da SV405CC)."""
        return int(self.props.MaxBitDepth)

    @property
    def full_scale(self) -> int:
        """Valor de saturacao dos dados entregues.

        Em RAW16 a camera entrega os 14 bits do ADC *deslocados 2 bits a
        esquerda*: os valores sao multiplos de 4 e a escala vai de 0 a 65532.
        Verificado medindo o passo minimo entre valores distintos (== 4) com o
        white balance neutro.

        Atencao: isso so' vale com o pipeline linear. Com WB != 128 a SDK
        multiplica os dados RAW e a grade de quantizacao desaparece -- por isso
        open() chama _force_linear().
        """
        t = self.image_type
        if t.bytes_per_pixel == 1:
            return 255
        return ((1 << self.bit_depth) - 1) << (16 - self.bit_depth)

    @property
    def pixel_size_um(self) -> float:
        v = C.c_float()
        check("GetSensorPixelSize", sdk.GetSensorPixelSize(self._cid, C.byref(v)))
        return float(v.value)

    @property
    def firmware(self) -> str:
        buf = C.create_string_buffer(128)
        if sdk.GetCameraFirmwareVersion(self._cid, buf) != 0:
            return "?"
        return buf.value.decode(errors="replace")

    @property
    def supports_cooler(self) -> bool:
        if self.props_ex is not None:
            return bool(self.props_ex.bSupportControlTemp)
        return Control.COOLER_ENABLE in self.controls

    # ------------------------------------------------------------ controles
    def get(self, ctrl: int) -> tuple[int, bool]:
        val, auto = C.c_long(), C.c_int()
        check("GetControlValue", sdk.GetControlValue(self._cid, int(ctrl), C.byref(val), C.byref(auto)))
        return int(val.value), bool(auto.value)

    def set(self, ctrl: int, value: int, auto: bool = False) -> None:
        info = self.controls.get(int(ctrl))
        if info is not None:
            if not info.writable:
                raise ValueError(f"{info.label} nao e' gravavel nesta camera")
            if not (info.min <= value <= info.max):
                raise ValueError(f"{info.label}={value} fora da faixa [{info.min}, {info.max}]")
        check("SetControlValue", sdk.SetControlValue(self._cid, int(ctrl), int(value), int(auto)))

    # atalhos --------------------------------------------------------------
    @property
    def gain(self) -> int:
        return self.get(Control.GAIN)[0]

    @gain.setter
    def gain(self, v: int) -> None:
        self.set(Control.GAIN, v)

    @property
    def exposure(self) -> float:
        """Exposicao em segundos (a SDK usa microssegundos)."""
        return self.get(Control.EXPOSURE)[0] / 1e6

    @exposure.setter
    def exposure(self, seconds: float) -> None:
        self.set(Control.EXPOSURE, int(round(seconds * 1e6)))

    @property
    def offset(self) -> int:
        return self.get(Control.BLACK_LEVEL)[0]

    @offset.setter
    def offset(self, v: int) -> None:
        self.set(Control.BLACK_LEVEL, v)

    @property
    def temperature(self) -> float:
        return self.get(Control.CURRENT_TEMPERATURE)[0] / 10.0

    @property
    def cooler_power(self) -> int:
        return self.get(Control.COOLER_POWER)[0]

    @property
    def target_temperature(self) -> float:
        return self.get(Control.TARGET_TEMPERATURE)[0] / 10.0

    @target_temperature.setter
    def target_temperature(self, celsius: float) -> None:
        self.set(Control.TARGET_TEMPERATURE, int(round(celsius * 10)))

    @property
    def cooler(self) -> bool:
        return bool(self.get(Control.COOLER_ENABLE)[0])

    @cooler.setter
    def cooler(self, on: bool) -> None:
        self.set(Control.COOLER_ENABLE, 1 if on else 0)

    @property
    def bad_pixel_correction(self) -> bool:
        return bool(self.get(Control.BAD_PIXEL_CORRECTION_ENABLE)[0])

    @bad_pixel_correction.setter
    def bad_pixel_correction(self, on: bool) -> None:
        self.set(Control.BAD_PIXEL_CORRECTION_ENABLE, 1 if on else 0)

    # ------------------------------------------------------------ ROI / formato
    @property
    def geometry(self) -> Geometry:
        x, y, w, h, b = (C.c_int() for _ in range(5))
        check("GetROIFormat", sdk.GetROIFormat(self._cid, *(C.byref(v) for v in (x, y, w, h, b))))
        return Geometry(x.value, y.value, w.value, h.value, b.value)

    @property
    def image_type(self) -> ImgType:
        v = C.c_int()
        check("GetOutputImageType", sdk.GetOutputImageType(self._cid, C.byref(v)))
        return ImgType(v.value)

    @image_type.setter
    def image_type(self, t: ImgType) -> None:
        check("SetOutputImageType", sdk.SetOutputImageType(self._cid, int(t)))
        self._buf = None  # invalida o buffer: bytes/pixel mudou

    def set_roi(self, bin: int = 1, x: int = 0, y: int = 0,
                width: int | None = None, height: int | None = None) -> Geometry:
        """Define ROI/binning. Sem width/height usa o sensor inteiro no bin dado.

        Largura e altura precisam ser multiplos de 8 -- a SDK rejeita o resto.
        Mantemos o start par para nao trocar a fase do padrao Bayer.
        """
        if bin not in self.supported_bins:
            raise ValueError(f"bin {bin} nao suportado; disponiveis: {self.supported_bins}")
        if bin > 2:
            # Medido: o binning e' soma de pixels de mesma cor, grampeada em
            # 0xFFFF. Com 14 bits deslocados <<2, bin2 cabe exato (4*16383 =
            # 65532) mas bin3 (147447) e bin4 (262128) estouram -- bin3 perdeu
            # ~3.2 stops de altas luzes na medicao. Ver HARDWARE.md secao 7.
            import warnings
            warnings.warn(
                f"bin{bin} grampeia as altas luzes nesta camera (soma satura em "
                f"0xFFFF); use bin2 para binning sem perda", RuntimeWarning, stacklevel=2)
        mw, mh = self.sensor_size
        width = (mw // bin) if width is None else width
        height = (mh // bin) if height is None else height
        # start par para nao trocar a fase do padrao Bayer
        x -= x % 2
        y -= y % 2
        rc = sdk.SetROIFormat(self._cid, x, y, width, height, bin)
        if rc != 0:
            # algumas builds exigem largura/altura multiplas de 8
            w8, h8 = width - width % 8, height - height % 8
            rc8 = sdk.SetROIFormat(self._cid, x, y, w8, h8, bin)
            if rc8 == 0:
                width, height, rc = w8, h8, 0
        check("SetROIFormat", rc)
        self._buf = None
        return self.geometry

    # ------------------------------------------------------------ streaming
    def _ensure_buffer(self) -> tuple[C.Array, Geometry, ImgType]:
        g, t = self.geometry, self.image_type
        key = (g.width, g.height, int(t))
        if self._buf is None or self._buf_geom != key:
            self._buf = (C.c_ubyte * (g.width * g.height * t.bytes_per_pixel))()
            self._buf_geom = key
        return self._buf, g, t

    def start_video(self) -> None:
        check("StartVideoCapture", sdk.StartVideoCapture(self._cid))
        self._streaming = True

    def stop_video(self) -> None:
        check("StopVideoCapture", sdk.StopVideoCapture(self._cid))
        self._streaming = False

    def read_frame(self, timeout: float | None = None) -> np.ndarray:
        """Le um frame. Retorna copia propria -- o buffer interno e' reciclado.

        timeout em segundos; por padrao 2x a exposicao + 1 s de folga.
        Levanta SVBError(code=TIMEOUT) se nada chegar.
        """
        buf, g, t = self._ensure_buffer()
        if timeout is None:
            timeout = self.exposure * 2 + 1.0
        rc = sdk.GetVideoData(self._cid, buf, len(buf), int(timeout * 1000))
        check("GetVideoData", rc)
        self.frames_read += 1

        if t.bytes_per_pixel == 1:
            arr = np.frombuffer(buf, dtype=np.uint8, count=g.width * g.height)
            return arr.reshape(g.height, g.width).copy()
        if t is ImgType.RGB24:
            arr = np.frombuffer(buf, dtype=np.uint8, count=g.width * g.height * 3)
            return arr.reshape(g.height, g.width, 3).copy()  # BGR na saida da SDK
        arr = np.frombuffer(buf, dtype="<u2", count=g.width * g.height)
        return arr.reshape(g.height, g.width).copy()

    @property
    def dropped_frames(self) -> int:
        v = C.c_int()
        check("GetDroppedFrames", sdk.GetDroppedFrames(self._cid, C.byref(v)))
        return v.value

    def describe(self) -> str:
        w, h = self.sensor_size
        lines = [
            f"{self.id.name}   sn={self.id.serial or '?'}   fw={self.firmware}",
            f"  sensor        {w} x {h}  @ {self.pixel_size_um:.2f} um   {self.bit_depth} bits "
            f"(escala util 0..{(1 << self.bit_depth) - 1})",
            f"  cor           {'sim, Bayer ' + self.bayer.name if self.is_color else 'mono'}",
            f"  bins          {self.supported_bins}",
            f"  formatos      {[f.name for f in self.supported_formats]}",
            f"  TEC           {'sim' if self.supports_cooler else 'nao'}",
            f"  ROI atual     {self.geometry}   fmt={self.image_type.name}",
            "  controles:",
        ]
        for c in sorted(self.controls.values(), key=lambda c: c.control):
            try:
                val, auto = self.get(c.control)
                cur = f"{val}{' (auto)' if auto else ''}"
            except SVBError as e:
                cur = f"<{e.code}>"
            rw = "rw" if c.writable else "ro"
            lines.append(
                f"    {c.label:<32} {cur:>12}   [{c.min}..{c.max}] def={c.default} {rw}"
            )
        return "\n".join(lines)
