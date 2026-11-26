"""Offline pytest suite for zemax_mcp package, MCPServer tools/resources/prompts, and Streamable HTTP."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from zemax_mcp.api.http_app import create_app
from zemax_mcp.core.connection import ZemaxConnectionManager
from zemax_mcp.core.security import validate_file_path
from zemax_mcp.core.snapshots import SnapshotManager
from zemax_mcp.core.zos_compat import _json_safe_state
from zemax_mcp.mcp_server import create_mcp_server
from zemax_mcp.schemas.optical import (
    EvaluateRequest,
    FieldItem,
    HammerOptimizeRequest,
    OptimizeRequest,
    ParameterTuneRequest,
    PromoteAsphereRequest,
    ScaleSystemRequest,
    SplitLensRequest,
    SurfaceItem,
    SurfacePatchRequest,
    SynthesizeDesignRequest,
    SystemBuildRequest,
)
from zemax_mcp.services import export_service, optical_service, synthesis_service, topology_service


class _FakeConnectionManager(ZemaxConnectionManager):
    """In-memory connection manager that yields a mock OSS without touching OpticStudio."""

    def __init__(self, fake_oss):
        super().__init__()
        self.oss = fake_oss

    def connect(self):
        return self.oss


def _make_fake_oss():
    s0 = SimpleNamespace(Comment="OBJ", IsStop=False, Radius=0.0, Thickness=float("inf"), Material="", SemiDiameter=0.0)
    s1 = SimpleNamespace(Comment="Lens1", IsStop=True, Radius=100.0, Thickness=5.0, Material="N-BK7", SemiDiameter=12.5)
    s2 = SimpleNamespace(Comment="Back", IsStop=False, Radius=-100.0, Thickness=95.0, Material="", SemiDiameter=12.5)
    s3 = SimpleNamespace(Comment="IMA", IsStop=False, Radius=0.0, Thickness=0.0, Material="", SemiDiameter=1.0)
    surfaces = [s0, s1, s2, s3]

    lde = MagicMock()
    lde.NumberOfSurfaces = len(surfaces)
    lde.GetSurfaceAt.side_effect = lambda idx: surfaces[idx]

    mfe = MagicMock()
    mfe.CalculateMeritFunction.return_value = 0.05
    mfe.GetOperandValue.return_value = 0.01

    spot = MagicMock()
    spot.Succeeded = True
    radii = MagicMock()
    radii.GetLength.side_effect = lambda axis: 1
    radii.GetValue.return_value = 0.005
    spot.RMSSpotRadii = radii

    tools = MagicMock()
    tools.OpenRMSSpotRadiusCalculator.return_value = spot

    wl = SimpleNamespace(IsPrimary=True)
    wavelengths = MagicMock()
    wavelengths.NumberOfWavelengths = 1
    wavelengths.GetWavelength.return_value = wl

    system_data = SimpleNamespace(Wavelengths=wavelengths)
    return SimpleNamespace(LDE=lde, MFE=mfe, Tools=tools, SystemData=system_data)


def test_json_safe_state_handles_nonfinite():
    payload = {"a": float("inf"), "b": float("-inf"), "c": float("nan"), "nested": [1.0, float("inf")]}
    safe = _json_safe_state(payload)
    assert safe == {"a": "Infinity", "b": "-Infinity", "c": None, "nested": [1.0, "Infinity"]}


def test_mcp_server_registers_tools_resources_prompts():
    fake_oss = _make_fake_oss()
    zm = _FakeConnectionManager(fake_oss)
    mcp = create_mcp_server(connection_manager=zm)

    async def _inspect():
        tools = await mcp.list_tools()
        resources = await mcp.list_resources()
        prompts = await mcp.list_prompts()
        return tools, resources, prompts

    tools, resources, prompts = asyncio.run(_inspect())
    tool_names = {t.name for t in tools}
    assert {
        "zemax_get_status",
        "zemax_load_file",
        "zemax_save_file",
        "zemax_build_system",
        "zemax_patch_surface",
        "zemax_quick_focus",
        "zemax_optimize",
        "zemax_evaluate",
        "zemax_tune_parameter",
    }.issubset(tool_names)

    resource_uris = {str(r.uri) for r in resources}
    assert "zemax://system/state" in resource_uris
    assert "zemax://samples/list" in resource_uris

    prompt_names = {p.name for p in prompts}
    assert {
        "design_singlet_workflow",
        "design_afocal_telescope_workflow",
        "diagnose_tir_9e9",
    }.issubset(prompt_names)


def test_mcp_tool_call_status_and_resource():
    fake_oss = _make_fake_oss()
    zm = _FakeConnectionManager(fake_oss)
    mcp = create_mcp_server(connection_manager=zm)

    async def _call():
        res = await mcp.call_tool("zemax_get_status", {})
        read_res = await mcp.read_resource("zemax://system/state")
        return res, read_res

    tool_result, resource_contents = asyncio.run(_call())
    # call_tool returns a CallToolResult or list[ContentBlock] depending on mcp version
    text_out = (
        tool_result.content[0].text
        if hasattr(tool_result, "content")
        else tool_result[0].text
    )
    parsed = json.loads(text_out)
    assert parsed["status"] == "ok"
    assert parsed["data"]["num_surfaces"] == 4
    assert parsed["data"]["surfaces"][0]["thickness"] == "Infinity"

    res_text = resource_contents[0].content if hasattr(resource_contents[0], "content") else str(resource_contents[0])
    assert '"status": "ok"' in res_text


def test_http_app_legacy_routes_and_mcp_mount():
    fake_oss = _make_fake_oss()
    zm = _FakeConnectionManager(fake_oss)
    app = create_app(connection_manager=zm, connect_on_startup=False)

    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        resp = client.get("/status")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        assert body["data"]["num_surfaces"] == 4

        # Non-finite input returns JSON 422 instead of 500
        bad_resp = client.post(
            "/patch_surface",
            content='{"surface_index": 1, "semi_diameter": 1e309}',
            headers={"Content-Type": "application/json"},
        )
        assert bad_resp.status_code == 422

        # Streamable HTTP MCP endpoint is mounted at /mcp
        mcp_resp = client.post(
            "/mcp",
            headers={"Accept": "application/json, text/event-stream"},
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "pytest", "version": "0.1"},
                },
            },
        )
        assert mcp_resp.status_code == 200
        assert "zemax-opticstudio" in mcp_resp.text


def test_analyze_sweep_trend_interior_minimum():
    results = [
        {"value": 50.0, "ray_trace_valid": True, "metric_value": 10.0},
        {"value": 60.0, "ray_trace_valid": True, "metric_value": 2.0},
        {"value": 70.0, "ray_trace_valid": True, "metric_value": 8.0},
    ]
    trend = optical_service.analyze_sweep_trend(results, results[1], 50.0, 70.0)
    assert trend["direction"] == "interior_minimum"
    assert trend["recommended_range"] == [50.0, 70.0]


def test_path_security_sandbox(tmp_path, monkeypatch):
    monkeypatch.setenv("ZEMAX_WORKSPACE_DIR", str(tmp_path))
    monkeypatch.delenv("ZEMAX_ALLOW_UNSAFE_PATHS", raising=False)

    inside = validate_file_path(str(tmp_path / "outputs" / "lens.zmx"), mode="write")
    assert inside == (tmp_path / "outputs" / "lens.zmx").resolve()

    with pytest.raises(PermissionError):
        validate_file_path(str(tmp_path.parent / "outside.zmx"), mode="write")

    with pytest.raises(ValueError):
        validate_file_path(str(tmp_path / "bad.txt"), mode="write")


def test_snapshot_manager_undo_and_bounded_stack(tmp_path):
    fake_oss = _make_fake_oss()
    fake_oss.save_as = lambda p: open(p, "w", encoding="utf-8").write("ZMX")
    fake_oss.load = MagicMock()

    sm = SnapshotManager(max_snapshots=2, snapshot_dir=str(tmp_path))
    s1 = sm.create(fake_oss, label="step1")
    s2 = sm.create(fake_oss, label="step2")
    s3 = sm.create(fake_oss, label="step3")

    snaps = sm.list_snapshots()
    assert len(snaps) == 2
    assert [s["label"] for s in snaps] == ["step3", "step2"]

    restored = sm.undo(fake_oss)
    assert restored["snapshot_id"] == s3["snapshot_id"]
    fake_oss.load.assert_called_once()
    assert len(sm.list_snapshots()) == 1

    # Rollback to s2
    rb = sm.rollback_to(fake_oss, s2["snapshot_id"])
    assert rb["snapshot_id"] == s2["snapshot_id"]


def test_mcp_snapshot_and_export_layout_image(tmp_path, monkeypatch):
    monkeypatch.setenv("ZEMAX_WORKSPACE_DIR", str(tmp_path))
    fake_oss = _make_fake_oss()
    fake_oss.save_as = lambda p: open(p, "w", encoding="utf-8").write("ZMX")
    fake_oss.load = MagicMock()

    zm = _FakeConnectionManager(fake_oss)
    sm = SnapshotManager(max_snapshots=5, snapshot_dir=str(tmp_path / "snaps"))
    mcp = create_mcp_server(connection_manager=zm, snapshot_manager=sm)

    class _FakeViewer:
        def run(self, oss, image_output_file=None, oncomplete=None):
            assert oncomplete == "Close"
            with open(image_output_file, "wb") as f:
                f.write(b"\x89PNG\r\n\x1a\nFAKEPNG")

    fake_zp = SimpleNamespace(
        analyses=SimpleNamespace(
            systemviewers=SimpleNamespace(CrossSection=_FakeViewer)
        )
    )
    monkeypatch.setattr(export_service, "get_zospy", lambda: fake_zp)

    async def _run():
        snap_res = await mcp.call_tool("zemax_snapshot_create", {"label": "before_opt"})
        list_res = await mcp.call_tool("zemax_list_snapshots", {})
        img_res = await mcp.call_tool("zemax_export_layout", {"viewer_type": "cross_section"})
        undo_res = await mcp.call_tool("zemax_undo", {})
        return snap_res, list_res, img_res, undo_res

    snap_res, list_res, img_res, undo_res = asyncio.run(_run())
    snap_data = json.loads(snap_res.content[0].text)
    assert snap_data["status"] == "ok"
    assert snap_data["snapshot"]["label"] == "before_opt"

    list_data = json.loads(list_res.content[0].text)
    assert len(list_data["snapshots"]) == 1

    # img_res should contain an ImageContent (type == "image") and metadata TextContent
    content_blocks = img_res.content if hasattr(img_res, "content") else img_res
    types = [getattr(b, "type", None) for b in content_blocks]
    assert "image" in types
    assert "text" in types

    undo_data = json.loads(undo_res.content[0].text)
    assert undo_data["status"] == "ok"
    assert undo_data["restored_snapshot"]["label"] == "before_opt"


def test_conic_and_even_asphere_validation_build_patch_and_extract():
    from pydantic import ValidationError

    # 1. Invalid asphere order rejected
    with pytest.raises(ValidationError):
        SurfaceItem(surface_type="EvenAsphere", asphere_terms={3: 1e-5})

    with pytest.raises(ValidationError):
        SurfacePatchRequest(surface_index=1, asphere_terms_variable={5: True})

    # 2. Mock LDE surface supporting TypeName, Conic, ConicCell, ChangeType, GetSurfaceCell
    class _FakeCell:
        def __init__(self, val=0.0):
            self.DoubleValue = val
            self.solves = []

        def CreateSolveType(self, st):
            return SimpleNamespace(solve_type=st)

        def SetSolveData(self, s):
            self.solves.append(s)

    class _FakeSurf:
        def __init__(self):
            self.Comment = ""
            self.TypeName = "Standard"
            self.IsStop = False
            self.Radius = 0.0
            self.Thickness = 0.0
            self.Material = ""
            self.Conic = 0.0
            self.SemiDiameter = 12.5
            self.RadiusCell = _FakeCell()
            self.ThicknessCell = _FakeCell()
            self.ConicCell = _FakeCell()
            self.par_cells = {f"Par{i}": _FakeCell(0.0) for i in range(1, 9)}
            self.TypeData = SimpleNamespace(CanBeStop=True)

        def GetSurfaceTypeSettings(self, st):
            return SimpleNamespace(target_type=st)

        def ChangeType(self, settings):
            if settings.target_type == "EvenAspheric":
                self.TypeName = "Even Asphere"
            else:
                self.TypeName = "Standard"

        def GetSurfaceCell(self, col):
            return self.par_cells[col]

    s0, s1, s2 = _FakeSurf(), _FakeSurf(), _FakeSurf()
    surfaces = [s0, s1, s2]
    lde = MagicMock()
    lde.NumberOfSurfaces = 3
    lde.GetSurfaceAt.side_effect = lambda i: surfaces[i]

    fake_zp = SimpleNamespace(
        constants=SimpleNamespace(
            SystemData=SimpleNamespace(
                ZemaxApertureType=SimpleNamespace(EntrancePupilDiameter="EPD"),
                WavelengthPreset=SimpleNamespace(HeNe_0p6328="HeNe"),
            ),
            Editors=SimpleNamespace(
                SolveType=SimpleNamespace(Variable="VAR", Fixed="FIX", MarginalRayHeight="MRH"),
                LDE=SimpleNamespace(
                    SurfaceType=SimpleNamespace(Standard="Standard", EvenAspheric="EvenAspheric"),
                    SurfaceColumn=SimpleNamespace(**{f"Par{i}": f"Par{i}" for i in range(1, 9)}),
                ),
            ),
        )
    )

    fields = MagicMock()
    wavelengths = MagicMock()
    wavelengths.NumberOfWavelengths = 1
    wavelengths.GetWavelength.return_value = SimpleNamespace(IsPrimary=True)
    oss = SimpleNamespace(
        new=MagicMock(),
        SystemData=SimpleNamespace(
            Aperture=SimpleNamespace(ApertureType=None, ApertureValue=None, AfocalImageSpace=False),
            Wavelengths=wavelengths,
            Fields=fields,
        ),
        LDE=lde,
        MFE=SimpleNamespace(CalculateMeritFunction=lambda: 0.01),
        Tools=SimpleNamespace(OpenRMSSpotRadiusCalculator=lambda: None),
    )

    build_req = SystemBuildRequest(
        epd=35.0,
        surfaces=[
            SurfaceItem(
                comment="AsphericFront",
                surface_type="EvenAsphere",
                radius=60.0,
                thickness=5.0,
                material="N-BK7",
                conic=-0.5,
                conic_variable=True,
                asphere_terms={4: -1.5e-6, 6: 2.0e-9},
                asphere_terms_variable=[4, 6],
            )
        ],
    )
    res_build = optical_service.build_system(oss, build_req, zp_mod=fake_zp)
    assert res_build["status"] == "ok"
    assert s1.TypeName == "Even Asphere"
    assert s1.Conic == -0.5
    assert s1.ConicCell.solves[-1].solve_type == "VAR"
    assert s1.par_cells["Par2"].DoubleValue == -1.5e-6
    assert s1.par_cells["Par3"].DoubleValue == 2.0e-9
    assert s1.par_cells["Par2"].solves[-1].solve_type == "VAR"

    surf_out = res_build["data"]["surfaces"][1]
    assert surf_out["surface_type"] == "Even Asphere"
    assert surf_out["conic"] == -0.5
    assert surf_out["asphere_terms"] == {"4": -1.5e-6, "6": 2e-09}

    # Patch conic & 8th-order term
    patch_req = SurfacePatchRequest(
        surface_index=1,
        conic=-0.8,
        asphere_terms={8: -3.0e-12},
        asphere_terms_variable={4: False, 8: True},
    )
    res_patch = optical_service.patch_surface(oss, patch_req, zp_mod=fake_zp)
    assert res_patch["status"] == "ok"
    assert s1.Conic == -0.8
    assert s1.par_cells["Par4"].DoubleValue == -3.0e-12
    assert s1.par_cells["Par2"].solves[-1].solve_type == "FIX"
    assert s1.par_cells["Par4"].solves[-1].solve_type == "VAR"


def test_http_routes_create_auto_snapshots_and_tuner_best_candidate(tmp_path, monkeypatch):
    fake_oss = _make_fake_oss()
    fake_oss.save_as = lambda p: open(p, "w", encoding="utf-8").write("ZMX")
    fake_oss.load = MagicMock()
    fake_oss.new = MagicMock()
    fake_oss.SystemData = SimpleNamespace(
        Aperture=SimpleNamespace(ApertureType=None, ApertureValue=None, AfocalImageSpace=False),
        Wavelengths=SimpleNamespace(
            SelectWavelengthPreset=MagicMock(),
            NumberOfWavelengths=1,
            GetWavelength=lambda _: SimpleNamespace(IsPrimary=True),
        ),
        Fields=SimpleNamespace(DeleteAllFields=MagicMock(), GetField=lambda _: SimpleNamespace(Y=0.0), AddField=MagicMock()),
    )

    zm = _FakeConnectionManager(fake_oss)
    sm = SnapshotManager(max_snapshots=5, snapshot_dir=str(tmp_path))
    app = create_app(connection_manager=zm, connect_on_startup=False, snapshot_manager=sm)

    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        # Calling HTTP /load_file should push an auto snapshot
        zmx_file = tmp_path / "lens.zmx"
        zmx_file.write_text("ZMX", encoding="utf-8")
        monkeypatch.setenv("ZEMAX_WORKSPACE_DIR", str(tmp_path))
        resp = client.post("/load_file", json={"filepath": str(zmx_file)})
        assert resp.status_code == 200
        snaps = client.get("/snapshots").json()["snapshots"]
        assert len(snaps) == 1
        assert snaps[0]["label"] == "auto:before_load"

    # Two-stage tuner exposes top-level best_candidate
    import zemax_tuner

    monkeypatch.setattr(
        zemax_tuner,
        "call_tune_api",
        lambda **kw: {
            "status": "ok",
            "best_candidate": {"value": 55.0, "metric_value": 1.2},
            "trend_analysis": {"summary": "ok", "recommended_range": [52.0, 58.0]},
        },
    )
    res_2s = zemax_tuner.two_stage_tune(1, "conic", -1.5, 0.5)
    assert res_2s["best_candidate"] == {"value": 55.0, "metric_value": 1.2}
    assert res_2s["final_best"] == {"value": 55.0, "metric_value": 1.2}


def test_connection_self_heal_and_seidel_aberrations_and_custom_build():
    from pydantic import ValidationError

    # 1. Validation: missing epd/aperture_value fails unless FloatByStopSize
    with pytest.raises(ValidationError):
        SystemBuildRequest(surfaces=[])
    ok_float = SystemBuildRequest(aperture_type="FloatByStopSize", surfaces=[])
    assert ok_float.aperture_type == "FloatByStopSize"

    # 2. ConnectionManager self-heal on stale session
    zm = ZemaxConnectionManager()
    stale_oss = SimpleNamespace(LDE=SimpleNamespace(NumberOfSurfaces=property(lambda _: (_ for _ in ()).throw(RuntimeError("COM dead")))))
    # Make LDE.NumberOfSurfaces raise when accessed
    class _BrokenLDE:
        @property
        def NumberOfSurfaces(self):
            raise RuntimeError("COM dead")
    zm.oss = SimpleNamespace(LDE=_BrokenLDE())
    fresh_oss = _make_fake_oss()
    zm.connect = lambda: setattr(zm, "oss", fresh_oss) or fresh_oss
    assert zm.run_locked(lambda oss: oss.LDE.NumberOfSurfaces) == 4

    # 3. Seidel aberrations tool
    op_vals = {"SPHA_OP": 0.12, "COMA_OP": -0.03, "ASTI_OP": 0.01, "FCUR_OP": 0.02, "DIST_OP": -0.005, "AXCL_OP": 0.04}
    fake_zp = SimpleNamespace(
        constants=SimpleNamespace(
            Editors=SimpleNamespace(
                MFE=SimpleNamespace(
                    MeritOperandType=SimpleNamespace(
                        SPHA="SPHA_OP", COMA="COMA_OP", ASTI="ASTI_OP", FCUR="FCUR_OP", DIST="DIST_OP", AXCL="AXCL_OP"
                    )
                )
            )
        )
    )
    fake_oss = _make_fake_oss()
    fake_oss.SystemData.Wavelengths.NumberOfWavelengths = 2
    fake_oss.MFE.GetOperandValue.side_effect = lambda op, *a: op_vals[op]
    ab = optical_service.get_aberrations(fake_oss, zp_mod=fake_zp)
    assert ab["status"] == "ok"
    assert ab["system_totals"]["spha"] == 0.12
    assert ab["system_totals"]["axcl_mm"] == 0.04
    assert len(ab["per_surface"]) == 2


def test_mneg_only_targets_glass_surfaces():
    fake_oss = _make_fake_oss()  # s1 has Material="N-BK7", s2 has Material=""
    mfe = MagicMock()
    inserted_rows = []

    def _fake_insert(mfe_arg, row_idx, op_enum, target, weight, int1=None, int2=None, **kw):
        inserted_rows.append((row_idx, op_enum, target, int1, int2))

    op_enum = SimpleNamespace(MNEG="MNEG_OP")
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(optical_service, "_insert_operand_row", _fake_insert)
        next_row = optical_service._insert_glass_min_edge_constraints(
            mfe, fake_oss, 1, 1.5, op_enum
        )
    assert next_row == 2
    assert inserted_rows == [(1, "MNEG_OP", 1.5, 1, 1)]


def test_enhanced_aberration_attribution_and_hints():
    fake_oss = _make_fake_oss()
    fake_oss.SystemData.Wavelengths.NumberOfWavelengths = 2
    # Make surface 1 steep (|R|/SD = 15/12.5 = 1.2 < 1.5) and dominant high-order wavefront
    fake_oss.LDE.GetSurfaceAt(1).Radius = 15.0
    fake_oss.LDE.GetSurfaceAt(1).SemiDiameter = 12.5

    op_map = {
        ("SPHA_OP", 0): 0.02, ("SPHA_OP", 1): 0.08, ("SPHA_OP", 2): -0.06,
        ("COMA_OP", 0): 0.01, ("COMA_OP", 1): 0.09, ("COMA_OP", 2): -0.08,
        ("ASTI_OP", 0): 0.005, ("ASTI_OP", 1): 0.005, ("ASTI_OP", 2): 0.0,
        ("FCUR_OP", 0): 0.01, ("FCUR_OP", 1): 0.01, ("FCUR_OP", 2): 0.0,
        ("DIST_OP", 0): 0.0, ("DIST_OP", 1): 0.0, ("DIST_OP", 2): 0.0,
        ("PETZ_OP", 0): 0.01, ("PETZ_OP", 1): 0.01, ("PETZ_OP", 2): 0.0,
    }

    def _get_op(op, surf_idx, *args):
        if op == "AXCL_OP":
            return 0.35
        if op == "LACL_OP":
            return 0.02
        if op == "RWCE_OP":
            return 0.65
        return op_map.get((op, surf_idx), 0.0)

    fake_oss.MFE.GetOperandValue.side_effect = _get_op
    fake_zp = SimpleNamespace(
        constants=SimpleNamespace(
            Editors=SimpleNamespace(
                MFE=SimpleNamespace(
                    MeritOperandType=SimpleNamespace(
                        SPHA="SPHA_OP", COMA="COMA_OP", ASTI="ASTI_OP",
                        FCUR="FCUR_OP", DIST="DIST_OP", PETZ="PETZ_OP",
                        AXCL="AXCL_OP", LACL="LACL_OP", RWCE="RWCE_OP",
                    )
                )
            )
        )
    )
    res = optical_service.get_aberrations(fake_oss, zp_mod=fake_zp)
    assert res["status"] == "ok"
    assert res["dominant_regime"] == "high_order"
    assert res["system_totals"]["axcl_mm"] == 0.35
    assert res["system_totals"]["lacl_mm"] == 0.02
    assert res["per_surface"][0]["spha_share_pct"] == round(0.08 / 0.14 * 100.0, 1)
    assert any("高级像差主导" in h for h in res["attribution_hints"])
    assert any("轴向色差偏大" in h for h in res["attribution_hints"])
    assert any("曲率偏陡" in h for h in res["attribution_hints"])


def test_hammer_optimization_and_material_substitute(tmp_path):
    fake_oss = _make_fake_oss()
    fake_oss.save_as = lambda p: open(p, "w", encoding="utf-8").write("ZMX")
    fake_oss.MFE.SEQOptimizationWizard = SimpleNamespace(
        Type=0, Data=0, OverallWeight=1.0, IsGlassUsed=True, GlassMin=2.0, GlassMax=30.0, GlassEdge=1.5,
        IsAirUsed=True, AirMin=0.5, AirMax=500.0, AirEdge=0.5, StartAt=1, IsAddFavoriteOperandsUsed=False,
        Apply=MagicMock(),
    )
    fake_oss.MFE.NumberOfOperands = 0
    fake_oss.MFE.GetOperandValue.return_value = 100.0

    hammer_tool = MagicMock()
    hammer_tool.RunAndWaitWithTimeout.return_value = "TimedOut"
    hammer_tool.Succeeded = True
    hammer_tool.ErrorMessage = ""
    fake_oss.Tools.OpenHammerOptimization = MagicMock(return_value=hammer_tool)

    fake_zp = SimpleNamespace(
        constants=SimpleNamespace(
            Editors=SimpleNamespace(
                MFE=SimpleNamespace(
                    MeritOperandType=SimpleNamespace(
                        EFFL="EFFL", TOTR="TOTR", AXCL="AXCL", OPLT="OPLT", OPGT="OPGT", TTHI="TTHI", MNEG="MNEG", TRAC="TRAC"
                    ),
                    MeritColumn=SimpleNamespace(Param1=1, Param2=2, Param3=3, Param4=4),
                ),
                SolveType=SimpleNamespace(Variable="VAR", Fixed="FIX", MaterialSubstitute="MATSUB"),
            ),
            Tools=SimpleNamespace(
                RunStatus=SimpleNamespace(TimedOut="TimedOut"),
                Optimization=SimpleNamespace(OptimizationAlgorithm=SimpleNamespace(DampedLeastSquares="DLS")),
            ),
        )
    )

    sm = SnapshotManager(max_snapshots=5, snapshot_dir=str(tmp_path))
    req = HammerOptimizeRequest(target_run_time_minutes=0.25, target_effl=100.0)
    res = optical_service.run_hammer_optimization(fake_oss, req, zp_mod=fake_zp, snapshot_manager=sm)
    assert res["status"] == "ok"
    assert res["mode"] == "hammer"
    hammer_tool.Cancel.assert_called_once()
    hammer_tool.Close.assert_called_once()
    assert len(sm.list_snapshots()) == 1
    assert sm.list_snapshots()[0]["label"] == "auto:before_hammer"

    # Also test MaterialSubstitute helper
    mat_cell = MagicMock()
    solve_obj = SimpleNamespace(_S_MaterialSubstitute=SimpleNamespace(Catalog=""))
    mat_cell.CreateSolveType.return_value = solve_obj
    from zemax_mcp.core.zos_compat import _set_material_substitute

    _set_material_substitute(mat_cell, True, catalog="OHARA", zp_mod=fake_zp)
    assert solve_obj._S_MaterialSubstitute.Catalog == "OHARA"
    mat_cell.SetSolveData.assert_called_once_with(solve_obj)


def test_pw_synthesis_all_topologies_and_scale_fallback():
    for topo, expected_len in (("singlet_min_spha", 2), ("achromatic_doublet", 3), ("cooke_triplet", 6)):
        req = SynthesizeDesignRequest(
            topology=topo, target_effl=100.0, epd=25.0, fovs=[0.0, 3.0], apply_to_system=False
        )
        res = synthesis_service.synthesize_initial_design(req)
        assert res["status"] == "ok"
        assert len(res["synthesized_build_config"]["surfaces"]) == expected_len

    # Scale fallback (no OpenScale)
    fake_oss = _make_fake_oss()
    fake_oss.Tools.OpenScale = None
    fake_oss.SystemData.Aperture = SimpleNamespace(ApertureValue=25.0)
    fake_zp = SimpleNamespace(
        constants=SimpleNamespace(
            Editors=SimpleNamespace(MFE=SimpleNamespace(MeritOperandType=SimpleNamespace(EFFL="EFFL")))
        )
    )
    fake_oss.MFE.GetOperandValue.return_value = 50.0
    res_scale = synthesis_service.scale_system(fake_oss, ScaleSystemRequest(target_effl=100.0), zp_mod=fake_zp)
    assert res_scale["status"] == "ok"
    assert res_scale["scale_factor"] == 2.0
    assert fake_oss.LDE.GetSurfaceAt(1).Radius == 200.0
    assert fake_oss.SystemData.Aperture.ApertureValue == 50.0


def test_split_lens_success_and_auto_rollback(tmp_path):
    class _Cell:
        def CreateSolveType(self, st):
            return SimpleNamespace(st=st)

        def SetSolveData(self, s):
            pass

    def _mk_surf(r=50.0, t=5.0, m="", sd=12.5):
        return SimpleNamespace(
            Comment="", Radius=r, Thickness=t, Material=m, SemiDiameter=sd, IsStop=False,
            RadiusCell=_Cell(), ThicknessCell=_Cell(),
        )

    surfaces = [_mk_surf(0.0, float("inf")), _mk_surf(50.0, 6.0, "N-BK7"), _mk_surf(-50.0, 80.0, ""), _mk_surf(0.0, 0.0)]

    def _insert(idx):
        s = _mk_surf()
        surfaces.insert(idx, s)
        return s

    lde = MagicMock()
    lde.NumberOfSurfaces = property(lambda self: len(surfaces))
    type(lde).NumberOfSurfaces = property(lambda self: len(surfaces))
    lde.GetSurfaceAt.side_effect = lambda idx: surfaces[idx]
    lde.InsertNewSurfaceAt.side_effect = _insert

    spot = MagicMock()
    spot.Succeeded = True
    radii = MagicMock()
    radii.GetLength.return_value = 1
    radii.GetValue.return_value = 0.005
    spot.RMSSpotRadii = radii

    oss = SimpleNamespace(
        LDE=lde,
        MFE=SimpleNamespace(CalculateMeritFunction=lambda: 0.05, GetOperandValue=lambda *a: 0.01),
        Tools=SimpleNamespace(OpenRMSSpotRadiusCalculator=lambda: spot),
        SystemData=SimpleNamespace(Wavelengths=SimpleNamespace(NumberOfWavelengths=1, GetWavelength=lambda _: SimpleNamespace(IsPrimary=True))),
        save_as=lambda p: open(p, "w", encoding="utf-8").write("ZMX"),
        load=MagicMock(),
    )
    fake_zp = SimpleNamespace(
        constants=SimpleNamespace(
            Editors=SimpleNamespace(
                SolveType=SimpleNamespace(Variable="V", Fixed="F"),
                MFE=SimpleNamespace(MeritOperandType=SimpleNamespace(TRAC="TRAC")),
            )
        )
    )
    sm = SnapshotManager(max_snapshots=5, snapshot_dir=str(tmp_path))
    res = topology_service.split_lens(oss, SplitLensRequest(surface_index=1, split_ratio=0.5, air_gap=1.5), zp_mod=fake_zp, snapshot_manager=sm)
    assert res["status"] == "ok"
    assert len(surfaces) == 6
    assert surfaces[1].Material == "N-BK7"
    assert surfaces[2].Material == ""
    assert surfaces[3].Material == "N-BK7"

    # Failure auto-rollback check: simulate ray trace failure after split
    oss.Tools.OpenRMSSpotRadiusCalculator = lambda: None
    res_fail = topology_service.split_lens(oss, SplitLensRequest(surface_index=1), zp_mod=fake_zp, snapshot_manager=sm)
    assert res_fail["status"] == "error"
    assert res_fail["auto_rolled_back"] is True
    oss.load.assert_called_once()


def test_mcp_registers_all_20_tools_and_5_prompts():
    mcp = create_mcp_server(connection_manager=_FakeConnectionManager(_make_fake_oss()))

    async def _check():
        tools = await mcp.list_tools()
        prompts = await mcp.list_prompts()
        return {t.name for t in tools}, {p.name for p in prompts}

    tool_names, prompt_names = asyncio.run(_check())
    assert {
        "zemax_get_aberrations",
        "zemax_hammer_optimize",
        "zemax_synthesize_initial_design",
        "zemax_scale_system",
        "zemax_split_lens",
        "zemax_promote_asphere",
    }.issubset(tool_names)
    assert len(tool_names) == 20
    assert "physics_driven_design_loop" in prompt_names
    assert len(prompt_names) == 5


def test_promote_asphere_rejects_and_rolls_back_degenerate_conic(tmp_path, monkeypatch):
    fake_oss = _make_fake_oss()
    s1 = fake_oss.LDE.GetSurfaceAt(1)
    s1.TypeName = "Even Asphere"
    s1.Conic = 0.0
    s1.ConicCell = MagicMock()
    s1.RadiusCell = MagicMock(Solve="VAR")
    s1.GetSurfaceCell = MagicMock(return_value=MagicMock())
    fake_oss.save_as = lambda p: open(p, "w", encoding="utf-8").write("ZMX")
    fake_oss.load = MagicMock()

    fake_zp = SimpleNamespace(
        constants=SimpleNamespace(
            Editors=SimpleNamespace(
                SolveType=SimpleNamespace(Variable="VAR", Fixed="FIX"),
                LDE=SimpleNamespace(SurfaceColumn=SimpleNamespace(**{f"Par{i}": f"Par{i}" for i in range(1, 9)})),
            )
        )
    )

    def _fake_opt(oss_arg, req_arg, zp_mod=None, snapshot_manager=None):
        s1.Conic = 5000.0  # Simulate degenerate conic blow-up
        return {"status": "ok", "final_merit": 0.0, "achieved_constraints": {}, "data": {}}

    monkeypatch.setattr(optical_service, "run_optimization", _fake_opt)
    sm = SnapshotManager(max_snapshots=5, snapshot_dir=str(tmp_path))
    res = topology_service.promote_asphere(
        fake_oss, PromoteAsphereRequest(surface_index=1, max_order=6, target_effl=70.0), zp_mod=fake_zp, snapshot_manager=sm
    )
    assert res["status"] == "error"
    assert res["auto_rolled_back"] is True
    assert "病态值" in res["message"]
    fake_oss.load.assert_called_once()


def test_http_app_degrades_gracefully_when_opticstudio_unavailable():
    zm = ZemaxConnectionManager()
    zm.connect = MagicMock(side_effect=ConnectionRefusedError("OpticStudio not open"))
    app = create_app(connection_manager=zm, connect_on_startup=True)

    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        resp = client.get("/status")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "error"
        assert "OpticStudio not open" in body["message"]








