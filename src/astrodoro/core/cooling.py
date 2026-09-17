from __future__ import annotations

from dataclasses import dataclass

from ..i18n import gettext as _


@dataclass
class CoolingState:
    phase: str = "off"
    current: float = float("nan")
    target: float | None = None
    setpoint: float | None = None
    power: int = 0
    stable: bool = False
    eta_s: float = float("nan")
    ambient: float | None = None
    message: str = ""


class CoolerController:
    def __init__(
        self,
        ramp_c_per_min: float = 2.0,
        band: float = 0.3,
        stable_seconds: float = 30.0,
        sat_power: int = 95,
        sat_seconds: float = 90.0,
        warm_margin: float = 4.0,
    ):
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

    def set_target(self, celsius: float | None, current: float | None = None) -> None:
        if celsius is None:
            if self.target is not None or self._setpoint is not None:
                self._warming = True
            self.target = None
        else:
            if self.ambient is None and current is not None:
                self.ambient = current
            self.target = float(celsius)
            self._warming = False
            if self._setpoint is None and current is not None:
                self._setpoint = current
        self._t_in_band = None
        self._t_saturated = None

    def update(
        self, current: float, power: int, now: float
    ) -> tuple[float | None, bool]:
        dt = 0.0 if self._t_last is None else max(now - self._t_last, 0.0)
        self._t_last = now
        step = self.ramp * dt / 60.0

        if self._warming:
            return self._step_warm(current, power, step)
        if self.target is None:
            self.state = CoolingState(
                phase="off",
                current=current,
                power=power,
                ambient=self.ambient,
                message=_("cooler off"),
            )
            return None, False
        return self._step_cool(current, power, step, now)

    def _step_cool(self, current, power, step, now):
        if self._setpoint is None:
            self._setpoint = current
        if self._setpoint > self.target:
            self._setpoint = max(self.target, self._setpoint - step)
        else:
            self._setpoint = min(self.target, self._setpoint + step)

        at_target = abs(self._setpoint - self.target) < 1e-3
        in_band = abs(current - self.target) <= self.band

        if at_target and in_band:
            self._t_in_band = self._t_in_band or now
            stable = (now - self._t_in_band) >= self.stable_seconds
        else:
            self._t_in_band = None
            stable = False

        if power >= self.sat_power and current > self._setpoint + 1.0:
            self._t_saturated = self._t_saturated or now
        else:
            self._t_saturated = None
        saturated = (
            self._t_saturated is not None
            and (now - self._t_saturated) >= self.sat_seconds
        )

        if saturated:
            phase = "saturated"
            message = _(
                "TEC at {power}% for {seconds}s without reaching the "
                "target — unreachable tonight; try {suggestion:+.0f} C"
            ).format(
                power=power,
                seconds=int(now - self._t_saturated),
                suggestion=round(current + 1.0),
            )
        elif stable:
            phase = "stable"
            message = _("stable at {current:+.1f} C, TEC at {power}%").format(
                current=current, power=power
            )
        else:
            phase = "cooling"
            message = _(
                "cooling: {current:+.1f} -> {target:+.1f} C "
                "(setpoint {setpoint:+.1f}), TEC {power}%"
            ).format(
                current=current,
                target=self.target,
                setpoint=self._setpoint,
                power=power,
            )

        eta = float("nan")
        if not stable:
            eta = abs(current - self.target) / max(self.ramp, 1e-6) * 60.0
            eta += self.stable_seconds
        self.state = CoolingState(
            phase,
            current,
            self.target,
            self._setpoint,
            power,
            stable,
            eta,
            self.ambient,
            message,
        )
        return self._setpoint, True

    def _step_warm(self, current, power, step):
        goal = (
            (self.ambient - self.warm_margin)
            if self.ambient is not None
            else current + 15.0
        )
        if self._setpoint is None:
            self._setpoint = current
        self._setpoint = min(goal, self._setpoint + step)
        if current >= goal - self.band:
            self._warming = False
            self._setpoint = None
            self.state = CoolingState(
                "off",
                current,
                None,
                None,
                power,
                ambient=self.ambient,
                message=_("warmed up to {current:+.1f} C, cooler off").format(
                    current=current
                ),
            )
            return None, False
        eta = max(goal - current, 0.0) / max(self.ramp, 1e-6) * 60.0
        self.state = CoolingState(
            "warming",
            current,
            None,
            self._setpoint,
            power,
            eta_s=eta,
            ambient=self.ambient,
            message=_(
                "warming up: {current:+.1f} -> {goal:+.1f} C before switching off"
            ).format(current=current, goal=goal),
        )
        return self._setpoint, True
