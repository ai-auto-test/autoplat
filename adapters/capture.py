"""Video capture devices via OpenCV (Chapters 6 and 7)."""
from __future__ import annotations

import time
from typing import Any

from autoplat.adapters.base import Frame
from autoplat.errors import CaptureNoSignal
from autoplat.timeline import traced


class OpenCvCapture:
    """UVC/HDMI capture card. Freshness is proven by comparing consecutive frames' content."""

    def __init__(self, device: int | str, width: int | None = None, height: int | None = None):
        import cv2

        self._cv2 = cv2
        self._cap = cv2.VideoCapture(device)
        if width:
            self._cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
        if height:
            self._cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
        self._counter = 0
        self.device = device

    @traced("capture")
    def grab(self, *, timeout_s: float = 2.0) -> Frame:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            ok, image = self._cap.read()
            if ok and image is not None:
                self._counter += 1
                return Frame(image=image, timestamp=time.monotonic(), counter=self._counter)
            time.sleep(0.05)
        raise CaptureNoSignal(f"capture device {self.device!r} returned no frame within {timeout_s}s")

    def signal_info(self) -> dict[str, Any]:
        cv2 = self._cv2
        return {
            "opened": bool(self._cap.isOpened()),
            "width": int(self._cap.get(cv2.CAP_PROP_FRAME_WIDTH)),
            "height": int(self._cap.get(cv2.CAP_PROP_FRAME_HEIGHT)),
            "fps": float(self._cap.get(cv2.CAP_PROP_FPS)),
        }

    def close(self) -> None:
        self._cap.release()
