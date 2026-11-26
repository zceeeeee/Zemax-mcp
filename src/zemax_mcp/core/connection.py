"""Thread-safe OpticStudio ZOS-API connection manager."""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import Any, Callable, Iterable, TypeVar

from zemax_mcp.core.zos_compat import get_zospy

logger = logging.getLogger(__name__)
T = TypeVar("T")

_REQUIRED_ZOS_DLLS = ("ZOSAPI.dll", "ZOSAPI_Interfaces.dll")


def _has_zos_dlls(directory: Path) -> bool:
    return directory.is_dir() and all((directory / dll).is_file() for dll in _REQUIRED_ZOS_DLLS)


def discover_opticstudio_directory(extra_roots: Iterable[Path] | None = None) -> str | None:
    """Scan common Windows OpticStudio installation locations as a fallback when registry lookup fails."""
    candidates: list[Path] = []
    search_bases: list[Path] = []
    for env_key in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432", "LOCALAPPDATA"):
        val = os.environ.get(env_key, "").strip()
        if val:
            search_bases.append(Path(val))
    if extra_roots is not None:
        search_bases.extend(extra_roots)

    for base in search_bases:
        candidates.append(base / "Zemax OpticStudio")
        candidates.extend(sorted((base / "ANSYS Inc").glob("*/Zemax OpticStudio"), reverse=True))
        candidates.extend(sorted(base.glob("Zemax*/ANSYS Inc/*/Zemax OpticStudio"), reverse=True))

    for drive in ("C:", "D:", "E:", "F:"):
        drive_root = Path(f"{drive}/")
        if not drive_root.exists():
            continue
        candidates.append(drive_root / "Zemax OpticStudio")
        candidates.extend(sorted((drive_root / "ANSYS Inc").glob("*/Zemax OpticStudio"), reverse=True))
        candidates.extend(sorted((drive_root / "Zemax").glob("ANSYS Inc/*/Zemax OpticStudio"), reverse=True))

    seen: set[Path] = set()
    for path in candidates:
        if path in seen:
            continue
        seen.add(path)
        if _has_zos_dlls(path):
            return str(path)
    return None


class ZemaxConnectionManager:
    """Singleton connection manager with RLock serialization for COM/STA safety."""

    def __init__(self, opticstudio_directory: str | None = None):
        self.zos: Any = None
        self.oss: Any = None
        self._lock = threading.RLock()
        self._opticstudio_directory = opticstudio_directory

    @property
    def opticstudio_directory(self) -> str | None:
        """Return explicit constructor or OPTICSTUDIO_DIRECTORY path, or None for registry auto-discovery."""
        if self._opticstudio_directory:
            return self._opticstudio_directory
        env_dir = os.environ.get("OPTICSTUDIO_DIRECTORY", "").strip()
        return env_dir or None

    def connect(self) -> Any:
        with self._lock:
            if self.oss is None:
                zp = get_zospy()
                configured_dir = self.opticstudio_directory
                if configured_dir:
                    logger.info("Connecting to OpticStudio Interactive Extension via %s...", configured_dir)
                    self.zos = zp.ZOS(opticstudio_directory=configured_dir)
                else:
                    logger.info(
                        "Connecting to OpticStudio Interactive Extension (auto-locating via Windows Registry)..."
                    )
                    try:
                        self.zos = zp.ZOS(opticstudio_directory=None)
                    except Exception as reg_exc:
                        fallback_dir = discover_opticstudio_directory()
                        if fallback_dir is None:
                            raise RuntimeError(
                                "无法自动定位 Zemax OpticStudio 安装目录（注册表及常见路径均未找到 ZOSAPI.dll）。"
                                "请设置环境变量 OPTICSTUDIO_DIRECTORY 为包含 ZOSAPI.dll 的 OpticStudio 目录后重试。"
                            ) from reg_exc
                        logger.info(
                            "Registry auto-discovery failed (%s); falling back to discovered path: %s",
                            reg_exc,
                            fallback_dir,
                        )
                        self.zos = zp.ZOS(opticstudio_directory=fallback_dir)
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
