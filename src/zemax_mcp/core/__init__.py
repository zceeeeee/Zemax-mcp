"""Core package init."""

from zemax_mcp.core.connection import ZemaxConnectionManager
from zemax_mcp.core.zos_compat import _json_safe_state

__all__ = ["ZemaxConnectionManager", "_json_safe_state"]
