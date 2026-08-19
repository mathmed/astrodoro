"""Sistema de design: tokens, tipografia e componentes reutilizáveis.

Duas decisões que atravessam tudo:

1. **Hierarquia por tamanho, não por caixa.** A informação vital da sessão
   (integração, saúde dos frames, HFR, curso da plataforma) fica grande e sempre
   visível; o resto encolhe. Antes tudo tinha o mesmo peso visual, o que na
   prática significa que nada se destaca.

2. **No modo noturno a semântica vem do brilho, não do matiz.** Se toda a tela é
   vermelha, verde-amarelo-vermelho deixa de existir como código. Então "ok",
   "atenção" e "problema" viram vermelho apagado, médio e intenso.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import (
    QFrame, QGridLayout, QHBoxLayout, QLabel, QPushButton, QSizePolicy,
    QVBoxLayout, QWidget,
)

MONO_FAMILY = "Menlo"
SANS_FAMILY = ""          # deixa o Qt escolher a fonte de sistema


# ------------------------------------------------------------------ tipografia
def font(size: int, weight: int = QFont.Normal, mono: bool = False) -> QFont:
    f = QFont(MONO_FAMILY if mono else SANS_FAMILY, size)
    f.setWeight(weight)
    return f


def T_DISPLAY() -> QFont:  return font(38, QFont.Bold, mono=True)
def T_XL() -> QFont:       return font(22, QFont.DemiBold, mono=True)
def T_L() -> QFont:        return font(16, QFont.DemiBold, mono=True)
def T_H1() -> QFont:       return font(15, QFont.DemiBold)
def T_H2() -> QFont:       return font(12, QFont.DemiBold)
def T_BODY() -> QFont:     return font(12)
def T_MONO() -> QFont:     return font(11, QFont.Normal, mono=True)
def T_SMALL() -> QFont:    return font(10)


# ------------------------------------------------------------------ paletas
@dataclass(frozen=True)
class Palette:
    bg: str
    surface: str
    surface2: str
    border: str
    text: str
    text_dim: str
    ok: str
    warn: str
    bad: str
    idle: str
    accent: str
    plot_bg: str
    curve: str
    mark: str
    # ceiling da rampa vermelha da imagem em modo noturno (0 = não aplica)
    image_red: int = 0


DARK = Palette(
    bg="#16181c", surface="#1e2126", surface2="#262a30", border="#343941",
    text="#dcdfe4", text_dim="#8b93a0",
    ok="#5fbf7f", warn="#e0a44c", bad="#e0665f", idle="#5a6068", accent="#77a8d8",
    plot_bg="#16181c", curve="#77c4f0", mark="#e0665f",
)

# Três intensidades de noturno. A mais escura é a que preserva adaptação; as
# outras existem porque nem todo lugar é escuro de verdade.
NIGHT = (
    Palette(bg="#000000", surface="#0a0201", surface2="#140403", border="#37100a",
            text="#8e2e20", text_dim="#5a1c13",
            ok="#4d1a11", warn="#8e2e20", bad="#cf4632", idle="#2a0a06",
            accent="#7a271b", plot_bg="#000000", curve="#8e2e20", mark="#cf4632",
            image_red=170),
    Palette(bg="#000000", surface="#0d0302", surface2="#1a0605", border="#4a1610",
            text="#b03a2a", text_dim="#74241a",
            ok="#632217", warn="#b03a2a", bad="#e8543c", idle="#340d08",
            accent="#96301f", plot_bg="#000000", curve="#b03a2a", mark="#e8543c",
            image_red=215),
    Palette(bg="#000000", surface="#120403", surface2="#210807", border="#5e1c14",
            text="#d24631", text_dim="#8d2d20", ok="#7a2a1c", warn="#d24631",
            bad="#ff6a4d", idle="#420f09", accent="#b3391f",
            plot_bg="#000000", curve="#d24631", mark="#ff6a4d", image_red=255),
)


def palette(theme: str, night_level: int = 1) -> Palette:
    return DARK if theme != "night" else NIGHT[max(0, min(2, night_level))]


def stylesheet(p: Palette, touch: bool = False) -> str:
    """`touch` aumenta as áreas de clique — no escuro, com frio e sem óculos,
    alvo pequeno custa caro.

    Cuidado central: **não** dar `background` ao seletor genérico `QWidget`.
    Todo QWidget usado só para agrupar layout herdaria o fundo da janela e o
    pintaria por cima do card, desenhando um retângulo escuro em volta de cada
    grupo. Os widgets neutros ficam transparentes e o fundo é declarado apenas
    na raiz, nos cards e nos campos de entrada.
    """
    pad = "9px 14px" if touch else "5px 11px"
    ctrl = "6px" if touch else "4px"
    bar = "12px" if touch else "9px"
    return f"""
