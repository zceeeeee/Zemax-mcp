"""FastAPI application with mounted Streamable HTTP MCP server and legacy HTTP endpoints."""

from __future__ import annotations

from contextlib import asynccontextmanager
import logging
from typing import Optional

import uvicorn
from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)

from zemax_mcp.core.connection import ZemaxConnectionManager
from zemax_mcp.core.snapshots import SnapshotManager
from zemax_mcp.core.zos_compat import _json_safe_state
from zemax_mcp.mcp_server import create_mcp_server
from zemax_mcp.schemas.optical import (
    EvaluateRequest,
    ExportLayoutRequest,
    FileRequest,
    HammerOptimizeRequest,
    OptimizeRequest,
    ParameterTuneRequest,
    PromoteAsphereRequest,
    ScaleSystemRequest,
    SnapshotCreateRequest,
    SnapshotRollbackRequest,
    SplitLensRequest,
    SurfacePatchRequest,
    SynthesizeDesignRequest,
    SystemBuildRequest,
)
from zemax_mcp.services import export_service, optical_service, synthesis_service, topology_service


def create_app(
    connection_manager: Optional[ZemaxConnectionManager] = None,
    connect_on_startup: bool = True,
    snapshot_manager: Optional[SnapshotManager] = None,
) -> FastAPI:
    """Create FastAPI app hosting both /mcp (Streamable HTTP) and legacy REST routes."""
    zm = connection_manager or ZemaxConnectionManager()
    sm = snapshot_manager or SnapshotManager()
    mcp_server = create_mcp_server(connection_manager=zm, snapshot_manager=sm)
    mcp_http_app = mcp_server.streamable_http_app(
        streamable_http_path="/",
        stateless_http=False,
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if connect_on_startup:
            try:
                zm.connect()
            except Exception as exc:
                logger.warning(
                    "启动时连接 OpticStudio 失败（将在后续工具调用时按需重连）: %s", exc
                )
        async with mcp_server.session_manager.run():
            yield

    app = FastAPI(
        title="Ansys Zemax 2024 R2 MCP & HTTP 守护进程",
        lifespan=lifespan,
    )
    app.state.connection_manager = zm
    app.state.mcp_server = mcp_server

    @app.exception_handler(RequestValidationError)
    async def request_validation_error_handler(request: Request, exc: RequestValidationError):
        """校验错误保留原始输入，非有限数值也需先转换再序列化。"""
        return JSONResponse(
            status_code=422,
            content=_json_safe_state(jsonable_encoder({"detail": exc.errors()})),
        )

    @app.get("/status")
    def get_status():
        return optical_service.safe_call(lambda: zm.run_locked(optical_service.get_status))

    @app.post("/load_file")
    def load_zmx_file(req: FileRequest):
        return optical_service.safe_call(
            lambda: zm.run_locked(
                lambda oss: optical_service.load_zmx_file(oss, req, snapshot_manager=sm)
            )
        )

    @app.post("/build")
    def build_system(req: SystemBuildRequest):
        return optical_service.safe_call(
            lambda: zm.run_locked(
                lambda oss: optical_service.build_system(oss, req, snapshot_manager=sm)
            )
        )

    @app.post("/patch_surface")
    def patch_surface(req: SurfacePatchRequest):
        return optical_service.safe_call(
            lambda: zm.run_locked(lambda oss: optical_service.patch_surface(oss, req)),
            include_trace=False,
        )

    @app.post("/quick_focus")
    def quick_focus():
        return optical_service.safe_call(lambda: zm.run_locked(optical_service.quick_focus))

    @app.post("/optimize")
    def run_optimization(req: OptimizeRequest):
        return optical_service.safe_call(
            lambda: zm.run_locked(
                lambda oss: optical_service.run_optimization(oss, req, snapshot_manager=sm)
            )
        )

    @app.post("/evaluate")
    def evaluate_image_quality(req: EvaluateRequest):
        return optical_service.safe_call(
            lambda: zm.run_locked(lambda oss: optical_service.evaluate_image_quality(oss, req))
        )

    @app.post("/tune")
    def tune_parameter(req: ParameterTuneRequest):
        return optical_service.safe_call(
            lambda: zm.run_locked(lambda oss: optical_service.tune_parameter(oss, req))
        )

    @app.post("/save")
    def save_file(req: FileRequest):
        return optical_service.safe_call(
            lambda: zm.run_locked(lambda oss: optical_service.save_zmx_file(oss, req)),
            include_trace=False,
        )

    @app.post("/snapshot")
    def create_snapshot(req: SnapshotCreateRequest):
        return optical_service.safe_call(
            lambda: {"status": "ok", "snapshot": zm.run_locked(lambda oss: sm.create(oss, label=req.label))},
            include_trace=False,
        )

    @app.get("/snapshots")
    def list_snapshots():
        return {"status": "ok", "snapshots": sm.list_snapshots()}

    @app.post("/undo")
    def undo_snapshot():
        def _undo():
            entry = zm.run_locked(sm.undo)
            state = zm.run_locked(optical_service.extract_system_state)
            return {"status": "ok", "restored_snapshot": entry, "data": state}

        return optical_service.safe_call(_undo, include_trace=False)

    @app.post("/rollback")
    def rollback_snapshot(req: SnapshotRollbackRequest):
        def _rb():
            entry = zm.run_locked(lambda oss: sm.rollback_to(oss, req.snapshot_id))
            state = zm.run_locked(optical_service.extract_system_state)
            return {"status": "ok", "restored_snapshot": entry, "data": state}

        return optical_service.safe_call(_rb, include_trace=False)

    @app.get("/aberrations")
    def get_aberrations():
        return optical_service.safe_call(
            lambda: zm.run_locked(optical_service.get_aberrations)
        )

    @app.post("/hammer_optimize")
    def hammer_optimize(req: HammerOptimizeRequest):
        return optical_service.safe_call(
            lambda: zm.run_locked(
                lambda oss: optical_service.run_hammer_optimization(oss, req, snapshot_manager=sm)
            )
        )

    @app.post("/synthesize")
    def synthesize_design(req: SynthesizeDesignRequest):
        if req.apply_to_system:
            return optical_service.safe_call(
                lambda: zm.run_locked(
                    lambda oss: synthesis_service.synthesize_initial_design(
                        req, oss=oss, snapshot_manager=sm
                    )
                )
            )
        return optical_service.safe_call(
            lambda: synthesis_service.synthesize_initial_design(req, oss=None)
        )

    @app.post("/scale")
    def scale_system(req: ScaleSystemRequest):
        return optical_service.safe_call(
            lambda: zm.run_locked(
                lambda oss: synthesis_service.scale_system(oss, req, snapshot_manager=sm)
            )
        )

    @app.post("/split_lens")
    def split_lens(req: SplitLensRequest):
        return optical_service.safe_call(
            lambda: zm.run_locked(
                lambda oss: topology_service.split_lens(oss, req, snapshot_manager=sm)
            )
        )

    @app.post("/promote_asphere")
    def promote_asphere(req: PromoteAsphereRequest):
        return optical_service.safe_call(
            lambda: zm.run_locked(
                lambda oss: topology_service.promote_asphere(oss, req, snapshot_manager=sm)
            )
        )

    @app.post("/export_image")
    def export_image(req: ExportLayoutRequest):
        return optical_service.safe_call(
            lambda: zm.run_locked(
                lambda oss: export_service.export_layout_image(
                    oss, viewer_type=req.viewer_type, output_path=req.output_path  # type: ignore[arg-type]
                )
            )
        )

    app.mount("/mcp", mcp_http_app)
    return app


def main() -> None:
    app = create_app()
    uvicorn.run(app, host="127.0.0.1", port=8765, workers=1, loop="asyncio")
