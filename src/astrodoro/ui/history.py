from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from ..core import debayer
from ..drivers import Bayer


@dataclass
class FrameEntry:
    index: int
    cfa: np.ndarray
    bayer: Bayer
    info: dict = field(default_factory=dict)
    accepted: bool = True

    def rgb(self) -> np.ndarray:
        return (
            debayer.to_rgb(self.cfa, self.bayer, quality="linear").astype(np.float32)
            / 65535.0
        )


class FrameHistory:
    def __init__(self, budget_mb: int = 320):
        self.budget = int(budget_mb) * 1024 * 1024
        self._items: dict[int, FrameEntry] = {}
        self.nbytes = 0

    def push(
        self, index, cfa, bayer, info: dict | None = None, accepted: bool = True
    ) -> None:
        if index is None or cfa is None or bayer is None:
            return
        key = int(index)
        old = self._items.pop(key, None)
        if old is not None:
            self.nbytes -= old.cfa.nbytes
        self._items[key] = FrameEntry(key, cfa, bayer, dict(info or {}), bool(accepted))
        self.nbytes += cfa.nbytes
        while self.nbytes > self.budget and len(self._items) > 1:
            self.nbytes -= self._items.pop(next(iter(self._items))).cfa.nbytes

    def get(self, index) -> FrameEntry | None:
        if index is None:
            return None
        return self._items.get(int(index))

    def clear(self) -> None:
        self._items.clear()
        self.nbytes = 0

    def __len__(self) -> int:
        return len(self._items)

    def __contains__(self, index) -> bool:
        return index is not None and int(index) in self._items

    @property
    def mb(self) -> float:
        return self.nbytes / (1024 * 1024)
