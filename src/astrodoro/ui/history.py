"""Recent frame history, so clicking a mark on the health strip shows the sub
behind it.

**Stores the calibrated CFA mosaic as uint16, not the screen's float32 RGB.** At
bin2 (2072x1411) the float32 RGB costs 35 MB per frame and the mosaic costs
5.8 MB: the same memory budget holds 6x more frames, and demosaicing back costs
about 15 ms — once, on the click, not per frame. The mosaic is exactly the array
`CaptureWorker._process` already builds in order to demosaic, so archiving it is
storing a reference.

**The budget is in bytes, not in frame count.** A bin1 frame has 4x the area of a
bin2 one; a budget in frames would become 4x the memory with nobody asking.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from ..core import debayer


@dataclass
class FrameEntry:
    index: int
    cfa: np.ndarray                 # calibrated mosaic, uint16, scale 0..65535
    bayer: object
    info: dict = field(default_factory=dict)
    accepted: bool = True

    def rgb(self) -> np.ndarray:
        """Rebuild the float32 [0,1] RGB that was on screen for that frame.

        Same `quality="linear"` as the live pipeline, on purpose: the reviewed
        frame has to be the frame that went through, not a better version of it.
        """
        return (debayer.to_rgb(self.cfa, self.bayer, quality="linear")
                .astype(np.float32) / 65535.0)


class FrameHistory:
    """The most recent frames by capture index, within a memory budget."""

    def __init__(self, budget_mb: int = 320):
        self.budget = int(budget_mb) * 1024 * 1024
        self._items: dict[int, FrameEntry] = {}
        self.nbytes = 0

    def push(self, index, cfa, bayer, info: dict | None = None,
             accepted: bool = True) -> None:
        if index is None or cfa is None or bayer is None:
            return
        key = int(index)
        # A replay in loop mode passes the same indices again: the new entry
        # replaces the old rather than duplicating the accounting.
        old = self._items.pop(key, None)
        if old is not None:
            self.nbytes -= old.cfa.nbytes
        self._items[key] = FrameEntry(key, cfa, bayer, dict(info or {}),
                                      bool(accepted))
        self.nbytes += cfa.nbytes
        while self.nbytes > self.budget and len(self._items) > 1:
            # dict preserves insertion order: the first is the oldest.
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
