"""Gestão da refrigeração termoelétrica.

A SDK só aceita "temperatura alvo" e liga o TEC no máximo até chegar lá. Isso é
insuficiente por três motivos:

* **Choque térmico.** Mandar de 25 °C direto para −10 °C faz o TEC puxar 100% e
  a temperatura despencar. Resfriamento rápido demais estressa a junta do sensor
  e favorece condensação na janela. Prática usual é rampa de 1 a 3 °C/min.
* **Saturação silenciosa.** Se o alvo é inalcançável para o ambiente da noite, o
  TEC fica em 100% para sempre, a temperatura nunca estabiliza e seus darks nunca
  batem com os lights. Sem detecção, você só descobre olhando o número.
* **Reaquecimento.** Desligar o TEC de −10 °C com o sensor frio deixa a janela
  condensar. Subir em rampa antes de encerrar evita isso.

Esta classe é pura lógica: recebe leituras, devolve o setpoint a escrever.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class CoolingState:
    phase: str = "off"          # off, cooling, stable, saturated, warming, indisponível
    current: float = float("nan")
    target: float | None = None
    setpoint: float | None = None
    power: int = 0
    stable: bool = False
    eta_s: float = float("nan")
    ambient: float | None = None
    message: str = ""

    @property
    def busy(self) -> bool:
        return self.phase in ("cooling", "warming")


class CoolerController:
    def __init__(self, ramp_c_per_min: float = 2.0, band: float = 0.3,
                 stable_seconds: float = 30.0, sat_power: int = 95,
                 sat_seconds: float = 90.0, warm_margin: float = 4.0):
        self.ramp = ramp_c_per_min
        self.band = band
        self.stable_seconds = stable_seconds
        self.sat_power = sat_power
        self.sat_seconds = sat_seconds
        self.warm_margin = warm_margin

        self.target: float | None = None
        self.ambient: float | None = None
        self._setpoint: float | None = None
        self._t_last: float | None = None
        self._t_in_band: float | None = None
        self._t_saturated: float | None = None
        self._warming = False
        self.state = CoolingState()

    # ------------------------------------------------------------------ pedido
    def set_target(self, celsius: float | None, current: float | None = None) -> None:
        """`None` inicia o reaquecimento e depois desliga."""
        if celsius is None:
            if self.target is not None or self._setpoint is not None:
                self._warming = True
            self.target = None
        else:
            if self.ambient is None and current is not None:
                # primeira leitura com o TEC desligado serve de referência de
                # ambiente, e é o que define até onde reaquecer no fim
                self.ambient = current
            self.target = float(celsius)
            self._warming = False
            if self._setpoint is None and current is not None:
                self._setpoint = current
        self._t_in_band = None
        self._t_saturated = None

    def cancel_warmup(self) -> None:
        self._warming = False
        self._setpoint = None

    # ------------------------------------------------------------------ passo
    def update(self, current: float, power: int, now: float) -> tuple[float | None, bool]:
        """Devolve (setpoint a escrever, ligar TEC). `None` = não escrever."""
        dt = 0.0 if self._t_last is None else max(now - self._t_last, 0.0)
        self._t_last = now
        passo = self.ramp * dt / 60.0

        if self._warming:
            return self._step_warm(current, power, passo)
        if self.target is None:
            self.state = CoolingState(phase="off", current=current, power=power,
                                      ambient=self.ambient, message="TEC desligado")
            return None, False
        return self._step_cool(current, power, passo, now)

    def _step_cool(self, current, power, passo, now):
        if self._setpoint is None:
            self._setpoint = current
        # rampa do setpoint em direção ao alvo
        if self._setpoint > self.target:
            self._setpoint = max(self.target, self._setpoint - passo)
        else:
            self._setpoint = min(self.target, self._setpoint + passo)

        no_alvo = abs(self._setpoint - self.target) < 1e-3
        dentro = abs(current - self.target) <= self.band

        if no_alvo and dentro:
            self._t_in_band = self._t_in_band or now
            estavel = (now - self._t_in_band) >= self.stable_seconds
        else:
            self._t_in_band = None
            estavel = False

        # saturação: potência no talo e ainda longe do setpoint
        if power >= self.sat_power and current > self._setpoint + 1.0:
            self._t_saturated = self._t_saturated or now
        else:
            self._t_saturated = None
        saturado = (self._t_saturated is not None
                    and (now - self._t_saturated) >= self.sat_seconds)

        if saturado:
            alcancavel = round(current + 1.0)
            fase, msg = "saturated", (
                f"TEC em {power}% há {int(now - self._t_saturated)}s sem chegar ao "
                f"alvo — inalcançável nesta noite; tente {alcancavel:+.0f} °C")
        elif estavel:
            fase, msg = "stable", f"estável em {current:+.1f} °C, TEC em {power}%"
        else:
            falta = abs(current - self.target)
            fase = "cooling"
            msg = (f"resfriando: {current:+.1f} → {self.target:+.1f} °C "
                   f"(setpoint {self._setpoint:+.1f}), TEC {power}%")

        eta = float("nan")
        if not estavel:
            eta = abs(current - self.target) / max(self.ramp, 1e-6) * 60.0
            eta += self.stable_seconds
        self.state = CoolingState(fase, current, self.target, self._setpoint, power,
                                  estavel, eta, self.ambient, msg)
        return self._setpoint, True

    def _step_warm(self, current, power, passo):
        destino = (self.ambient - self.warm_margin) if self.ambient is not None \
            else current + 15.0
        if self._setpoint is None:
            self._setpoint = current
        self._setpoint = min(destino, self._setpoint + passo)
        pronto = current >= destino - self.band
        if pronto:
            self._warming = False
            self._setpoint = None
            self.state = CoolingState("off", current, None, None, power,
                                      ambient=self.ambient,
                                      message=f"reaquecido a {current:+.1f} °C, TEC desligado")
            return None, False
        eta = max(destino - current, 0.0) / max(self.ramp, 1e-6) * 60.0
        self.state = CoolingState("warming", current, None, self._setpoint, power,
                                  eta_s=eta, ambient=self.ambient,
                                  message=f"reaquecendo: {current:+.1f} → "
                                          f"{destino:+.1f} °C antes de desligar")
        return self._setpoint, True
