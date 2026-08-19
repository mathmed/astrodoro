"""Janela principal — painel de EAA organizado por modo de tarefa.

A interface não é um painel de configurações: é uma sequência de fases com
necessidades de tela diferentes. Enquadrar quer o frame ao vivo grande e a
direção do alvo; focar quer o HFR enorme e a lupa; integrar quer o stack e a
saúde dos frames. Cada modo reorganiza a tela para a fase em que você está.

Acima de tudo isso fica uma barra de sinais vitais que nunca sai da tela, porque
no escuro você olha de relance, não lê painéis.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import (QEvent, QObject, QRunnable, QSettings, QSize, Qt,
                            QThread, QThreadPool, QTimer, Signal, Slot)
from PySide6.QtGui import QFont, QGuiApplication, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractScrollArea, QAbstractSpinBox, QApplication, QCheckBox, QComboBox,
    QDoubleSpinBox, QFileDialog, QFrame,
    QHBoxLayout, QLabel, QLineEdit, QMainWindow, QPlainTextEdit, QProgressBar,
    QPushButton, QScrollArea, QSlider, QSpinBox, QSplitter, QStackedWidget,
    QVBoxLayout, QWidget,
)

from octans import background, platesolve, stretch
from octans.catalog import Catalog
from octans.pushto import guide
from svbony.camera import list_cameras
from ui.audio import Beeper
from ui import icons
from ui.design import (Card, HealthStrip, ModeRail, Palette, Stat, T_BODY,
                       T_DISPLAY, T_H1, T_H2, T_L, T_MONO, T_SMALL, T_XL,
                       image_lut, palette, stylesheet)
from ui.worker import CaptureWorker, Config

pg.setConfigOptions(imageAxisOrder="row-major", antialias=False)

MODES = [
    ("frame",  "1  ENQUADRAR", "achar e centralizar o alvo — frame ao vivo, "
                              "plate solve e push-to", "frame"),
    ("focus",  "2  FOCAR",     "HFR grande, tendência, lupa 5× e bipe por frame",
                              "focus"),
    ("stack",  "3  INTEGRAR",  "empilhar, gravar e acompanhar a plataforma", "stack"),
    ("adjust", "4  AJUSTAR",   "stretch, presets e salvar", "adjust"),
]

STRETCH_PRESETS = {"suave": (0.15, 3.2), "médio": (0.25, 2.8), "forte": (0.40, 2.2)}

# Sítio de observação: Ouro Branco, RN (Seridó). Centro do município.
# A latitude entra direto na altitude do polo celeste, então erro em latitude
# vira erro de igual tamanho na correção de alinhamento polar: 0,1° de latitude
# = 6' de erro. Se for afinar, use a coordenada real do quintal.
SITE_LAT = -6.7003
SITE_LON = -36.9436
SITE_ELEV = 350.0

# Óptica: dobsoniano de 1200 mm com a SV405CC (pixel de 4,63 um).
#   bin1 -> 0,796"/px    bin2 -> 1,592"/px    campo 55,0' x 37,4' nos dois
FOCAL_MM = 1200.0
PIXEL_UM = 4.63


def pixel_scale(bin: int, halved: bool = False) -> float:
    """Escala em arcsec/px. `halved` para a imagem de luminância, que sai com
    metade da resolução do frame (soma das quadras 2x2 do mosaico Bayer) e
    portanto o dobro da escala. Passar a escala errada ao solver é a diferença
    entre resolver em um segundo e não resolver."""
    s = 206.265 * PIXEL_UM / FOCAL_MM * bin
    return s * 2 if halved else s


class _SemRoda(QObject):
    """Impede que a roda do mouse altere valores de campos.

    Comportamento padrão do Qt: rolar sobre um spinbox, combo ou slider muda o
    valor. Num painel rolável isso significa trocar exposição, ganho ou alvo de
    temperatura sem querer, só de procurar outro controle — e no escuro você não
    percebe que mudou.

    O evento é engolido no campo e reenviado ao viewport da área rolável mais
    próxima, para o painel continuar rolando normalmente. Sem esse reenvio, o
    campo viraria um buraco morto no meio da rolagem.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._reenviando = False

    def eventFilter(self, obj, ev):
        if ev.type() != QEvent.Type.Wheel or self._reenviando:
            return False
        alvo = obj.parentWidget()
        while alvo is not None and not isinstance(alvo, QAbstractScrollArea):
            alvo = alvo.parentWidget()
        if alvo is not None:
            self._reenviando = True
            try:
                QApplication.sendEvent(alvo.viewport(), ev)
            finally:
                self._reenviando = False
        return True


# --------------------------------------------------------------- plate solve
class SolveSignals(QObject):
    done = Signal(object)
    msg = Signal(str)


class SolveTask(QRunnable):
    def __init__(self, lum, scale_hint, hint):
        super().__init__()
        self.lum, self.scale_hint, self.hint = lum, scale_hint, hint
        self.signals = SolveSignals()

    def run(self):
        if not any(platesolve.solvers_available().values()):
            self.signals.msg.emit(platesolve.install_hint())
            self.signals.done.emit(None)
            return
        self.signals.msg.emit("resolvendo campo…")
        self.signals.done.emit(
            platesolve.solve(self.lum, scale_arcsec=self.scale_hint, hint=self.hint))