QMainWindow, QWidget#root {{ background: {p.bg}; }}
QWidget {{ background: transparent; color: {p.text}; }}

QFrame#card {{ background: {p.surface}; border: 1px solid {p.border};
               border-radius: 7px; }}
QLabel#sectionTitle {{ color: {p.text_dim}; }}
QLabel#statLabel {{ color: {p.text_dim}; }}

QPushButton {{ background: {p.surface2}; border: 1px solid {p.border};
               border-radius: 5px; padding: {pad}; color: {p.text}; }}
QPushButton:hover {{ background: {p.border}; }}
QPushButton:disabled {{ color: {p.idle}; border-color: {p.idle};
                        background: {p.surface}; }}
QPushButton:checked {{ background: {p.accent}; border-color: {p.accent};
                       color: {p.bg}; }}
QPushButton#mode {{ text-align: left; padding: 11px 14px; font-weight: 600; }}
QPushButton#ghost {{ background: transparent; border: none; color: {p.text_dim};
                     padding: 2px 6px; }}
QPushButton#ghost:hover {{ color: {p.text}; }}

QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox, QPlainTextEdit {{
    background: {p.bg}; border: 1px solid {p.border}; border-radius: 4px;
    padding: {ctrl}; color: {p.text}; selection-background-color: {p.accent};
    selection-color: {p.bg}; }}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {{
    border-color: {p.accent}; }}
QComboBox::drop-down {{ border: none; width: 18px; }}
QComboBox QAbstractItemView {{ background: {p.surface2}; color: {p.text};
    border: 1px solid {p.border}; selection-background-color: {p.accent};
    selection-color: {p.bg}; outline: none; }}
QAbstractSpinBox::up-button, QAbstractSpinBox::down-button {{ width: 14px;
    background: transparent; border: none; }}

QSlider::groove:horizontal {{ height: 4px; background: {p.border};
                              border-radius: 2px; }}
QSlider::handle:horizontal {{ background: {p.text}; width: 14px; margin: -6px 0;
                              border-radius: 7px; }}
QSlider::sub-page:horizontal {{ background: {p.accent}; border-radius: 2px; }}

QCheckBox, QRadioButton {{ color: {p.text}; spacing: 8px; }}
QCheckBox::indicator, QRadioButton::indicator {{ width: 15px; height: 15px;
    border: 1px solid {p.border}; border-radius: 3px; background: {p.bg}; }}
QRadioButton::indicator {{ border-radius: 8px; }}
QCheckBox::indicator:checked, QRadioButton::indicator:checked {{
    background: {p.accent}; border-color: {p.accent}; }}

QScrollArea {{ border: none; background: transparent; }}
QScrollBar:vertical {{ background: transparent; width: {bar}; margin: 0; }}
QScrollBar::handle:vertical {{ background: {p.border}; min-height: 34px;
    border-radius: 4px; margin: 1px; }}
QScrollBar::handle:vertical:hover {{ background: {p.text_dim}; }}
QScrollBar:horizontal {{ background: transparent; height: {bar}; margin: 0; }}
QScrollBar::handle:horizontal {{ background: {p.border}; min-width: 34px;
    border-radius: 4px; margin: 1px; }}
