"""Realimentação sonora para o foco manual.

Com a mão no focalizador de um dobsoniano, você não está olhando a tela. Um bipe
por frame, com a altura do tom codificando o HFR, permite focar de ouvido: tom
subindo, foco melhorando. Funciona como um detector de metais.
"""
from __future__ import annotations

import math
import struct
import tempfile
import wave
from pathlib import Path

import numpy as np

N_STEPS = 18
F_LOW, F_HIGH = 300.0, 1500.0
DURATION = 0.07
RATE = 22050


def _write_tone(path: Path, freq: float) -> None:
    n = int(RATE * DURATION)
    t = np.arange(n) / RATE
    # envelope curto evita o "clique" no início e no fim
    env = np.minimum(1.0, np.minimum(t / 0.008, (DURATION - t) / 0.015))
    env = np.clip(env, 0.0, 1.0)
    sig = (np.sin(2 * math.pi * freq * t) * env * 0.35 * 32767).astype(np.int16)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(struct.pack("<%dh" % n, *sig))


class Beeper:
    """Banco de tons pré-gerados, tocados via QSoundEffect.

    Pré-gerar arquivos em vez de sintetizar em tempo real mantém tudo dentro do
    Qt, sem dependência de áudio extra, e o custo é ~4 KB por tom.
    """

    def __init__(self):
        self.enabled = False
        self._effects: list = []
        self._dir = Path(tempfile.mkdtemp(prefix="octans-tones-"))
        self._ready = False

    def _ensure(self) -> bool:
        if self._ready:
            return True
        try:
            from PySide6.QtCore import QUrl
            from PySide6.QtMultimedia import QSoundEffect
        except ImportError:
            return False
        for i in range(N_STEPS):
            f = F_LOW * (F_HIGH / F_LOW) ** (i / (N_STEPS - 1))
            p = self._dir / f"tone_{i:02d}.wav"
            if not p.exists():
                _write_tone(p, f)
            e = QSoundEffect()
            e.setSource(QUrl.fromLocalFile(str(p)))
            e.setVolume(0.5)
            self._effects.append(e)
        self._ready = True
        return True

    def beep_ratio(self, ratio: float) -> None:
        """`ratio` = HFR atual / melhor HFR da sessão. 1.0 toca o tom mais agudo.

        A escala vai até 2.5x porque acima disso você está longe do foco e a
        informação útil é só "ainda ruim".
        """
        if not self.enabled or not np.isfinite(ratio):
            return
        if not self._ensure():
            return
        r = float(np.clip(ratio, 1.0, 2.5))
        idx = int(round((1.0 - (r - 1.0) / 1.5) * (N_STEPS - 1)))
        idx = max(0, min(N_STEPS - 1, idx))
        eff = self._effects[idx]
        eff.play()

    def beep_index(self, i: int) -> None:
        if not self.enabled or not self._ensure():
            return
        self._effects[max(0, min(N_STEPS - 1, i))].play()
