from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

DEG = np.pi / 180.0


def rotation_matrix(alpha: float, beta: float, gamma: float) -> np.ndarray:
    x, y, z = beta * DEG, gamma * DEG, alpha * DEG
    cX, cY, cZ = np.cos(x), np.cos(y), np.cos(z)
    sX, sY, sZ = np.sin(x), np.sin(y), np.sin(z)
    return np.array(
        [
            [cZ * cY - sZ * sX * sY, -cX * sZ, cY * sZ * sX + cZ * sY],
            [cY * sZ + cZ * sX * sY, cZ * cX, sZ * sY - cZ * cY * sX],
            [-cX * sY, sX, cX * cY],
        ]
    )


def normalize(v: np.ndarray) -> np.ndarray:
    n = float(np.linalg.norm(v))
    return v / n if n > 1e-12 else np.array([0.0, 1.0, 0.0])


def altaz_to_enu(alt_deg: float, az_deg: float) -> np.ndarray:
    a, z = alt_deg * DEG, az_deg * DEG
    return np.array([np.cos(a) * np.sin(z), np.cos(a) * np.cos(z), np.sin(a)])


def enu_to_altaz(v: np.ndarray) -> tuple[float, float]:
    v = normalize(np.asarray(v, dtype=float))
    alt = float(np.degrees(np.arcsin(np.clip(v[2], -1.0, 1.0))))
    az = float(np.degrees(np.arctan2(v[0], v[1])) % 360.0)
    return alt, az


def separation(u: np.ndarray, v: np.ndarray) -> float:
    d = float(np.clip(np.dot(normalize(u), normalize(v)), -1.0, 1.0))
    return float(np.degrees(np.arccos(d)))


@dataclass
class Alignment:
    mat: np.ndarray = field(default_factory=lambda: np.eye(3))
    star: str = ""
    delta_alt_deg: float = 0.0
    delta_az_deg: float = 0.0

    @property
    def aligned(self) -> bool:
        return bool(self.star)

    @property
    def error_deg(self) -> float:
        return float(np.hypot(self.delta_alt_deg, self.delta_az_deg))

    def apply(self, v: np.ndarray) -> np.ndarray:
        return self.mat @ v

    @classmethod
    def solve(
        cls, fwd: np.ndarray, left: np.ndarray, star_enu: np.ndarray, name: str = ""
    ) -> Alignment:
        fwd = normalize(np.asarray(fwd, dtype=float))
        star = normalize(np.asarray(star_enu, dtype=float))
        left = normalize(np.asarray(left, dtype=float))

        fh, sh = normalize(fwd * [1, 1, 0]), normalize(star * [1, 1, 0])
        d_az = float(np.arctan2(np.cross(sh, fh)[2], float(np.dot(sh, fh))))
        d_alt = float(
            np.arcsin(np.clip(star[2], -1, 1)) - np.arcsin(np.clip(fwd[2], -1, 1))
        )

        c, s = np.cos(d_az), np.sin(d_az)
        m_az = np.array([[c, s, 0.0], [-s, c, 0.0], [0.0, 0.0, 1.0]])
        u0, u1, u2 = left
        W = np.array([[0.0, -u2, u1], [u2, 0.0, -u0], [-u1, u0, 0.0]])
        t = -d_alt
        m_alt = np.eye(3) + np.sin(t) * W + (1.0 - np.cos(t)) * (W @ W)

        return cls(
            m_az @ m_alt, name, float(np.degrees(d_alt)), float(np.degrees(d_az))
        )


def rays_from_fwd(
    fwd: np.ndarray, align: Alignment | None = None
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    fwd = normalize(np.asarray(fwd, dtype=float))
    h = np.hypot(fwd[0], fwd[1])
    if h < 1e-9:
        left = np.array([-1.0, 0.0, 0.0])
    else:
        left = np.array([-fwd[1] / h, fwd[0] / h, 0.0])
    top = np.cross(fwd, left)

    if align is not None:
        return align.apply(top), align.apply(left), align.apply(fwd)
    return top, left, fwd


def camera_rays(
    alpha: float, beta: float, gamma: float, align: Alignment | None = None
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    M = rotation_matrix(alpha, beta, gamma)
    return rays_from_fwd(M @ np.array([0.0, 1.0, 0.0]), align)


def bearing(target_enu: np.ndarray, rays: tuple) -> tuple[float, float, float]:
    top, left, fwd = rays
    t = normalize(np.asarray(target_enu, dtype=float))
    return float(-np.dot(left, t)), float(np.dot(top, t)), float(np.dot(fwd, t))


@dataclass
class Smoother:
    tau: float = 0.25
    _v: np.ndarray | None = None
    _t: float | None = None

    def update(self, v: np.ndarray, t: float) -> np.ndarray:
        v = normalize(np.asarray(v, dtype=float))
        if self._v is None or self._t is None or t <= self._t:
            self._v, self._t = v, t
            return v
        k = 1.0 - np.exp(-(t - self._t) / max(self.tau, 1e-3))
        self._v = normalize(self._v + k * (v - self._v))
        self._t = t
        return self._v

    def reset(self) -> None:
        self._v = self._t = None