QScrollBar::handle:horizontal:hover {{ background: {p.text_dim}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; border: none; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

QSplitter::handle {{ background: transparent; }}
QSplitter::handle:horizontal {{ width: 8px; }}
QSplitter::handle:vertical {{ height: 8px; }}

QStackedWidget {{ background: transparent; }}
QToolTip {{ background: {p.surface2}; color: {p.text};
            border: 1px solid {p.border}; padding: 5px; }}
QProgressBar {{ background: {p.surface2}; border: none; border-radius: 4px;
                height: 8px; text-align: center; }}
QProgressBar::chunk {{ background: {p.accent}; border-radius: 4px; }}
"""


def image_lut(p: Palette) -> np.ndarray | None:
    """Rampa vermelha aplicada à imagem toda em modo noturno.

    Pintar só os controles e deixar uma nebulosa branca de 2000x1400 na tela não
    preserva adaptação nenhuma — a imagem é a maior fonte de luz do programa.
    """
    if not p.image_red:
        return None
    x = np.arange(256, dtype=np.float32) / 255.0
    lut = np.zeros((256, 3), dtype=np.uint8)
    lut[:, 0] = np.clip(x * p.image_red, 0, 255)
    lut[:, 1] = np.clip(x ** 2.6 * p.image_red * 0.16, 0, 255)
    lut[:, 2] = np.clip(x ** 3.4 * p.image_red * 0.07, 0, 255)
    return lut


# ------------------------------------------------------------------ componentes
class Card(QFrame):
    """Agrupador com título discreto. Substitui o QGroupBox, cuja moldura com
    título embutido rouba altura e atenção."""

    def __init__(self, title: str = "", parent=None):
        super().__init__(parent)
        self.setObjectName("card")
        self._v = QVBoxLayout(self)
        self._v.setContentsMargins(11, 9, 11, 11)
        self._v.setSpacing(7)
        if title:
            lab = QLabel(title.upper())
            lab.setObjectName("sectionTitle")
            f = T_SMALL(); f.setLetterSpacing(QFont.AbsoluteSpacing, 1.0)
            f.setWeight(QFont.DemiBold)
            lab.setFont(f)
            self._v.addWidget(lab)

    def add(self, w: QWidget, stretch: int = 0) -> QWidget:
        self._v.addWidget(w, stretch)
        return w

    def add_layout(self, lay):
        self._v.addLayout(lay)
        return lay

    def row(self, *widgets, spacing: int = 6):
        h = QHBoxLayout(); h.setContentsMargins(0, 0, 0, 0); h.setSpacing(spacing)
        for w in widgets:
            h.addWidget(w)
        self._v.addLayout(h)
        return h

    def field(self, label: str, w: QWidget, hint: str = "") -> QWidget:
        h = QHBoxLayout(); h.setContentsMargins(0, 0, 0, 0)
        lab = QLabel(label); lab.setFont(T_BODY())
        lab.setMinimumWidth(84)
        if hint:
            lab.setToolTip(hint); w.setToolTip(hint)
        h.addWidget(lab); h.addWidget(w, 1)
        self._v.addLayout(h)
        return w


class Stat(QWidget):
    """Rótulo pequeno em cima, valor grande embaixo — a unidade da barra vital.

    Largura mínima e margens são o que impede os indicadores de colarem uns nos
    outros e o rótulo de tocar a borda do card.
    """

    def __init__(self, label: str, value: str = "—", big: bool = False,
                 mono: bool = True, min_width: int = 104, parent=None):
        super().__init__(parent)
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 1, 0, 1)
        v.setSpacing(3)
        self.lab = QLabel(label.upper())
        self.lab.setObjectName("statLabel")
        f = T_SMALL()
        f.setLetterSpacing(QFont.AbsoluteSpacing, 0.6)
        f.setWeight(QFont.DemiBold)
        self.lab.setFont(f)
        self.val = QLabel(value)
        self.val.setFont(T_XL() if big else T_L())
        v.addWidget(self.lab)
        v.addWidget(self.val)
        v.addStretch(1)
        self.setMinimumWidth(min_width)
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)

    def set(self, text: str, color: str | None = None) -> None:
        self.val.setText(text)
        self.val.setStyleSheet(f"color: {color}" if color else "")


class HealthStrip(QWidget):
    """Últimos N frames como marcas: aceito, rejeitado, motivo por tonalidade.

    Um número de rejeições não diz nada; a *sequência* diz tudo. Três rejeições
    isoladas em cinquenta frames é seeing. Doze seguidas é nuvem, orvalho ou o
    tubo fora do alvo — e você quer ver isso sem ler log.
    """

    def __init__(self, capacity: int = 90, parent=None):
        super().__init__(parent)
        self.capacity = capacity
        self.marks: list[int] = []      # 1 ok, 0 rejeitado
        self.setMinimumHeight(14)
        self.setMaximumHeight(14)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setVisible(False)
        self._p = DARK

    def set_palette_(self, p: Palette) -> None:
        self._p = p
        self.update()

    def push(self, ok: bool) -> None:
        self.marks.append(1 if ok else 0)
        if len(self.marks) > self.capacity:
            del self.marks[:len(self.marks) - self.capacity]
        self.setVisible(True)
        self.update()

    def clear(self) -> None:
        self.marks.clear()
        self.setVisible(False)
        self.update()

    def streak(self) -> int:
        """Rejeições consecutivas no fim da série."""
        n = 0
        for m in reversed(self.marks):
            if m:
                break
            n += 1
        return n

    def paintEvent(self, ev) -> None:
        if not self.marks:
            return
        pnt = QPainter(self)
        w, h = self.width(), self.height()
        n = len(self.marks)
        bw = max(2.0, w / max(n, self.capacity))
        for i, m in enumerate(self.marks):
            x = i * bw
            col = QColor(self._p.ok if m else self._p.bad)
            pnt.fillRect(int(x), 2 if m else 0, max(1, int(bw - 1)),
                         h - 4 if m else h, col)


class ModeRail(QWidget):
    """Seletor de modo: ícone + rótulo, exclusivo, em grade 2x2.

    Em grade e não em coluna porque quatro botões empilhados custavam 168 px de
    uma coluna de 735, e essa altura vale mais no painel do modo ativo.

    O ícone do botão selecionado é retingido: com o fundo em cor de destaque, um
    ícone na cor do texto normal desapareceria.
    """

    changed = Signal(str)

    def __init__(self, modes: list[tuple], columns: int = 2, parent=None):
        super().__init__(parent)
        g = QGridLayout(self)
        g.setContentsMargins(0, 0, 0, 0)
        g.setSpacing(5)
        self.buttons: dict[str, QPushButton] = {}
        self.icon_names: dict[str, str] = {}
        self._provider = None
        for i, mode in enumerate(modes):
            key, label, hint = mode[0], mode[1], mode[2]
            b = QPushButton(label)
            b.setObjectName("mode")
            b.setCheckable(True)
            b.setToolTip(hint)
            b.clicked.connect(lambda _=False, k=key: self.select(k))
            g.addWidget(b, i // columns, i % columns)
            self.buttons[key] = b
            if len(mode) > 3 and mode[3]:
                self.icon_names[key] = mode[3]
        for c in range(columns):
            g.setColumnStretch(c, 1)
        self.current = modes[0][0]
        self.buttons[self.current].setChecked(True)

    def set_icon_provider(self, fn) -> None:
        """fn(nome, selecionado) -> QIcon"""
        self._provider = fn
        self.refresh_icons()

    def refresh_icons(self) -> None:
        if self._provider is None:
            return
        for key, b in self.buttons.items():
            name = self.icon_names.get(key)
            if not name:
                continue
            b.setIcon(self._provider(name, b.isChecked()))
            b.setIconSize(QSize(17, 17))

    def select(self, key: str) -> None:
        for k, b in self.buttons.items():
            b.setChecked(k == key)
        self.current = key
        self.refresh_icons()
        self.changed.emit(key)
