"""Thread-safe OpticStudio ZOS-API connection manager."""

from __future__ import annotations

import logging
import os
import threading
from typing import Any, Callable, TypeVar

from zemax_mcp.core.zos_compat import get_zospy

logger = logging.getLogger(__name__)
T = TypeVar("T")

DEFAULT_OPTICSTUDIO_DIR = r"E:\Zemax\ANSYS Inc\v242\Zemax OpticStudio"


class ZemaxConnectionManager:
    """Singleton connection manager with RLock serialization for COM/STA safety."""

    def __init__(self, opticstudio_directory: str | None = None):
        self.zos: Any = None
        self.oss: Any = None
        self._lock = threading.RLock()
        self._opticstudio_directory = opticstudio_directory

    @property
    def opticstudio_directory(self) -> str:
        return self._opticstudio_directory or os.environ.get(
            "OPTICSTUDIO_DIRECTORY", DEFAULT_OPTICSTUDIO_DIR
        )

    def connect(self) -> Any:
        with self._lock:
            if self.oss is None:
                logger.info("Connecting to Ansys Zemax OpticStudio Interactive Extension...")
                zp = get_zospy()
                self.zos = zp.ZOS(opticstudio_directory=self.opticstudio_directory)
                self.oss = self.zos.connect(mode="extension")
                logger.info("Connected to Zemax OpticStudio Interactive Extension.")
            return self.oss

    def disconnect(self) -> None:
        with self._lock:
            self.oss = None
            self.zos = None

    def is_connected(self) -> bool:
        with self._lock:
            return self.oss is not None

    def check_health(self) -> bool:
        """Lightweight liveness check against the active OpticStudio session."""
        with self._lock:
            if self.oss is None:
                return False
            try:
                _ = int(self.oss.LDE.NumberOfSurfaces)
                return True
            except Exception as exc:
                logger.warning("OpticStudio health check failed: %s", exc)
                return False

    def run_locked(self, fn: Callable[[Any], T]) -> T:
        """Execute a callable with the active OSS instance under the STA lock (with 1x auto-reconnect)."""
        with self._lock:
            if self.oss is not None and not self.check_health():
                logger.info("Stale OpticStudio connection detected; reconnecting...")
                self.disconnect()
            oss = self.connect()
            return fn(oss)
