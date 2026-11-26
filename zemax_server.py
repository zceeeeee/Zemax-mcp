"""Backward-compatible root entrypoint for Zemax MCP + HTTP daemon."""

from __future__ import annotations

import os
import sys
from pathlib import Path

_SRC_DIR = str(Path(__file__).resolve().parent / "src")
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from zemax_mcp.api.http_app import create_app, main
from zemax_mcp.core.connection import ZemaxConnectionManager
from zemax_mcp.core.zos_compat import _json_safe_state
from zemax_mcp.schemas.optical import (
    CustomOperandItem,
    EvaluateRequest,
    FileRequest,
    OptimizeRequest,
    ParameterTuneRequest,
    SurfaceItem,
    SurfacePatchRequest,
    SystemBuildRequest,
)
from zemax_mcp.services.optical_service import analyze_sweep_trend

zm = ZemaxConnectionManager()
app = create_app(
    connection_manager=zm,
    connect_on_startup=os.environ.get("ZEMAX_SKIP_STARTUP_CONNECT", "0") != "1",
)

if __name__ == "__main__":
    main()
