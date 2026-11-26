"""In-memory indexed .zmx snapshot stack for safe Undo/Rollback."""

from __future__ import annotations

import os
import tempfile
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass
class SnapshotEntry:
    snapshot_id: str
    label: str
    created_at: float
    filepath: str
    num_surfaces: Optional[int] = None


class SnapshotManager:
    """Bounded FIFO/LIFO snapshot stack backed by temporary .zmx files."""

    def __init__(self, max_snapshots: int = 10, snapshot_dir: Optional[str] = None):
        self.max_snapshots = max(1, int(max_snapshots))
        if snapshot_dir is not None:
            self._dir = Path(snapshot_dir)
            self._dir.mkdir(parents=True, exist_ok=True)
            self._temp_dir = None
        else:
            self._temp_dir = tempfile.TemporaryDirectory(prefix="zemax_mcp_snap_")
            self._dir = Path(self._temp_dir.name)
        self._stack: List[SnapshotEntry] = []
        self._lock = threading.RLock()

    def create(self, oss: Any, label: str = "manual") -> Dict[str, Any]:
        """Capture current optical system state to a snapshot."""
        with self._lock:
            snap_id = f"snap_{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}"
            snap_path = self._dir / f"{snap_id}.zmx"
            oss.save_as(str(snap_path))
            num_surfaces = None
            try:
                num_surfaces = int(oss.LDE.NumberOfSurfaces)
            except Exception:
                pass
            entry = SnapshotEntry(
                snapshot_id=snap_id,
                label=label,
                created_at=time.time(),
                filepath=str(snap_path),
                num_surfaces=num_surfaces,
            )
            self._stack.append(entry)
            while len(self._stack) > self.max_snapshots:
                evicted = self._stack.pop(0)
                try:
                    if os.path.exists(evicted.filepath):
                        os.remove(evicted.filepath)
                except OSError:
                    pass
            return {
                "snapshot_id": entry.snapshot_id,
                "label": entry.label,
                "num_surfaces": entry.num_surfaces,
                "stack_size": len(self._stack),
            }

    def list_snapshots(self) -> List[Dict[str, Any]]:
        """Return snapshots from newest to oldest."""
        with self._lock:
            return [
                {
                    "snapshot_id": e.snapshot_id,
                    "label": e.label,
                    "created_at": e.created_at,
                    "num_surfaces": e.num_surfaces,
                }
                for e in reversed(self._stack)
            ]

    def undo(self, oss: Any) -> Dict[str, Any]:
        """Pop the latest snapshot and restore it into OpticStudio."""
        with self._lock:
            if not self._stack:
                raise RuntimeError("快照栈为空，没有可撤销的历史状态")
            entry = self._stack.pop()
            if not os.path.exists(entry.filepath):
                raise RuntimeError(f"快照文件已丢失: {entry.snapshot_id}")
            oss.load(entry.filepath)
            try:
                os.remove(entry.filepath)
            except OSError:
                pass
            return asdict(entry)

    def rollback_to(self, oss: Any, snapshot_id: str) -> Dict[str, Any]:
        """Restore a specific snapshot by ID without discarding earlier ones."""
        with self._lock:
            idx = next(
                (i for i, e in enumerate(self._stack) if e.snapshot_id == snapshot_id),
                None,
            )
            if idx is None:
                raise RuntimeError(f"未找到指定快照: {snapshot_id}")
            entry = self._stack[idx]
            if not os.path.exists(entry.filepath):
                raise RuntimeError(f"快照文件已丢失: {snapshot_id}")
            oss.load(entry.filepath)
            return asdict(entry)