# --------------------------------------------------------------- janela
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("octans")
        av = QGuiApplication.primaryScreen().availableGeometry()
        self.resize(min(1500, int(av.width() * 0.98)), min(980, int(av.height() * 0.96)))

        self.settings = QSettings("octans", "eaa")
        self.worker: CaptureWorker | None = None
        self.thread: QThread | None = None
        self.beeper = Beeper()
        self.pool = QThreadPool.globalInstance()

        self._theme = self.settings.value("theme", "dark")
        self._night_level = int(self.settings.value("night_level", 1))
        self._touch = self.settings.value("touch", "false") == "true"
        self.pal: Palette = palette(self._theme, self._night_level)

        self._view = "stack"
        self._live = self._stack = self._q = self._q_src = None
        self._black = 0.0
        self._dark_path = None
        self._flat_path = None
        self._prep = None
        self._prep_key = None
        self._replay_folder = ""
        self._solution = None
        self._target = None
        self._catalog: Catalog | None = None
        self._anno: list = []
        self._iconed: list[tuple] = []
        self._paused = False
        self._cool: dict = {}
        self._state = "idle"
        self._t_frame = 0.0
        self._exposure = 5.0
        self._last_stats: dict = {}

        self._build()
        self._bloquear_roda()
        self._shortcuts()
        self.refresh_cameras()
        self._restore()
        self.apply_theme()
        self.set_mode("frame")

        self.tick = QTimer(self)
        self.tick.timeout.connect(self._on_tick)
        self.tick.start(120)

    # ================================================================ layout
    def _build(self) -> None:
        root = QWidget()
        root.setObjectName("root")      # o fundo da janela é declarado aqui, não
                                        # no seletor genérico QWidget
        outer = QVBoxLayout(root)
        outer.setContentsMargins(8, 8, 8, 6)
        outer.setSpacing(8)

        outer.addWidget(self._status_bar())
        self.alert = QLabel("")
        self.alert.setFont(T_H2())
        self.alert.setVisible(False)
        self.alert.setWordWrap(True)
        outer.addWidget(self.alert)

        self.split = QSplitter(Qt.Horizontal)
        self.split.addWidget(self._left())
        self.split.addWidget(self._right())
        self.split.setStretchFactor(0, 0)
        self.split.setStretchFactor(1, 1)
        self.split.setSizes([368, max(self.width() - 368, 520)])
        outer.addWidget(self.split, 1)

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setFont(T_MONO())
        self.log.setMaximumHeight(120)
        self.log.setVisible(False)
        outer.addWidget(self.log)
        self.setCentralWidget(root)

    # ---------------------------------------------------------- barra de status
    def _status_bar(self) -> QWidget:
        """Sinais vitais. Todos os itens têm a mesma forma — rótulo pequeno em
        cima, valor grande embaixo — inclusive o estado e a exposição, para a
        linha ler como uma linha e não como uma colagem de caixinhas."""
        card = Card()
        top = QHBoxLayout()
        top.setSpacing(22)
        top.setContentsMargins(2, 0, 2, 0)

        self.st_state = Stat("estado", "● OCIOSO", big=True, min_width=168)
        self.st_integ = Stat("integração", "—", big=True)
        self.st_frames = Stat("frames", "—", big=True)
        self.st_hfr = Stat("HFR", "—", big=True, min_width=88)
        self.st_platform = Stat("plataforma", "—", big=True, min_width=116)
        for st in (self.st_state, self.st_integ, self.st_frames,
                   self.st_hfr, self.st_platform):
            top.addWidget(st)
        top.addStretch(1)

        # bloco da exposição, na mesma estrutura dos indicadores
        expo = QWidget()
        ev = QVBoxLayout(expo)
        ev.setContentsMargins(0, 1, 0, 1)
        ev.setSpacing(3)
        self.phase = QLabel("EXPOSIÇÃO")
        self.phase.setObjectName("statLabel")
        self.phase.setFont(_stat_label_font())
        self.prog = QProgressBar()
        self.prog.setTextVisible(False)
        self.prog.setFixedHeight(10)
        self.prog.setMinimumWidth(210)
        ev.addWidget(self.phase)
        ev.addWidget(self.prog)
        ev.addStretch(1)
        top.addWidget(expo)

        # botões alinhados com a linha dos valores, não com a dos rótulos
        btns = QWidget()
        bv = QVBoxLayout(btns)
        bv.setContentsMargins(0, 1, 0, 1)
        bv.setSpacing(3)
        spacer = QLabel(" ")
        spacer.setFont(_stat_label_font())
        bv.addWidget(spacer)
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)
        self.btn_start = QPushButton("Iniciar captura")
        self._ic(self.btn_start, "play")
        self.btn_start.setToolTip(
            "Liga a câmera. O que é feito com os frames depende do modo:\n"
            "Enquadrar e Focar só capturam e medem estrelas; Integrar empilha,\n"
            "grava os subs e mede a plataforma.")
        self.btn_start.clicked.connect(self.start)

        self.btn_pause = QPushButton("Pausar")
        self._ic(self.btn_pause, "pause")
        self.btn_pause.setEnabled(False)
        self.btn_pause.setToolTip(
            "Para de puxar frames sem fechar a câmera.\n"
            "Stack, gravação e cronômetro da plataforma ficam intactos —\n"
            "Continuar retoma no mesmo acumulador.")
        self.btn_pause.clicked.connect(self.toggle_pause)

        self.btn_finish = QPushButton("Finalizar")
        self._ic(self.btn_finish, "stop")
        self.btn_finish.setEnabled(False)
        self.btn_finish.setToolTip(
            "Encerra a sessão e fecha a câmera.\n"
            "O stack final é sempre gravado em disco antes de encerrar.")
        self.btn_finish.clicked.connect(self.finish)

        for b in (self.btn_start, self.btn_pause, self.btn_finish):
            row.addWidget(b)
        bv.addLayout(row)
        bv.addStretch(1)
        top.addWidget(btns)

        card.add_layout(top)
        self.health = HealthStrip()
        self.health.setToolTip(
            "últimos frames: aceito em barra baixa, rejeitado em barra cheia.\n"
            "A sequência importa mais que a contagem — rejeições seguidas são\n"
            "nuvem, orvalho ou alvo fora do quadro.")
        card.add(self.health)
        return card

    # ---------------------------------------------------------------- esquerda
    def _left(self) -> QWidget:
        col = QWidget()
        v = QVBoxLayout(col)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(8)

        self.rail = ModeRail(MODES)
        self.rail.changed.connect(self.set_mode)
        v.addWidget(self.rail)

        self.panels = QStackedWidget()
        self._panel_index = {}
        for key, build in (("frame", self._panel_frame), ("focus", self._panel_focus),
                           ("stack", self._panel_stack), ("adjust", self._panel_adjust)):
            sa = QScrollArea()
            sa.setWidget(build())
            sa.setWidgetResizable(True)
            sa.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            sa.setFrameShape(QScrollArea.NoFrame)
            self._panel_index[key] = self.panels.addWidget(sa)
        v.addWidget(self.panels, 1)

        v.addWidget(self._capture_strip())
        col.setMinimumWidth(348)
        col.setMaximumWidth(452)
        self._left_col = col
        return col

    def _capture_strip(self) -> QWidget:
        """Sempre visível, em qualquer modo: são os quatro controles que você
        mexe a noite toda. Duas linhas em vez de quatro — a altura da coluna é o
        recurso escasso."""
        c = Card("captura")
        r1 = QHBoxLayout()
        r1.setSpacing(6)
        self.sp_exp = QDoubleSpinBox()
        self.sp_exp.setRange(0.001, 600.0)
        self.sp_exp.setDecimals(3)
        self.sp_exp.setValue(5.0)
        self.sp_exp.setSuffix(" s")
        self.sp_exp.valueChanged.connect(self._exposure_changed)
        self.sp_gain = QSpinBox()
        self.sp_gain.setRange(0, 570)
        self.sp_gain.setValue(250)
        self.sp_gain.valueChanged.connect(lambda x: self._req(gain=x))
        r1.addWidget(_tag("exp"))
        r1.addWidget(self.sp_exp, 1)
        r1.addWidget(_tag("ganho"))
        r1.addWidget(self.sp_gain, 1)
        c.add_layout(r1)

        r2 = QHBoxLayout()
        r2.setSpacing(6)
        self.sp_offset = QSpinBox()
        self.sp_offset.setRange(0, 80)
        self.sp_offset.setValue(20)
        self.sp_offset.setToolTip("offset 0 trunca a cauda esquerda do ruído")
        self.sp_offset.valueChanged.connect(lambda x: self._req(offset=x))
        self.cb_bin = QComboBox()
        self.cb_bin.addItems(["1", "2", "3", "4"])
        self.cb_bin.setCurrentText("2")
        self.cb_bin.setToolTip("bin2 é o único bin >1 sem perda nesta câmera;\n"
                               "bin3 e bin4 grampeiam as altas luzes")
        self.cb_bin.currentTextChanged.connect(self._bin_changed)
        r2.addWidget(_tag("offset"))
        r2.addWidget(self.sp_offset, 1)
        r2.addWidget(_tag("bin"))
        r2.addWidget(self.cb_bin, 1)
        c.add_layout(r2)

        # Refrigeração fica aqui, sempre visível, e não num painel de modo:
        # resfriar leva quinze a vinte minutos, e você quer ligar isso já no
        # enquadramento para estar estável na hora de integrar.
        r3 = QHBoxLayout()
        r3.setSpacing(6)
        self.btn_cooler = QPushButton("TEC")
        self.btn_cooler.setCheckable(True)
        self.btn_cooler.setMaximumWidth(72)
        self.btn_cooler.setToolTip(
            "Liga a refrigeração em rampa controlada.\n"
            "Mandar o alvo final de uma vez faz o TEC puxar 100% e a temperatura\n"
            "despencar, o que estressa a junta do sensor e favorece condensação.")
        self.btn_cooler.toggled.connect(self._cooler_toggled)
        self.sp_temp = QDoubleSpinBox()
        self.sp_temp.setRange(-40, 30)
        self.sp_temp.setValue(-10)
        self.sp_temp.setSuffix(" °C")
        self.sp_temp.valueChanged.connect(lambda x: self._req(target_temp=x))
        r3.addWidget(self.btn_cooler)
        r3.addWidget(_tag("alvo"))
        r3.addWidget(self.sp_temp, 1)
        c.add_layout(r3)
        self.lbl_cool = QLabel("TEC desligado")
        self.lbl_cool.setFont(T_MONO())
        self.lbl_cool.setWordWrap(True)
        c.add(self.lbl_cool)
        return c

    # ------------------------------------------------------- painel ENQUADRAR
    def _panel_frame(self) -> QWidget:
        w = QWidget(); v = QVBoxLayout(w); v.setContentsMargins(0, 0, 0, 0); v.setSpacing(8)

        c = Card("fonte")
        row = QHBoxLayout()
        self.cb_source = QComboBox(); self.cb_source.addItems(["câmera", "replay"])
        self.cb_source.currentIndexChanged.connect(self._source_changed)
        row.addWidget(self.cb_source)
        self.cb_camera = QComboBox()
        row.addWidget(self.cb_camera, 1)
        self.btn_refresh = QPushButton(); self.btn_refresh.setMaximumWidth(38)
        self.btn_refresh.setToolTip("reprocurar câmeras")
        self._ic(self.btn_refresh, "refresh")
        self.btn_refresh.clicked.connect(self.refresh_cameras)
        row.addWidget(self.btn_refresh)
        self.btn_folder = QPushButton("pasta…")
        self._ic(self.btn_folder, "folder")
        self.btn_folder.clicked.connect(self.pick_replay)
        self.btn_folder.setVisible(False)
        row.addWidget(self.btn_folder)
        c.add_layout(row)
        v.addWidget(c)

        c = Card("o que este modo faz")
        c.add(_hint("Captura e mede estrelas, sem empilhar e sem gravar. Passe "
                    "para Integrar quando o alvo estiver centrado e focado — é "
                    "lá que o stack e a gravação começam."))
        v.addWidget(c)

        c = Card("exposição rápida")
        row = QHBoxLayout()
        for s in (0.2, 0.5, 1.0, 2.0):
            b = QPushButton(f"{s:g}s")
            b.clicked.connect(lambda _=False, x=s: self.sp_exp.setValue(x))
            row.addWidget(b)
        c.add_layout(row)
        c.add(_hint("exposições curtas para enquadrar; volte a subs longos "
                    "antes de integrar"))
        v.addWidget(c)

        c = Card("onde estou apontando")
        row = QHBoxLayout()
        self.btn_solve = QPushButton("Resolver campo")
        self._ic(self.btn_solve, "target")
        self.btn_solve.clicked.connect(self.solve_now)
        self.chk_anno = QCheckBox("anotar")
        self.chk_anno.toggled.connect(lambda: self._draw_anno())
        row.addWidget(self.btn_solve, 1); row.addWidget(self.chk_anno)
        c.add_layout(row)
        self.lbl_solution = QLabel("nenhuma solução")
        self.lbl_solution.setFont(T_MONO()); self.lbl_solution.setWordWrap(True)
        c.add(self.lbl_solution)
        v.addWidget(c)

        c = Card("push-to")
        row = QHBoxLayout()
        self.ed_goto = QLineEdit()
        self.ed_goto.setPlaceholderText("M8, NGC 5128, Sombrero…")
        self.ed_goto.returnPressed.connect(self.set_goto)
        b = QPushButton("ir"); b.setMaximumWidth(62)
        self._ic(b, "arrow"); b.clicked.connect(self.set_goto)
        row.addWidget(self.ed_goto, 1); row.addWidget(b)
        c.add_layout(row)
        self.lbl_goto = QLabel("nenhum alvo"); self.lbl_goto.setWordWrap(True)
        self.lbl_goto.setFont(T_MONO())
        c.add(self.lbl_goto)
        v.addWidget(c)

        v.addStretch(1)
        return w

    # ---------------------------------------------------------- painel FOCAR
    def _panel_focus(self) -> QWidget:
        w = QWidget(); v = QVBoxLayout(w); v.setContentsMargins(0, 0, 0, 0); v.setSpacing(8)
        c = Card()
        self.lbl_hfr = QLabel("—")
        self.lbl_hfr.setFont(T_DISPLAY())
        self.lbl_hfr.setAlignment(Qt.AlignCenter)
        c.add(self.lbl_hfr)
        self.lbl_hfr_sub = QLabel("HFR mediano")
        self.lbl_hfr_sub.setFont(T_SMALL()); self.lbl_hfr_sub.setAlignment(Qt.AlignCenter)
        c.add(self.lbl_hfr_sub)
        self.lbl_verdict = QLabel("—")
        self.lbl_verdict.setFont(T_H2()); self.lbl_verdict.setAlignment(Qt.AlignCenter)
        self.lbl_verdict.setWordWrap(True)
        c.add(self.lbl_verdict)
        v.addWidget(c)

        c = Card("o que este modo faz")
        c.add(_hint("Captura e mede o foco, sem empilhar e sem gravar. O stack "
                    "que já existir é preservado."))
        v.addWidget(c)

        c = Card("realimentação")
        self.chk_beep = QCheckBox("bipe por frame")
        self.chk_beep.toggled.connect(lambda on: setattr(self.beeper, "enabled", on))
        c.add(self.chk_beep)
        c.add(_hint("o tom sobe conforme o HFR cai. Com a mão no focalizador "
                    "você não está olhando a tela."))
        b = QPushButton("zerar melhor foco")
        self._ic(b, "refresh")
        b.clicked.connect(lambda: self._flag("reset_focus_best"))
        c.add(b)
        v.addWidget(c)

        c = Card("dica")
        c.add(_hint("A tendência em px/min é o sinal que importa enquanto você "
                    "gira: o valor instantâneo oscila com o seeing.\n\n"
                    "Use exposições de 1 a 2 s aqui."))
        v.addWidget(c)
        v.addStretch(1)
        return w

    # -------------------------------------------------------- painel INTEGRAR
    def _panel_stack(self) -> QWidget:
        w = QWidget(); v = QVBoxLayout(w); v.setContentsMargins(0, 0, 0, 0); v.setSpacing(8)

        c = Card("integração")
        self.btn_integrar = QPushButton("Começar a integrar")
        self.btn_integrar.setCheckable(True)
        self.btn_integrar.setMinimumHeight(38)
        self.btn_integrar.setToolTip(
            "Começa a empilhar, gravar os subs e medir a plataforma.\n"
            "Trocar de modo não dispara isso sozinho: entrar aqui só mostra o\n"
            "painel. Escrever em disco e consumir curso da plataforma é decisão\n"
            "sua, não efeito colateral de clicar numa aba.")
        self._ic(self.btn_integrar, "play")
        self.btn_integrar.toggled.connect(self._integrar_toggled)
        c.add(self.btn_integrar)
        self.lbl_integrar = QLabel("parada — captura segue, nada é acumulado")
        self.lbl_integrar.setFont(T_SMALL())
        self.lbl_integrar.setWordWrap(True)
        c.add(self.lbl_integrar)
        v.addWidget(c)

        c = Card("alvo e gravação")
        self.ed_target = QLineEdit()
        self.ed_target.setPlaceholderText("M42, Centaurus A…")
        self.ed_target.editingFinished.connect(
            lambda: self._req(target_name=self.ed_target.text()))
        c.field("alvo", self.ed_target)
        self.chk_record = QCheckBox("gravar subs brutos"); self.chk_record.setChecked(True)
        c.add(self.chk_record)
        row = QHBoxLayout()
        self.chk_compress = QCheckBox("RICE"); self.chk_compress.setChecked(True)
        self.chk_compress.setToolTip("compressão sem perda, tipicamente 2x")
        self.sp_every = QSpinBox(); self.sp_every.setRange(1, 20); self.sp_every.setValue(1)
        self.sp_every.setToolTip("grava 1 de cada N subs")
        self.sp_every.valueChanged.connect(lambda x: self._req(record_every=x))
        row.addWidget(self.chk_compress); row.addWidget(QLabel("1 a cada"))
        row.addWidget(self.sp_every)
        c.add_layout(row)
        self.lbl_disk = QLabel("—"); self.lbl_disk.setFont(T_MONO())
        self.lbl_disk.setWordWrap(True)
        c.add(self.lbl_disk)
        v.addWidget(c)

        c = Card("stack")
        row = QHBoxLayout()
        self.btn_dark = QPushButton("dark…")
        self._ic(self.btn_dark, "dark")
        self.btn_dark.clicked.connect(self.pick_dark)
        self.btn_grava_dark = QPushButton("gravar dark")
        self._ic(self.btn_grava_dark, "dark")
        self.btn_grava_dark.setToolTip(
            "Grava um master dark com a exposição, ganho, offset, bin e\n"
            "temperatura da sessão em curso — que é justamente o que um dark\n"
            "precisa casar. Tampe o sensor antes.")
        self.btn_grava_dark.clicked.connect(self.gravar_dark)
        row.addWidget(self.btn_grava_dark)
        self.btn_flat = QPushButton("flat…")
        self._ic(self.btn_flat, "grid")
        self.btn_flat.setToolTip("corrige vinheteamento e poeira; sem ele os "
                                 "cantos ficam escuros e o autostretch denuncia")
        self.btn_flat.clicked.connect(self.pick_flat)
        row.addWidget(self.btn_flat)
        self.btn_reset = QPushButton("zerar")
        self._ic(self.btn_reset, "trash")
        self.btn_reset.clicked.connect(self.zerar_stack)
        row.addWidget(self.btn_dark); row.addWidget(self.btn_reset)
        c.add_layout(row)
        self.lbl_dark = QLabel("sem dark"); self.lbl_dark.setFont(T_MONO())
        c.add(self.lbl_dark)
        self.lbl_flat = QLabel("sem flat"); self.lbl_flat.setFont(T_MONO())
        c.add(self.lbl_flat)
        self.chk_weight = QCheckBox("ponderar por qualidade")
        self.chk_weight.setChecked(True)
        self.chk_weight.setToolTip(
            "Pesa cada frame por fluxo/(ruído²·FWHM²). Faz diferença junto com o\n"
            "registro por votação, que deixa entrar frames marginais: o peso\n"
            "impede que eles puxem o stack para baixo.")
        c.add(self.chk_weight)
        self.sp_sigma = QDoubleSpinBox(); self.sp_sigma.setRange(0.0, 6.0)
        self.sp_sigma.setValue(3.0); self.sp_sigma.setSingleStep(0.5)
        self.sp_sigma.valueChanged.connect(
            lambda x: self._req(sigma_clip=(x if x > 0 else None)))
        c.field("sigma clip", self.sp_sigma, "0 desliga. Rejeita satélite e avião.")
        self.cb_rigor = QComboBox()
        self.cb_rigor.addItems(["tolerante", "normal", "rigoroso"])
        self.cb_rigor.setCurrentText("normal")
        self.cb_rigor.setToolTip(
            "Ajusta em grupo os limiares de FWHM, arraste e salto de fundo.\n"
            "Tolerante aproveita mais frames em noite ruim; rigoroso só deixa\n"
            "passar o que está bom.")
        self.cb_rigor.currentTextChanged.connect(lambda t: self._req(rigor=t))
        c.field("rigor", self.cb_rigor)
        self.btn_segment = QPushButton("Novo segmento   (espaço)")
        self._ic(self.btn_segment, "segment")
        self.btn_segment.setToolTip(
            "Após recentrar o tubo. Não zera o stack: relaxa os limiares e\n"
            "reextrai a referência do que já foi integrado.")
        self.btn_segment.clicked.connect(lambda: self._flag("new_segment"))
        c.add(self.btn_segment)
        v.addWidget(c)

        c = Card("plataforma equatorial")
        self.sp_platform = QDoubleSpinBox(); self.sp_platform.setRange(5, 240)
        self.sp_platform.setValue(60); self.sp_platform.setSuffix(" min")
        self.sp_platform.valueChanged.connect(lambda x: self._req(platform_minutes=x))
        c.field("curso total", self.sp_platform)
        b = QPushButton("Resetei a plataforma")
        self._ic(b, "rotate")
        b.clicked.connect(lambda: self._flag("platform_reset"))
        b.setToolTip("Reinicia o cronômetro sem zerar o stack.")
        c.add(b)
        self.lbl_advice = QLabel("—"); self.lbl_advice.setWordWrap(True)
        self.lbl_advice.setFont(T_MONO())
        c.add(self.lbl_advice)
        v.addWidget(c)
        v.addStretch(1)
        return w

    # --------------------------------------------------------- painel AJUSTAR
    def _panel_adjust(self) -> QWidget:
        w = QWidget(); v = QVBoxLayout(w); v.setContentsMargins(0, 0, 0, 0); v.setSpacing(8)

        c = Card("stretch")
        row = QHBoxLayout()
        self.cb_algo = QComboBox()
        self.cb_algo.addItems(["MTF (padrão)", "arcsinh"])
        self.cb_algo.setToolTip(
            "MTF é o padrão, mesma família do PixInsight e do SharpCap.\n"
            "arcsinh é mais gentil nas altas luzes e preserva a cor do núcleo\n"
            "de estrela brilhante, globular e M42 — onde o MTF estoura em branco.")
        self.cb_algo.currentIndexChanged.connect(lambda: self._render(True))
        row.addWidget(QLabel("algoritmo")); row.addWidget(self.cb_algo, 1)
        c.add_layout(row)
        row = QHBoxLayout()
        for name in STRETCH_PRESETS:
            b = QPushButton(name)
            b.clicked.connect(lambda _=False, n=name: self.apply_preset(n))
            row.addWidget(b)
        c.add_layout(row)
        self.chk_auto = QCheckBox("autostretch"); self.chk_auto.setChecked(True)
        self.chk_auto.toggled.connect(lambda: self._render(True))
        c.add(self.chk_auto)
        self.sl_bg, wbg = self._slider("fundo", 5, 60, 25, 100.0)
        self.sl_clip, wcl = self._slider("sombras", 10, 60, 28, 10.0)
        c.add(wbg); c.add(wcl)
        self.chk_linked = QCheckBox("ligado entre canais")
        self.chk_linked.setToolTip(
            "Desligado: cada canal em separado, neutraliza o fundo — melhor sob\n"
            "poluição luminosa. Ligado: preserva as razões de cor.")
        self.chk_linked.toggled.connect(lambda: self._render(True))
        c.add(self.chk_linked)
        self.sl_sat, wsat = self._slider("saturação", 0, 25, 10, 10.0)
        self.sl_sat.setToolTip("esticar comprime a distância entre canais e a "
                               "nebulosa fica pálida; isto devolve a cor sem "
                               "mexer no brilho")
        c.add(wsat)
        v.addWidget(c)

        c = Card("gradiente de fundo")
        self.chk_bg = QCheckBox("remover gradiente")
        self.chk_bg.setToolTip(
            "Ajusta uma superfície suave ao fundo e subtrai. Corrige poluição\n"
            "luminosa e o amp glow residual que sobra do dark — que é justamente\n"
            "o que o autostretch amplifica melhor.\n"
            "Só afeta a exibição; o acumulador não é alterado.")
        self.chk_bg.toggled.connect(lambda: self._render(True))
        c.add(self.chk_bg)
        row = QHBoxLayout()
        self.sp_bg_deg = QSpinBox(); self.sp_bg_deg.setRange(1, 2)
        self.sp_bg_deg.setValue(2)
        self.sp_bg_deg.setToolTip("1 corrige inclinação; 2 pega o amp glow, que é "
                                  "uma mancha de canto.\nGrau maior começa a comer "
                                  "nebulosa extensa.")
        self.sp_bg_deg.valueChanged.connect(lambda: self._render(True))
        row.addWidget(QLabel("grau")); row.addWidget(self.sp_bg_deg)
        self.lbl_bg = QLabel(""); self.lbl_bg.setFont(T_MONO())
        row.addWidget(self.lbl_bg, 1)
        c.add_layout(row)
        v.addWidget(c)

        c = Card("vista")
        row = QHBoxLayout()
        for label, name, fn in (("ajustar", "fit", self.fit_view),
                                ("1:1", "grid", self.zoom_one),
                                ("salvar", "save", self.save)):
            b = QPushButton(label); self._ic(b, name)
            b.clicked.connect(fn); row.addWidget(b)
        c.add_layout(row)
        v.addWidget(c)

        c = Card("local — Ouro Branco, RN")
        self.sp_lat = QDoubleSpinBox(); self.sp_lat.setRange(-90, 90)
        self.sp_lat.setDecimals(4); self.sp_lat.setValue(SITE_LAT); self.sp_lat.setSuffix("°")
        self.sp_lon = QDoubleSpinBox(); self.sp_lon.setRange(-180, 180)
        self.sp_lon.setDecimals(4); self.sp_lon.setValue(SITE_LON); self.sp_lon.setSuffix("°")
        c.field("latitude", self.sp_lat)
        c.field("longitude", self.sp_lon)
        v.addWidget(c)

        c = Card("tela")
        self.btn_night = QPushButton("modo noturno   (N)")
        self._ic(self.btn_night, "moon")
        self.btn_night.setCheckable(True)
        self.btn_night.toggled.connect(self.toggle_night)
        c.add(self.btn_night)
        self.sl_night, wn = self._slider("brilho noturno", 0, 2, 1, 1.0)
        self.sl_night.valueChanged.connect(self._night_changed)
        c.add(wn)
        self.chk_touch = QCheckBox("alvos de clique grandes")
        self.chk_touch.setToolTip("no escuro, com frio e sem óculos, alvo pequeno "
                                  "custa caro")
        self.chk_touch.toggled.connect(self._touch_changed)
        c.add(self.chk_touch)
        self.btn_full = QPushButton("só a imagem   (F)")
        self._ic(self.btn_full, "expand")
        self.btn_full.setCheckable(True)
        self.btn_full.toggled.connect(self.toggle_full)
        c.add(self.btn_full)
        b = QPushButton("mostrar log   (L)")
        self._ic(b, "list")
        b.setCheckable(True)
        # lookup diferido: o log é criado depois dos painéis
        b.toggled.connect(lambda on: self.log.setVisible(on))
        c.add(b)
        self.btn_log = b
        v.addWidget(c)
        v.addStretch(1)
        return w

    # ---------------------------------------------------------------- direita
    def _right(self) -> QWidget:
        w = QWidget(); v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0); v.setSpacing(8)

        # Alternância stack / último frame, sempre acessível. Estava enterrada
        # num painel de configuração, inútil durante a integração — que é
        # exatamente quando você precisa olhar o sub individual para ver nuvem,
        # tubo esbarrado ou estrela arrastada. O stack faz média e esconde isso.
        bar = QWidget()
        hb = QHBoxLayout(bar)
        hb.setContentsMargins(2, 0, 2, 0)
        hb.setSpacing(6)
        self.btn_view_stack = QPushButton("Stack")
        self.btn_view_live = QPushButton("Último frame")
        for b, mode in ((self.btn_view_stack, "stack"), (self.btn_view_live, "live")):
            b.setCheckable(True)
            b.clicked.connect(lambda _=False, m=mode: self._set_view(m))
            hb.addWidget(b)
        self._ic(self.btn_view_stack, "stack", 15)
        self._ic(self.btn_view_live, "camera", 15)
        self.lbl_view = QLabel("—")
        self.lbl_view.setFont(T_MONO())
        hb.addSpacing(10)
        hb.addWidget(self.lbl_view, 1)
        hint = QLabel("V alterna")
        hint.setObjectName("statLabel")
        hint.setFont(T_SMALL())
        hb.addWidget(hint)
        v.addWidget(bar)

        self.view = pg.GraphicsLayoutWidget()
        self.vb = self.view.addViewBox(lockAspect=True, invertY=True)
        self.img = pg.ImageItem()
        self.vb.addItem(self.img)
        v.addWidget(self.view, 1)

        self.context = QStackedWidget()
        self.context.setMaximumHeight(210)
        self._ctx_index = {
            "frame": self.context.addWidget(self._ctx_frame()),
            "focus": self.context.addWidget(self._ctx_focus()),
            "stack": self.context.addWidget(self._ctx_stack()),
            "adjust": self.context.addWidget(self._ctx_adjust()),
        }
        v.addWidget(self.context)
        self._right_col = w
        return w

    def _ctx_frame(self) -> QWidget:
        c = Card("direção do alvo")
        self.lbl_goto_dir = QLabel("defina um alvo e resolva o campo")
        self.lbl_goto_dir.setFont(T_XL())
        self.lbl_goto_dir.setWordWrap(True)
        c.add(self.lbl_goto_dir)
        self.lbl_objects = QLabel("")
        self.lbl_objects.setFont(T_MONO())
        self.lbl_objects.setWordWrap(True)
        c.add(self.lbl_objects, 1)
        return c

    def _ctx_focus(self) -> QWidget:
        w = QWidget(); h = QHBoxLayout(w)
        h.setContentsMargins(0, 0, 0, 0); h.setSpacing(8)
        c = Card("lupa 5×")
        self.loupe_view = pg.GraphicsLayoutWidget()
        vb = self.loupe_view.addViewBox(lockAspect=True, invertY=True)
        vb.setMouseEnabled(False, False)
        self.loupe_img = pg.ImageItem()
        vb.addItem(self.loupe_img)
        c.add(self.loupe_view, 1)
        c.setMaximumWidth(230)
        h.addWidget(c)
        c2 = Card("HFR ao longo do tempo")
        self.plot_focus = pg.PlotWidget()
        self.curve_focus = self.plot_focus.plot()
        self.line_best = pg.InfiniteLine(angle=0)
        self.plot_focus.addItem(self.line_best)
        c2.add(self.plot_focus, 1)
        h.addWidget(c2, 1)
        return w

    def _ctx_stack(self) -> QWidget:
        w = QWidget(); h = QHBoxLayout(w)
        h.setContentsMargins(0, 0, 0, 0); h.setSpacing(8)
        c = Card("histograma")
        self.hist = pg.PlotWidget()
        self.hist.setLogMode(False, True)
        self.hist_curve = self.hist.plot()
        self.hist_black = pg.InfiniteLine(angle=90)
        self.hist.addItem(self.hist_black)
        c.add(self.hist, 1)
        h.addWidget(c, 1)

        c2 = Card("frames descartados")
        self.lbl_rej = QLabel("nenhum")
        self.lbl_rej.setFont(T_MONO())
        self.lbl_rej.setWordWrap(True)
        self.lbl_rej.setToolTip("por que os frames estão saindo — o que dizer se "
                                "vale mexer no rigor, refocar ou esperar a nuvem")
        c2.add(self.lbl_rej, 1)
        c2.setMaximumWidth(210)
        h.addWidget(c2)

        c2 = Card("rotação residual")
        grid = QWidget(); g = QVBoxLayout(grid)
        g.setContentsMargins(0, 0, 0, 0); g.setSpacing(1)
        self.st_rot = Stat("rotação", "—")
        self.st_useful = Stat("integração útil", "—")
        self.st_smear = Stat("arrasto no canto", "—")
        for s in (self.st_rot, self.st_useful, self.st_smear):
            g.addWidget(s)
        c2.add(grid)
        c2.setMaximumWidth(230)
        h.addWidget(c2)
        return w

    def _ctx_adjust(self) -> QWidget:
        c = Card("histograma")
        self.hist2 = pg.PlotWidget()
        self.hist2.setLogMode(False, True)
        self.hist2_curve = self.hist2.plot()
        self.hist2_black = pg.InfiniteLine(angle=90)
        self.hist2.addItem(self.hist2_black)
        c.add(self.hist2, 1)
        return c

    def _slider(self, name, lo, hi, val, div):
        hold = QWidget(); h = QHBoxLayout(hold)
        h.setContentsMargins(0, 0, 0, 0)
        lab = QLabel(name); lab.setFont(T_BODY()); lab.setMinimumWidth(96)
        s = QSlider(Qt.Horizontal); s.setRange(lo, hi); s.setValue(val)
        out = QLabel(f"{val/div:.2f}"); out.setFont(T_MONO()); out.setMinimumWidth(40)
        s.valueChanged.connect(lambda x: (out.setText(f"{x/div:.2f}"), self._render()))
        h.addWidget(lab); h.addWidget(s, 1); h.addWidget(out)
        s._div = div
        return s, hold

    def _shortcuts(self) -> None:
        for i, mode in enumerate(MODES):
            QShortcut(QKeySequence(str(i + 1)), self,
                      lambda k=mode[0]: self.rail.select(k))
        for key, fn in (("V", self.toggle_view),
                        ("F", lambda: self.btn_full.toggle()),
                        ("N", lambda: self.btn_night.toggle()),
                        ("L", lambda: self.btn_log.toggle()),
                        ("Space", lambda: self._flag("new_segment")),
                        ("Ctrl+S", self.save)):
            QShortcut(QKeySequence(key), self, fn)

    def _bloquear_roda(self) -> None:
        """Aplica o filtro a todo campo de valor. Os gráficos e a imagem ficam
        de fora de propósito: ali a roda dá zoom, que é o esperado."""
        self._filtro_roda = _SemRoda(self)
        n = 0
        for tipo in (QAbstractSpinBox, QComboBox, QSlider):
            for w in self.findChildren(tipo):
                w.installEventFilter(self._filtro_roda)
                # StrongFocus: sem isto a roda ainda daria foco ao campo
                w.setFocusPolicy(Qt.StrongFocus)
                n += 1
        self._n_sem_roda = n

    # ----------------------------------------------------------------- ícones
    def _ic(self, widget, name: str, size: int = 17):
        """Aplica um ícone e registra o par, para retingir quando o tema muda.

        Sem o registro, trocar para o modo noturno deixaria ícones claros sobre
        fundo preto — mais brilho na tela do que qualquer botão vermelho salvaria.
        """
        # idempotente: reaplicar num widget substitui a entrada, senão o
        # registro acumula duplicatas e o retingimento fica ambíguo
        self._iconed = [(w, n, sz) for w, n, sz in self._iconed if w is not widget]
        self._iconed.append((widget, name, size))
        widget.setIcon(icons.icon(name, self.pal.text, size))
        widget.setIconSize(QSize(size, size))
        return widget

    def _retint_icons(self) -> None:
        for widget, name, size in self._iconed:
            widget.setIcon(icons.icon(name, self.pal.text, size))
        self.rail.set_icon_provider(
            lambda n, checked: icons.icon(n, self.pal.bg if checked else self.pal.text))

    # ================================================================ modos
    def set_mode(self, key: str) -> None:
        self._mode = key
        self._req(mode=key)
        self.panels.setCurrentIndex(self._panel_index[key])
        self.context.setCurrentIndex(self._ctx_index[key])
        # cada modo tem um padrão sensato, mas a escolha explícita da barra
        # vale até você trocar de modo de novo
        self._set_view("live" if key in ("frame", "focus") else "stack")
        if key == "focus" and not self.chk_beep.isChecked():
            self.chk_beep.setToolTip("ligue para focar de ouvido")
        self._render(True)

    # ================================================================ sessão
    def _req(self, **kw) -> None:
        if self.worker:
            self.worker.request(**kw)

    def _flag(self, name: str) -> None:
        if self.worker:
            self.worker.flag(name)
            if name in ("reset", "platform_reset"):
                self.health.clear()

    def _exposure_changed(self, x: float) -> None:
        self._exposure = x
        self._req(exposure=x)

    def _cooler_toggled(self, on: bool) -> None:
        self._req(target_temp=self.sp_temp.value(), cooler=on)
        if not self.worker:
            self.lbl_cool.setText("TEC ligará ao iniciar a captura" if on
                                  else "TEC desligado")

    def _bin_changed(self, text: str) -> None:
        if int(text) > 2:
            self.on_log(f"aviso: bin{text} grampeia as altas luzes; bin2 é o "
                        f"único sem perda nesta câmera")
        self._req(bin=int(text))

    def _source_changed(self, i: int) -> None:
        replay = i == 1
        self.cb_camera.setVisible(not replay)
        self.btn_refresh.setVisible(not replay)
        self.btn_folder.setVisible(replay)

    def refresh_cameras(self) -> None:
        self.cb_camera.clear()
        try:
            cams = list_cameras()
        except Exception as e:                                       # noqa: BLE001
            self.on_log(f"erro ao listar câmeras: {e}")
            return
        if not cams:
            self.cb_camera.addItem("nenhuma câmera")
            self.on_log("nenhuma câmera — AstroDMx fechado? Parallels com o USB?")
            return
        for c in cams:
            self.cb_camera.addItem(f"{c.name}  ({c.port})")
        self.on_log(f"{len(cams)} câmera(s): " + ", ".join(c.name for c in cams))

    def pick_replay(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "sessão gravada", "sessions")
        if d:
            self._replay_folder = d
            self.on_log(f"replay: {d}")

    def zerar_stack(self) -> None:
        """Zerar descarta o acumulado, então confirma quando há o que perder."""
        integ = self._last_stats.get("integration", 0.0)
        if integ > 60:
            from PySide6.QtWidgets import QMessageBox
            r = QMessageBox.question(
                self, "Zerar stack",
                f"Descartar {_hms(integ)} de integração "
                f"({self._last_stats.get('n_stacked', 0)} frames)?\n\n"
                f"Os subs gravados em disco não são apagados.",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if r != QMessageBox.Yes:
                return
        self._flag("reset")

    def pick_dark(self) -> None:
        p, _ = QFileDialog.getOpenFileName(self, "master dark", "darks", "FITS (*.fits)")
        if p:
            self._dark_path = p
            self.lbl_dark.setText(Path(p).name)
            self._req(dark_path=p)

    def _integrar_toggled(self, on: bool) -> None:
        self.btn_integrar.setText("Parar integração" if on else "Começar a integrar")
        self._ic(self.btn_integrar, "pause" if on else "play")
        self.lbl_integrar.setText(
            "empilhando, gravando os subs e medindo a plataforma" if on
            else "parada — captura segue, nada é acumulado")
        self._flag("integrar_on" if on else "integrar_off")
        if on and not self.worker:
            self.on_log("inicie a captura primeiro")

    def gravar_dark(self) -> None:
        if not self.worker:
            self.on_log("inicie a captura antes de gravar o dark")
            return
        from PySide6.QtWidgets import QInputDialog, QMessageBox
        n, ok = QInputDialog.getInt(self, "Gravar dark",
                                    "Quantos frames?\n\n"
                                    "TAMPE O SENSOR antes de confirmar.\n"
                                    "A captura pausa durante a gravação.",
                                    20, 5, 100)
        if not ok:
            return
        self._req(dark_frames=n)
        self._flag("gravar_dark")
        self.on_log(f"gravando dark de {n} frames — não descubra o sensor")

    def pick_flat(self) -> None:
        p, _ = QFileDialog.getOpenFileName(self, "master flat", "flats", "FITS (*.fits)")
        if p:
            self._flat_path = p
            self.lbl_flat.setText(Path(p).name)
            self._req(flat_path=p)

    def start(self) -> None:
        replay = self.cb_source.currentIndex() == 1
        cfg = Config(
            mode=self._mode,
            source="replay" if replay else "camera",
            camera_index=max(self.cb_camera.currentIndex(), 0),
            replay_folder=self._replay_folder,
            bin=int(self.cb_bin.currentText()), exposure=self.sp_exp.value(),
            gain=self.sp_gain.value(), offset=self.sp_offset.value(),
            target_temp=(self.sp_temp.value() if self.btn_cooler.isChecked() else None),
            dark_path=self._dark_path, flat_path=self._flat_path,
            quality_weighting=self.chk_weight.isChecked(),
            rigor=self.cb_rigor.currentText(),
            sigma_clip=self.sp_sigma.value() if self.sp_sigma.value() > 0 else None,
            record=self.chk_record.isChecked(), target_name=self.ed_target.text(),
            record_every=self.sp_every.value(), compress=self.chk_compress.isChecked(),
            platform_minutes=self.sp_platform.value(),
        )
        self._exposure = cfg.exposure
        self.health.clear()
        self.alert.setVisible(False)
        self.worker = CaptureWorker(cfg)
        self.thread = QThread(self)
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.frame.connect(self.on_frame)
        self.worker.focus.connect(self.on_focus)
        self.worker.log.connect(self.on_log)
        self.worker.failed.connect(self.on_failed)
        self.worker.opened.connect(self.on_opened)
        self.worker.finished.connect(self.on_finished)
        self.worker.paused.connect(self.on_paused)
        self.worker.cooling.connect(self.on_cooling)
        self.worker.dark_saved.connect(self.on_dark_saved)
        self.thread.start()
        self.btn_start.setEnabled(False)
        self.btn_pause.setEnabled(True)
        self.btn_finish.setEnabled(True)
        self._paused = False
        self._set_state("exposing")
        self._t_frame = time.time()

    def toggle_pause(self) -> None:
        if not self.worker:
            return
        self.worker.set_paused(not self._paused)

    def finish(self) -> None:
        if self.worker:
            self.worker.stop()
        self.btn_pause.setEnabled(False)
        self.btn_finish.setEnabled(False)
        self._set_state("stopping")

    def save(self) -> None:
        if self._stack is None and self._live is None:
            self.on_log("nada para salvar")
            return
        import cv2
        from astropy.io import fits
        out = Path("session"); out.mkdir(exist_ok=True)
        cv2.imwrite(str(out / "stack.png"),
                    cv2.cvtColor(self._display(), cv2.COLOR_RGB2BGR))
        if self._stack is not None:
            fits.PrimaryHDU(self._stack.transpose(2, 0, 1)).writeto(
                out / "stack.fits", overwrite=True)
        self.on_log(f"salvo em {out}/")

    # ================================================================ tema
    def toggle_night(self, on: bool) -> None:
        self._theme = "night" if on else "dark"
        self.settings.setValue("theme", self._theme)
        self.apply_theme()

    def _night_changed(self, v: int) -> None:
        self._night_level = int(v)
        self.settings.setValue("night_level", self._night_level)
        if self._theme == "night":
            self.apply_theme()

    def _touch_changed(self, on: bool) -> None:
        self._touch = bool(on)
        self.settings.setValue("touch", "true" if on else "false")
        self.apply_theme()

    def apply_theme(self) -> None:
        p = palette(self._theme, self._night_level)
        self.pal = p
        QApplication.instance().setStyleSheet(stylesheet(p, self._touch))
        for plot, curve, line in ((self.hist, self.hist_curve, self.hist_black),
                                  (self.hist2, self.hist2_curve, self.hist2_black),
                                  (self.plot_focus, self.curve_focus, self.line_best)):
            plot.setBackground(p.plot_bg)
            for ax in ("left", "bottom"):
                plot.getAxis(ax).setPen(p.text_dim)
                plot.getAxis(ax).setTextPen(p.text_dim)
            curve.setPen(pg.mkPen(p.curve, width=2))
            line.setPen(pg.mkPen(p.mark, style=Qt.DashLine))
        for view in (self.view, self.loupe_view):
            view.setBackground(p.plot_bg)
        self.health.set_palette_(p)
        self._retint_icons()
        self._set_state(self._state)
        self._render(True)

    def toggle_full(self, on: bool) -> None:
        self.btn_view_stack.parentWidget().setVisible(not on)
        self._left_col.setVisible(not on)
        self.context.setVisible(not on)
        if on:
            self.log.setVisible(False)

    def fit_view(self) -> None:
        self.vb.autoRange()

    def zoom_one(self) -> None:
        if self._q is None:
            return
        c = self.vb.viewRect().center()
        vw, vh = self.vb.width() or 800, self.vb.height() or 600
        self.vb.setRange(xRange=(c.x() - vw / 2, c.x() + vw / 2),
                         yRange=(c.y() - vh / 2, c.y() + vh / 2), padding=0)

    def apply_preset(self, name: str) -> None:
        bg, clip = STRETCH_PRESETS[name]
        self.chk_auto.setChecked(True)
        self.sl_bg.setValue(int(round(bg * 100)))
        self.sl_clip.setValue(int(round(clip * 10)))

    # ================================================================ céu
    def _cat(self) -> Catalog | None:
        if self._catalog is None:
            try:
                self._catalog = Catalog().load()
                self.on_log(f"catálogo: {len(self._catalog.objs)} objetos")
            except Exception as e:                                   # noqa: BLE001
                self.on_log(f"catálogo indisponível: {e}")
        return self._catalog

    def solve_now(self) -> None:
        lum = self.worker.last_luminance() if self.worker else None
        if lum is None:
            self.on_log("sem frame para resolver — inicie a captura")
            return
        # o solver recebe a luminância, que tem metade da resolução do frame
        scale = pixel_scale(int(self.cb_bin.currentText()), halved=True)
        # palpite de posição: a solução anterior, se houver. Com a plataforma
        # rastreando ela continua válida até você encostar no tubo, e restringir
        # a busca derruba o tempo de solve.
        hint = ((self._solution.ra, self._solution.dec) if self._solution else None)
        self.on_log(f"resolvendo: {lum.shape[1]}x{lum.shape[0]} a {scale:.3f}\"/px"
                    + (f", perto de RA {hint[0]:.2f}° Dec {hint[1]:+.2f}°" if hint
                       else ", busca cega"))
        t = SolveTask(lum, scale, hint)
        t.signals.msg.connect(self.on_log)
        t.signals.done.connect(self.on_solved)
        self.pool.start(t)

    @Slot(object)
    def on_solved(self, sol) -> None:
        if sol is None:
            self.lbl_solution.setText("não resolveu")
            return
        self._solution = sol
        self.lbl_solution.setText(str(sol))
        cat = self._cat()
        if cat and sol.wcs is not None and self._q is not None:
            objs = cat.in_field(sol.wcs, self._q.shape[:2], limit=8)
            self.lbl_objects.setText("\n".join(
                f"{o.label} — {o.kind_label}" for o, _, _ in objs)
                or "nada catalogado no campo")
            self._draw_anno()
        self._update_goto()

    def set_goto(self) -> None:
        cat = self._cat()
        if not cat:
            return
        o = cat.find(self.ed_goto.text())
        if not o:
            self.lbl_goto.setText(f"'{self.ed_goto.text()}' não encontrado")
            return
        self._target = o
        self.lbl_goto.setText(f"{o.label}\n{o.kind_label} · RA {o.ra:.3f}° "
                              f"Dec {o.dec:+.3f}°")
        self._update_goto()

    def _update_goto(self) -> None:
        if self._target is None:
            return
        if self._solution is None:
            self.lbl_goto_dir.setText("resolva o campo para calcular a direção")
            return
        g = guide((self._solution.ra, self._solution.dec),
                  (self._target.ra, self._target.dec),
                  self.sp_lat.value(), self.sp_lon.value(),
                  elevation_m=SITE_ELEV, fov_deg=self._solution.fov_deg[0])
        self.lbl_goto_dir.setText(g.text)
        self.lbl_goto_dir.setStyleSheet(
            f"color: {self.pal.ok if g.on_target else self.pal.text}")

    def _draw_anno(self) -> None:
        for it in self._anno:
            self.vb.removeItem(it)
        self._anno.clear()
        if not self.chk_anno.isChecked() or self._solution is None or self._q is None:
            return
        cat = self._cat()
        if not cat or self._solution.wcs is None:
            return
        for o, x, y in cat.in_field(self._solution.wcs, self._q.shape[:2], limit=15):
            sc = pg.ScatterPlotItem([x], [y], size=20, symbol="o", brush=None,
                                    pen=pg.mkPen(self.pal.mark, width=2))
            tx = pg.TextItem(o.label, color=self.pal.mark, anchor=(0.5, 1.4))
            tx.setPos(x, y)
            for it in (sc, tx):
                self.vb.addItem(it)
                self._anno.append(it)

    # ================================================================ estado
    def _set_state(self, state: str) -> None:
        self._state = state
        p = self.pal
        spec = {"idle": ("OCIOSO", p.idle),
                "framing": ("ENQUADRANDO", p.accent),
                "focusing": ("FOCANDO", p.accent),
                "live": ("AO VIVO", p.accent),
                "exposing": ("EXPONDO", p.accent),
                "integrating": ("INTEGRANDO", p.ok),
                "replay": ("REPLAY", p.accent),
                "ready": ("PRONTO", p.accent),
                "paused": ("PAUSADO", p.warn),
                "stopping": ("PARANDO", p.warn), "error": ("ERRO", p.bad)}
        label, color = spec.get(state, (state.upper(), p.text))
        self.st_state.set(f"● {label}", color)

    def _on_tick(self) -> None:
        """Barra de exposição: sem ela o programa parece travado durante um sub
        de 10 s. Também é onde os alertas são avaliados."""
        if self.worker is None:
            self.prog.setValue(0)
            self.phase.setText("EXPOSIÇÃO")
            if self._state != "error":
                self.alert.setVisible(False)
            return
        if self._paused:
            self.prog.setValue(0)
            self.phase.setText("PAUSADO")
            return
        el = time.time() - self._t_frame
        exp = max(self._exposure, 0.001)
        if el <= exp:
            self.prog.setValue(int(min(el / exp, 1.0) * 100))
            self.phase.setText(f"EXPONDO  {el:.1f} / {exp:.1f}s")
        else:
            self.prog.setValue(100)
            over = el - exp
            self.phase.setText(f"LENDO E PROCESSANDO  +{over:.1f}s"
                               if over < 8 else f"AGUARDANDO FRAME  +{over:.0f}s")
        self._check_alerts()

    def _check_alerts(self) -> None:
        p, st = self.pal, self._last_stats
        msgs = []
        streak = self.health.streak()
        if streak >= 5:
            msgs.append((f"{streak} frames rejeitados em sequência — nuvem, orvalho "
                         f"ou o alvo saiu do quadro", p.bad))
        cool = self._cool
        if cool.get("phase") == "saturated":
            msgs.append((cool.get("message", "TEC saturado"), p.bad))
        dd = cool.get("dark_delta")
        if dd is not None and abs(dd) > 3.0:
            msgs.append((f"dark tirado {dd:+.1f} °C fora da temperatura atual — "
                         f"resíduo térmico no stack", p.warn))
        plat = st.get("platform", {})
        rem = plat.get("remaining_s", 1e9)
        if rem < 300:
            msgs.append((f"plataforma com {rem/60:.0f} min de curso", p.bad))
        elif rem < 600:
            msgs.append((f"plataforma com {rem/60:.0f} min de curso", p.warn))
        useful = plat.get("useful_s", float("inf"))
        integ = st.get("integration", 0.0)
        if np.isfinite(useful) and integ > useful:
            msgs.append((f"integração passou do orçamento de rotação "
                         f"({useful/60:.0f} min): estrelas do canto já arrastam",
                         p.warn))
        if msgs:
            text, color = msgs[0]
            self.alert.setText("⚠  " + text)
            self.alert.setStyleSheet(f"color: {color}")
            self.alert.setVisible(True)
        else:
            self.alert.setVisible(False)

    # ================================================================ slots
    @Slot(dict)
    def on_opened(self, info: dict) -> None:
        lo, hi = info.get("gain_range", (0, 570))
        self.sp_gain.setRange(lo, hi)
        tem_tec = bool(info.get("cooler"))
        self.btn_cooler.setEnabled(tem_tec)
        self.sp_temp.setEnabled(tem_tec)
        if not tem_tec:
            self.lbl_cool.setText("câmera sem refrigeração")
        self._set_state("replay" if not info.get("live", True) else "exposing")
        self.on_log(f"{info['name']}  {info.get('port','')}  "
                    f"{info['width']}x{info['height']} bin{info['bin']}  "
                    f"Bayer {info['bayer']}  escala 0..{info['full_scale']}")

    @Slot(object, object, dict)
    def on_frame(self, live, stack, st: dict) -> None:
        self._live = live
        if not st.get("n_stacked"):
            # zerar cria um stacker novo, e o worker passa a emitir stack=None.
            # Sem descartar aqui, a última imagem acumulada ficava na tela e
            # "zerar" parecia não fazer nada — o acumulador já estava limpo.
            self._stack = None
        elif stack is not None:
            self._stack = stack
        self._last_stats = st
        self._t_frame = time.time()
        if "exposure" in st:
            self._exposure = st["exposure"]
        acc = st.get("accepted")
        if st.get("stacking") and acc is not None:
            self.health.push(bool(acc))
            self._set_state("integrating" if st.get("n_stacked") else "exposing")
        elif st.get("pode_integrar"):
            # no modo Integrar mas ainda não integrando: o estado tem de deixar
            # isso óbvio, senão você acha que está acumulando e não está
            self._set_state("ready")
        else:
            self._set_state({"frame": "framing", "focus": "focusing"}
                            .get(st.get("mode"), "live"))

        p = self.pal
        self.st_integ.set(_hms(st.get("integration", 0.0)))
        n_ok, n_bad = st.get("n_stacked", 0), st.get("n_rejected", 0)
        self.st_frames.set(f"{n_ok}·{n_bad}",
                           p.bad if self.health.streak() >= 3 else None)
        fw = st.get("fwhm", float("nan"))
        plat = st.get("platform", {})
        rem = plat.get("remaining_s")
        if rem is not None:
            self.st_platform.set(f"{rem/60:.0f}m",
                                 p.bad if rem < 300 else (p.warn if rem < 600 else None))
        if acc is False:
            self.on_log(f"rejeitado: {st.get('reason','?')} "
                        f"({st.get('n_stars',0)} estrelas, FWHM {fw:.2f})")
        if st.get("recording"):
            self.lbl_disk.setText(st["recording"])
        self.lbl_advice.setText(st.get("platform_advice", "—"))
        rej = st.get("rejections") or {}
        if rej:
            nomes = {"estrelas": "poucas estrelas", "arrastado": "arrastado",
                     "fundo": "fundo alto", "fwhm": "FWHM", "registro": "registro",
                     "assentando": "assentando", "referencial": "referencial"}
            total = sum(rej.values())
            linhas = [f"{v:3d}  {nomes.get(k, k)}"
                      for k, v in sorted(rej.items(), key=lambda kv: -kv[1])]
            self.lbl_rej.setText("\n".join(linhas) + f"\n{'—'*12}\n{total:3d}  total")
        else:
            self.lbl_rej.setText("nenhum")
        self.st_rot.set(f"{plat.get('rotation_deg_min', float('nan')):+.4f}°/min"
                        if np.isfinite(plat.get("rotation_deg_min", float("nan"))) else "—")
        u = plat.get("useful_s", float("inf"))
        self.st_useful.set("sem limite" if not np.isfinite(u) else f"{u/60:.0f} min")
        sm = plat.get("corner_smear_px_min", float("nan"))
        self.st_smear.set(f"{sm:.2f} px/min" if np.isfinite(sm) else "—")
        self._update_view_label()
        self._render(True)
        self._update_goto()

    @Slot(object, dict)
    def on_focus(self, crop, d: dict) -> None:
        hfr = d.get("hfr", float("nan"))
        best = d.get("best", float("nan"))
        ratio = d.get("ratio", float("nan"))
        p = self.pal
        color = None
        if np.isfinite(ratio):
            color = p.ok if ratio < 1.05 else (p.warn if ratio < 1.3 else p.bad)
        self.lbl_hfr.setText(f"{hfr:.2f}" if np.isfinite(hfr) else "—")
        self.lbl_hfr.setStyleSheet(f"color: {color}" if color else "")
        self.st_hfr.set(f"{hfr:.2f}" if np.isfinite(hfr) else "—", color)
        self.lbl_hfr_sub.setText(f"HFR mediano · melhor {best:.2f}"
                                 if np.isfinite(best) else "HFR mediano")
        tr = d.get("trend", float("nan"))
        self.lbl_verdict.setText(d.get("verdict", "—")
                                 + (f"   {tr:+.2f} px/min" if np.isfinite(tr) else ""))
        t, h = d.get("series", (np.empty(0), np.empty(0)))
        if len(t):
            m = np.isfinite(h)
            self.curve_focus.setData(t[m], h[m])
        if np.isfinite(best):
            self.line_best.setValue(best)
        if crop is not None:
            c = crop.astype(np.float32)
            lo, hi = float(np.percentile(c, 5)), float(c.max())
            self.loupe_img.setImage(np.clip((c - lo) / max(hi - lo, 1e-6), 0, 1),
                                    autoLevels=False, levels=(0, 1))
        if self._mode == "focus":
            self.beeper.beep_ratio(ratio)

    @Slot(str)
    def on_dark_saved(self, caminho: str) -> None:
        self._dark_path = caminho
        self.lbl_dark.setText(Path(caminho).name)

    @Slot(dict)
    def on_cooling(self, st: dict) -> None:
        self._cool = st
        p = self.pal
        fase = st.get("phase", "off")
        cor = {"stable": p.ok, "cooling": p.accent, "warming": p.warn,
               "saturated": p.bad}.get(fase)
        cur, pot = st.get("current"), st.get("power", 0)
        eta = st.get("eta_s", float("nan"))
        txt = f"{cur:+.1f} °C · TEC {pot}%"
        if fase == "stable":
            txt += " · estável"
        elif fase in ("cooling", "warming") and np.isfinite(eta):
            txt += f" · {'resfriando' if fase=='cooling' else 'reaquecendo'} " \
                   f"~{eta/60:.0f} min"
        elif fase == "saturated":
            txt += " · alvo inalcançável"
        d = st.get("dark_delta")
        if d is not None and abs(d) > 2.0:
            txt += f" · dark {d:+.1f} °C fora"
        self.lbl_cool.setText(txt)
        self.lbl_cool.setStyleSheet(f"color: {cor}" if cor else "")

    @Slot(bool)
    def on_paused(self, on: bool) -> None:
        self._paused = on
        self.btn_pause.setText("Continuar" if on else "Pausar")
        self._ic(self.btn_pause, "play" if on else "pause")
        # ao retomar, o estado só seria corrigido no próximo frame — que pode
        # levar dez segundos num sub longo, e até lá o rótulo mentiria
        self._set_state("paused" if on else "exposing")

    @Slot(str)
    def on_log(self, msg: str) -> None:
        self.log.appendPlainText(msg)

    @Slot(str)
    def on_failed(self, msg: str) -> None:
        self.on_log(f"ERRO: {msg}")
        self._set_state("error")
        self.alert.setText(f"⚠  {msg}")
        self.alert.setStyleSheet(f"color: {self.pal.bad}")
        self.alert.setVisible(True)
        self.btn_log.setChecked(True)

    @Slot()
    def on_finished(self) -> None:
        if self.thread:
            self.thread.quit(); self.thread.wait(3000)
        self.worker = None; self.thread = None
        self._paused = False
        self.btn_start.setEnabled(True)
        self.btn_pause.setEnabled(False)
        self.btn_pause.setText("Pausar")
        self._ic(self.btn_pause, "pause")
        self.btn_finish.setEnabled(False)
        self.btn_finish.setText("Finalizar")
        if self.btn_integrar.isChecked():
            self.btn_integrar.setChecked(False)
        self._set_state("idle")
        self.on_log("sessão encerrada")

    # ================================================================ render
    def _set_view(self, which: str) -> None:
        self._view = which
        self.btn_view_stack.setChecked(which == "stack")
        self.btn_view_live.setChecked(which == "live")
        self._update_view_label()
        self._render(True)

    def toggle_view(self) -> None:
        self._set_view("live" if self._view == "stack" else "stack")

    def _update_view_label(self) -> None:
        st = self._last_stats
        p = self.pal
        if self._view == "live":
            acc = st.get("accepted")
            if acc is None:
                txt, col = "frame corrente, sem empilhar", None
            elif acc:
                txt, col = f"frame aceito · {st.get('n_stars',0)} estrelas", p.ok
            else:
                txt, col = f"frame REJEITADO · {st.get('reason','?')}", p.bad
            n = st.get("frame_index")
            if n:
                txt = f"#{n} · " + txt
        else:
            if self._stack is None:
                txt, col = "nenhum stack ainda", None
            else:
                txt = (f"{st.get('n_stacked',0)} frames · "
                       f"{_hms(st.get('integration',0.0))} de integração")
                col = None
        self.lbl_view.setText(txt)
        self.lbl_view.setStyleSheet(f"color: {col}" if col else "")

    def _src(self):
        if self._view == "live":
            return self._live
        return self._stack if self._stack is not None else self._live

    def _prepared(self):
        """Array a exibir, com o gradiente já removido se pedido.

        Cacheado por (fonte, ligado, grau): remover gradiente custa dezenas de ms
        e não pode rodar a cada movimento de slider.
        """
        src = self._src()
        if src is None:
            return None
        key = (id(src), self.chk_bg.isChecked(), self.sp_bg_deg.value())
        if self._prep_key == key and self._prep is not None:
            return self._prep
        out = src
        if self.chk_bg.isChecked():
            out, ok = background.remove(src, degree=self.sp_bg_deg.value(), grid=12)
            self.lbl_bg.setText("aplicado" if ok else "amostras insuficientes")
        else:
            self.lbl_bg.setText("")
        self._prep, self._prep_key = out, key
        return out

    def _quantize(self, src) -> None:
        if self._q_src is src and self._q is not None:
            return
        self._q = (np.clip(src, 0.0, 1.0) * 65535.0 + 0.5).astype(np.uint16)
        self._q_src = src

    def _stretch(self) -> np.ndarray:
        q, src = self._q, self._prepared()
        target = self.sl_bg.value() / self.sl_bg._div
        clip = -self.sl_clip.value() / self.sl_clip._div
        sat = self.sl_sat.value() / self.sl_sat._div

        if not self.chk_auto.isChecked():
            out = (q >> 8).astype(np.uint8)
        elif self.cb_algo.currentIndex() == 1:
            # arcsinh preservando cor acopla os canais, então não cabe numa LUT
            # por canal; custa mais que o MTF e é o preço da cor preservada
            img = stretch.auto_arcsinh(src, target_bg=target, shadows_clip=clip,
                                       preserve_color=True)
            self._black, _ = stretch.estimate_params(
                src.mean(axis=2) if src.ndim == 3 else src, target, clip, 8)
            out = stretch.to_uint8(stretch.saturate(img, sat))
        else:
            out = np.empty(q.shape, np.uint8)
            if self.chk_linked.isChecked():
                c0, m = stretch.estimate_params(src.mean(axis=2), target, clip, 8)
                lut = stretch.build_lut(c0, m)
                for k in range(3):
                    out[..., k] = lut[q[..., k]]
                self._black = c0
            else:
                blacks = []
                for k in range(3):
                    c0, m = stretch.estimate_params(src[..., k], target, clip, 8)
                    out[..., k] = stretch.build_lut(c0, m)[q[..., k]]
                    blacks.append(c0)
                self._black = float(np.mean(blacks))
            if abs(sat - 1.0) > 1e-3:
                out = stretch.to_uint8(
                    stretch.saturate(out.astype(np.float32) / 255.0, sat))

        lut = image_lut(self.pal)
        if lut is not None:
            out = lut[out.mean(axis=2).astype(np.uint8)]
        return out

    def _display(self) -> np.ndarray:
        src = self._prepared()
        if src is None:
            return np.zeros((1, 1, 3), np.uint8)
        self._quantize(src)
        return self._stretch()

    def _render(self, force: bool = False) -> None:
        src = self._prepared()
        if src is None:
            return
        self._quantize(src)
        self.img.setImage(self._stretch(), autoLevels=False, levels=(0, 255))
        if force:
            s = src[::8, ::8].mean(axis=2) if src.ndim == 3 else src[::8, ::8]
            counts, edges = np.histogram(s, bins=256,
                                         range=(0.0, max(float(s.max()), 1e-4)))
            for curve, line in ((self.hist_curve, self.hist_black),
                                (self.hist2_curve, self.hist2_black)):
                curve.setData(edges[:-1], counts + 1)
                line.setValue(self._black)

    # ================================================================ ajustes
    def _restore(self) -> None:
        s = self.settings
        self.btn_night.setChecked(s.value("theme", "dark") == "night")
        self.sl_night.setValue(self._night_level)
        self.chk_touch.setChecked(self._touch)
        # lat/lon ficam de fora de propósito: são chumbados no código, e um valor
        # antigo salvo sobrescreveria o sítio ao reabrir
        for name, wid, cast in (("exp", self.sp_exp, float), ("gain", self.sp_gain, int),
                                ("offset", self.sp_offset, int),
                                ("platform", self.sp_platform, float)):
            v = s.value(name)
            if v is not None:
                try:
                    wid.setValue(cast(v))
                except Exception:
                    pass
        self._exposure = self.sp_exp.value()

    def closeEvent(self, ev) -> None:
        s = self.settings
        for name, wid in (("exp", self.sp_exp), ("gain", self.sp_gain),
                          ("offset", self.sp_offset), ("platform", self.sp_platform)):
            s.setValue(name, wid.value())
        if self.worker:
            self.worker.stop()
        if self.thread:
            self.thread.quit(); self.thread.wait(2000)
        super().closeEvent(ev)


# ------------------------------------------------------------------ auxiliares
def _stat_label_font() -> QFont:
    from ui.design import T_SMALL
    f = T_SMALL()
    f.setLetterSpacing(QFont.AbsoluteSpacing, 0.6)
    f.setWeight(QFont.DemiBold)
    return f


def _tag(text: str) -> QLabel:
    """Rótulo curto ao lado de um campo, na largura do próprio texto."""
    from ui.design import T_SMALL
    lab = QLabel(text)
    lab.setObjectName("statLabel")
    lab.setFont(T_SMALL())
    return lab


def _hint(text: str) -> QLabel:
    lab = QLabel(text)
    lab.setFont(T_SMALL())
    lab.setWordWrap(True)
    return lab


def _hms(seconds: float) -> str:
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s//60}m{s%60:02d}"
    return f"{s//3600}h{(s%3600)//60:02d}"


def main() -> int:
    app = QApplication(sys.argv)
    win = MainWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
