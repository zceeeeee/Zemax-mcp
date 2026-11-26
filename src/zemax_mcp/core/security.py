"""Path security sandbox for Zemax file load, save, and image export."""

from __future__ import annotations

import os
from pathlib import Path
from typing import List, Literal


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def get_allowed_roots(mode: Literal["read", "write"]) -> List[Path]:
    """Return resolved allowed directory roots for file operations."""
    workspace = Path(os.environ.get("ZEMAX_WORKSPACE_DIR", os.getcwd())).expanduser().resolve()
    roots = [workspace]
    if mode == "read":
        samples_dir = (Path.home() / "Documents" / "Zemax" / "Samples").expanduser().resolve()
        roots.append(samples_dir)
    return roots


def validate_file_path(
    filepath: str,
    mode: Literal["read", "write"],
    allowed_extensions: tuple[str, ...] = (".zmx",),
) -> Path:
    """Validate and resolve a file path against allowed workspace/sample roots."""
    if not filepath or not str(filepath).strip():
        raise ValueError("文件路径不能为空")

    resolved = Path(filepath).expanduser().resolve()
    if allowed_extensions and resolved.suffix.lower() not in allowed_extensions:
        exts = ", ".join(allowed_extensions)
        raise ValueError(f"不支持的文件扩展名 '{resolved.suffix}'，仅允许: {exts}")

    if os.environ.get("ZEMAX_ALLOW_UNSAFE_PATHS", "0") == "1":
        return resolved

    allowed_roots = get_allowed_roots(mode)
    if not any(_is_within(resolved, root) for root in allowed_roots):
        roots_str = "; ".join(str(r) for r in allowed_roots)
        raise PermissionError(
            f"路径安全拦截：'{resolved}' 超出允许的{mode}目录范围 ({roots_str})。"
            "如需放开限制，请设置环境变量 ZEMAX_WORKSPACE_DIR 或 ZEMAX_ALLOW_UNSAFE_PATHS=1。"
        )
    return resolved
