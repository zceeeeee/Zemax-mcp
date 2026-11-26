"""Optical layout & analysis image export service for multimodal MCP responses."""

from __future__ import annotations

import base64
import os
import time
from pathlib import Path
from typing import Any, Dict, Literal, Optional

from zemax_mcp.core.security import validate_file_path
from zemax_mcp.core.zos_compat import get_zospy

ViewerType = Literal["cross_section", "viewer_3d", "shaded_model"]


def export_layout_image(
    oss: Any,
    viewer_type: ViewerType = "cross_section",
    output_path: Optional[str] = None,
    zp_mod: Any = None,
) -> Dict[str, Any]:
    """Export 2D CrossSection, 3D Viewer, or ShadedModel PNG using ZOSPy and close window."""
    zp = zp_mod or get_zospy()

    if output_path:
        target_path = validate_file_path(
            output_path, mode="write", allowed_extensions=(".png",)
        )
    else:
        workspace = Path(os.environ.get("ZEMAX_WORKSPACE_DIR", os.getcwd())).expanduser().resolve()
        export_dir = workspace / "outputs" / "exports"
        export_dir.mkdir(parents=True, exist_ok=True)
        target_path = export_dir / f"{viewer_type}_{int(time.time() * 1000)}.png"

    target_path.parent.mkdir(parents=True, exist_ok=True)

    viewers = zp.analyses.systemviewers
    viewer_map = {
        "cross_section": getattr(viewers, "CrossSection", None),
        "viewer_3d": getattr(viewers, "Viewer3D", None),
        "shaded_model": getattr(viewers, "ShadedModel", None),
    }
    viewer_cls = viewer_map.get(viewer_type)
    if viewer_cls is None:
        raise ValueError(f"不支持的视图类型: {viewer_type}")

    analysis = viewer_cls()
    analysis.run(oss, image_output_file=str(target_path), oncomplete="Close")

    if not target_path.exists():
        raise RuntimeError(f"OpticStudio 未生成图像文件: {target_path}")

    raw_bytes = target_path.read_bytes()
    return {
        "status": "ok",
        "viewer_type": viewer_type,
        "image_path": str(target_path),
        "mime_type": "image/png",
        "size_bytes": len(raw_bytes),
        "data_base64": base64.b64encode(raw_bytes).decode("ascii"),
    }
