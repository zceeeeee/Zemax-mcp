"""Offline checks: extract helpers without importing ZOSPy or connecting to OpticStudio."""
import ast
import asyncio
import json
import math
from pathlib import Path
import sys
import traceback
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / "zemax_server.py").read_text(encoding="utf-8")
HELPERS = {
    "_insert_operand_row", "_get_primary_wavelength_index",
    "_get_rms_spot_radii", "_json_safe_state", "_extract_system_state",
    "_get_normalized_field_coordinates",
}
TREE = ast.parse(SOURCE)
NAMESPACE = {"math": math, "Dict": dict, "Any": object}
exec(compile(ast.Module(body=[
    node for node in TREE.body
    if isinstance(node, ast.FunctionDef) and node.name in HELPERS
], type_ignores=[]), "<server-helpers>", "exec"), NAMESPACE)


class Matrix:
    def GetLength(self, axis):
        return (2, 2)[axis]

    def GetValue(self, config, field):
        return ((0.01, 0.02), (0.03, 0.04))[config][field]


class SpotTool:
    def __init__(self, succeeded=True, run_error=False, read_error=False):
        self.Succeeded = succeeded
        self.ErrorMessage = "Spot calculation failed"
        self.run_error = run_error
        self.read_error = read_error
        self.closed = False

    def SetMethodGaussianQuadrature(self, rings, arms, obscuration):
        assert rings > 0 and arms > 0 and obscuration == 0.0

    def RunAndWaitForCompletion(self):
        if self.run_error:
            raise RuntimeError("Tool run failed")

    @property
    def RMSSpotRadii(self):
        if self.read_error:
            raise RuntimeError("Cannot read result")
        return Matrix()

    def Close(self):
        self.closed = True


def with_tool(tool):
    return SimpleNamespace(Tools=SimpleNamespace(OpenRMSSpotRadiusCalculator=lambda: tool))


def server_function(name, **bindings):
    route = next(node for node in ast.parse(SOURCE).body
                 if isinstance(node, ast.FunctionDef) and node.name == name)
    route.decorator_list = []
    namespace = dict(NAMESPACE, traceback=traceback, **bindings)
    exec(compile(ast.Module(body=[route], type_ignores=[]), "<server-route>", "exec"), namespace)
    return namespace[name]


def validation_app():
    from fastapi import FastAPI, Request
    from fastapi.encoders import jsonable_encoder
    from fastapi.exceptions import RequestValidationError
    from fastapi.responses import JSONResponse
    from pydantic import BaseModel, Field
    from typing import Optional

    namespace = dict(NAMESPACE, app=FastAPI(), BaseModel=BaseModel, Field=Field, Optional=Optional,
                     Request=Request, RequestValidationError=RequestValidationError,
                     JSONResponse=JSONResponse, jsonable_encoder=jsonable_encoder, zm=Mock())
    namespace["zm"].connect.side_effect = AssertionError("Validation must reject input before connecting")
    names = {"SurfacePatchRequest", "EvaluateRequest", "request_validation_error_handler",
             "patch_surface", "evaluate_image_quality"}
    nodes = [node for node in TREE.body
             if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names]
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "<validation-app>", "exec"), namespace)
    return namespace["app"], namespace["zm"]


async def asgi_post(app, path, body):
    messages = []
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
             "method": "POST", "scheme": "http", "path": path, "raw_path": path.encode(),
             "query_string": b"", "root_path": "", "server": ("testserver", 80),
             "client": ("127.0.0.1", 1234), "headers": [(b"content-type", b"application/json")]}
    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}
    async def send(message):
        messages.append(message)
    await app(scope, receive, send)
    status = next(message["status"] for message in messages if message["type"] == "http.response.start")
    content = b"".join(message.get("body", b"") for message in messages if message["type"] == "http.response.body")
    return status, json.loads(content)


class PatchSurface:
    def __init__(self, cell):
        self.SemiDiameterCell = cell
        self.SemiDiameter = 12.5
        self.Radius = 100.0
        self.TypeData = SimpleNamespace(CanBeStop=True)
        self.stop = False
        self.stop_writes = []

    @property
    def IsStop(self):
        return self.stop

    @IsStop.setter
    def IsStop(self, value):
        self.stop_writes.append(value)
        # 本机 API 不能通过写入 False 取消 STOP。
        self.stop = True


def surface_patch_fixture(**changes):
    editors = SimpleNamespace(
        SolveType=SimpleNamespace(Automatic=object(), Fixed=object()),
        SolveStatus=SimpleNamespace(Success=object()),
    )
    cell = Mock(Solve=editors.SolveType.Automatic)
    cell.IsSolveTypeSupported.return_value = True
    cell.CreateSolveType.return_value = SimpleNamespace(IsValid=True)
    def set_solve(_):
        cell.Solve = cell.CreateSolveType.call_args.args[0]
        return editors.SolveStatus.Success
    cell.SetSolveData.side_effect = set_solve
    surf = PatchSurface(cell)
    oss = SimpleNamespace(LDE=SimpleNamespace(NumberOfSurfaces=4, GetSurfaceAt=lambda _: surf))
    route = server_function(
        "patch_surface", SurfacePatchRequest=object, zm=SimpleNamespace(connect=lambda: oss),
        zp=SimpleNamespace(constants=SimpleNamespace(Editors=editors)), _extract_system_state=lambda _: {},
    )
    params = dict(surface_index=1, radius=None, thickness=None, material=None,
                  radius_variable=None, thickness_variable=None, semi_diameter=None,
                  semi_diameter_auto=False, is_stop=None)
    params.update(changes)
    return route, SimpleNamespace(**params), surf, cell, editors


class CompatibilityTests(unittest.TestCase):
    def test_syntax(self):
        for filename in ("zemax_server.py", "zemax_cli.py"):
            compile((ROOT / filename).read_text(encoding="utf-8"), filename, "exec")

    def test_primary_wavelength_uses_one_based_indices(self):
        for primary in (1, 2, 3):
            def get_wavelength(index):
                self.assertIn(index, (1, 2, 3))
                return SimpleNamespace(IsPrimary=index == primary)
            wavelengths = SimpleNamespace(NumberOfWavelengths=3, GetWavelength=get_wavelength)
            oss = SimpleNamespace(SystemData=SimpleNamespace(Wavelengths=wavelengths))
            self.assertEqual(NAMESPACE["_get_primary_wavelength_index"](oss), primary)

    def test_missing_primary_is_an_error(self):
        wavelengths = SimpleNamespace(
            NumberOfWavelengths=2, GetWavelength=lambda _: SimpleNamespace(IsPrimary=False)
        )
        oss = SimpleNamespace(SystemData=SimpleNamespace(Wavelengths=wavelengths))
        with self.assertRaises(RuntimeError):
            NAMESPACE["_get_primary_wavelength_index"](oss)

    def test_rms_matrix_and_successful_tool_close(self):
        tool = SpotTool()
        self.assertEqual(NAMESPACE["_get_rms_spot_radii"](with_tool(tool)), [[0.01, 0.02], [0.03, 0.04]])
        self.assertTrue(tool.closed)

    def test_tool_closes_on_run_result_and_status_errors(self):
        for tool in (SpotTool(succeeded=False), SpotTool(run_error=True), SpotTool(read_error=True)):
            with self.subTest(tool=tool):
                with self.assertRaises(RuntimeError):
                    NAMESPACE["_get_rms_spot_radii"](with_tool(tool))
                self.assertTrue(tool.closed)

    def test_unavailable_tool(self):
        with self.assertRaises(RuntimeError):
            NAMESPACE["_get_rms_spot_radii"](with_tool(None))

    def test_nonfinite_json_values_and_input_preservation(self):
        data = {"radii": [float("inf"), -float("inf"), float("nan"), 0.0, 12.5], "ok": True}
        converted = NAMESPACE["_json_safe_state"](data)
        self.assertEqual(converted, {"radii": ["Infinity", "-Infinity", None, 0.0, 12.5], "ok": True})
        self.assertTrue(math.isinf(data["radii"][0]))
        json.dumps(converted, allow_nan=False)

    def test_request_validation_nonfinite_inputs_return_json_422(self):
        app, manager = validation_app()
        cases = [
            ("/patch_surface", b'{"surface_index":1,"semi_diameter":1e309}', "semi_diameter", "Infinity"),
            ("/patch_surface", b'{"surface_index":1,"semi_diameter":-1e309}', "semi_diameter", "-Infinity"),
            ("/patch_surface", b'{"surface_index":1,"semi_diameter":NaN}', "semi_diameter", None),
            ("/evaluate", b'{"max_frequency":1e309}', "max_frequency", "Infinity"),
            ("/evaluate", b'{"max_frequency":-1e309}', "max_frequency", "-Infinity"),
            ("/evaluate", b'{"max_frequency":NaN}', "max_frequency", None),
            ("/evaluate", b'{"mtf_sampling":{"nested":[NaN,1e309]}}', "mtf_sampling", {"nested": [None, "Infinity"]}),
        ]
        for path, body, field, expected_input in cases:
            with self.subTest(path=path, body=body):
                status, result = asyncio.run(asgi_post(app, path, body))
                self.assertEqual(status, 422)
                error = result["detail"][0]
                self.assertEqual(error["loc"], ["body", field])
                self.assertEqual(error["input"], expected_input)
                json.dumps(result, allow_nan=False)
        manager.connect.assert_not_called()

    def test_request_validation_preserves_normal_error_details(self):
        app, manager = validation_app()
        for path, body, error_type in (
            ("/evaluate", b'{"max_frequency":0}', "greater_than"),
            ("/evaluate", b'{"num_freq_points":0}', "greater_than_equal"),
            ("/patch_surface", b'{}', "missing"),
            ("/evaluate", b'{"max_frequency":', "json_invalid"),
        ):
            with self.subTest(path=path, body=body):
                status, result = asyncio.run(asgi_post(app, path, body))
                self.assertEqual(status, 422)
                self.assertEqual(result["detail"][0]["type"], error_type)
                self.assertIn("msg", result["detail"][0])
                json.dumps(result, allow_nan=False)
        manager.connect.assert_not_called()

    def test_operand_cells_receive_enum_members(self):
        columns = SimpleNamespace(**{f"Param{i}": object() for i in range(1, 5)})
        cells = {getattr(columns, f"Param{i}"): SimpleNamespace() for i in range(1, 5)}
        row = SimpleNamespace(ChangeType=lambda _: None, GetOperandCell=lambda col: cells[col])
        mfe = SimpleNamespace(InsertNewOperandAt=lambda _: row)
        old_zp = NAMESPACE.get("zp")
        NAMESPACE["zp"] = SimpleNamespace(constants=SimpleNamespace(
            Editors=SimpleNamespace(MFE=SimpleNamespace(MeritColumn=columns))
        ))
        try:
            result = NAMESPACE["_insert_operand_row"](mfe, 1, object(), 100, 10, 1, 2, 3.5, 4.5)
        finally:
            if old_zp is None:
                del NAMESPACE["zp"]
            else:
                NAMESPACE["zp"] = old_zp
        self.assertIs(result, row)
        self.assertEqual(row.Target, 100.0)
        self.assertEqual(row.Weight, 10.0)
        self.assertEqual(cells[columns.Param1].IntegerValue, 1)
        self.assertEqual(cells[columns.Param2].IntegerValue, 2)
        self.assertEqual(cells[columns.Param3].DoubleValue, 3.5)
        self.assertEqual(cells[columns.Param4].DoubleValue, 4.5)

    def test_normalized_field_coordinates(self):
        modes = SimpleNamespace(Radial=object(), Rectangular=object())
        fake_zp = SimpleNamespace(constants=SimpleNamespace(
            SystemData=SimpleNamespace(FieldNormalizationType=modes)
        ))
        cases = [
            (modes.Radial, [(0, 0), (3, 4), (-3, -4)], [(0, 0), (0.6, 0.8), (-0.6, -0.8)]),
            (modes.Rectangular, [(0, 0), (3, 4), (-6, -2)], [(0, 0), (0.5, 1), (-1, -0.5)]),
            (modes.Rectangular, [(0, 0), (0, -2)], [(0, 0), (0, -1)]),
            (modes.Radial, [(0, 0)], [(0, 0)]),
        ]
        with patch.dict(NAMESPACE, zp=fake_zp):
            for mode, points, expected in cases:
                fields = SimpleNamespace(
                    Normalization=mode, NumberOfFields=len(points),
                    GetField=lambda i: SimpleNamespace(X=points[i - 1][0], Y=points[i - 1][1]),
                )
                self.assertEqual(NAMESPACE["_get_normalized_field_coordinates"](fields), expected)
            fields.Normalization = object()
            with self.assertRaises(RuntimeError):
                NAMESPACE["_get_normalized_field_coordinates"](fields)

    def test_evaluation_operand_parameters(self):
        operands = SimpleNamespace(**{name: name for name in ("RWCE", "MTFA", "MTFT", "MTFS", "EFFL", "WFNO")})
        radial = object()
        fake_zp = SimpleNamespace(constants=SimpleNamespace(
            Editors=SimpleNamespace(MFE=SimpleNamespace(MeritOperandType=operands)),
            SystemData=SimpleNamespace(FieldNormalizationType=SimpleNamespace(Radial=radial, Rectangular=object())),
        ))
        points = [(0, 0), (3, 4)]
        fields = SimpleNamespace(
            NumberOfFields=2, Normalization=radial,
            GetField=lambda i: SimpleNamespace(X=points[i - 1][0], Y=points[i - 1][1]),
        )
        calls = []
        def operand_value(name, *params):
            calls.append((name, params))
            return {"RWCE": 0.01, "MTFA": 0.9, "MTFT": 0.7, "MTFS": 0.7, "EFFL": 100.0, "WFNO": 4.0}[name]
        oss = SimpleNamespace(
            Tools=with_tool(SpotTool()).Tools,
            MFE=SimpleNamespace(GetOperandValue=operand_value),
            SystemData=SimpleNamespace(Fields=fields, Wavelengths=SimpleNamespace(
                NumberOfWavelengths=3, GetWavelength=lambda i: SimpleNamespace(IsPrimary=i == 2)
            )),
        )
        evaluate = server_function(
            "evaluate_image_quality", zp=fake_zp, zm=SimpleNamespace(connect=lambda: oss), EvaluateRequest=object,
        )
        with patch.dict(NAMESPACE, zp=fake_zp):
            result = evaluate(SimpleNamespace(max_frequency=20.0, num_freq_points=2, mtf_sampling=4))
        self.assertEqual(result["status"], "ok")
        self.assertIn(("RWCE", (4, 2, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)), calls)
        self.assertIn(("RWCE", (4, 2, 0.6, 0.8, 0.0, 0.0, 0.0, 0.0)), calls)
        self.assertIn(("MTFA", (4, 0, 0.0, 20.0, 1.0, 0.0, 0.0, 0.0)), calls)
        self.assertIn(("MTFT", (4, 0, 2, 20.0, 0.0, 0.0, 0.0, 0.0)), calls)
        self.assertIn(("EFFL", (0, 2, 0, 0.0, 0.0, 0.0, 0.0, 0.0)), calls)
        self.assertTrue(result["evaluation_valid"])
        self.assertEqual(result["mtf_sampling"], 4)
        self.assertIsNone(result["mtf_sampling_converged"])
        json.dumps(result, allow_nan=False)

    def test_rms_invalid_or_empty_matrix_is_not_success(self):
        matrices = [
            SimpleNamespace(GetLength=lambda _: 0),
            *[SimpleNamespace(GetLength=lambda _: 1, GetValue=lambda *_, v=v: v)
              for v in (float("nan"), float("inf"), -0.01)],
        ]
        for matrix in matrices:
            with self.subTest(matrix=matrix):
                class InvalidSpotTool(SpotTool):
                    @property
                    def RMSSpotRadii(self):
                        return matrix
                tool = InvalidSpotTool()
                with self.assertRaises(RuntimeError):
                    NAMESPACE["_get_rms_spot_radii"](with_tool(tool))
                self.assertTrue(tool.closed)

    def test_evaluation_invalid_wavefront_and_nonfinite_values(self):
        operands = SimpleNamespace(**{name: name for name in ("RWCE", "MTFA", "MTFT", "MTFS", "EFFL", "WFNO")})
        fake_zp = SimpleNamespace(constants=SimpleNamespace(
            Editors=SimpleNamespace(MFE=SimpleNamespace(MeritOperandType=operands))
        ))
        fields = SimpleNamespace(NumberOfFields=1, GetField=lambda _: SimpleNamespace(Y=0.0))
        tool = SpotTool()
        values = {"RWCE": 0.01, "MTFA": 0.9, "MTFT": 0.7, "MTFS": 0.7, "EFFL": 100.0, "WFNO": 4.0}
        oss = SimpleNamespace(
            Tools=with_tool(tool).Tools, MFE=SimpleNamespace(GetOperandValue=lambda op, *_: values[op]),
            SystemData=SimpleNamespace(Fields=fields),
        )
        evaluate = server_function(
            "evaluate_image_quality", zp=fake_zp, zm=SimpleNamespace(connect=lambda: oss), EvaluateRequest=object,
            _get_primary_wavelength_index=lambda _: 1, _get_normalized_field_coordinates=lambda _: [(0.0, 0.0)],
        )
        req = SimpleNamespace(max_frequency=10.0, num_freq_points=1, mtf_sampling=3)
        for value in (0.0, -0.01, float("nan"), float("inf"), -float("inf"), 9e9):
            with self.subTest(rms=value):
                values["RWCE"] = value
                result = evaluate(req)
                self.assertEqual(result["status"], "error")
                wavefront = result["wavefront_summary"][0]
                self.assertFalse(wavefront["wavefront_valid"])
                self.assertFalse(wavefront["diffraction_limited"])
                self.assertIsNone(wavefront["strehl_ratio_approx"])
                json.dumps(result, allow_nan=False)
        values["RWCE"] = 0.0715
        self.assertFalse(evaluate(req)["wavefront_summary"][0]["diffraction_limited"])
        values["RWCE"] = 0.01
        for operand in ("MTFT", "MTFS", "MTFA", "EFFL", "WFNO"):
            for value in (float("nan"), float("inf"), 9e9):
                with self.subTest(operand=operand, value=value):
                    original = values[operand]
                    values[operand] = value
                    result = evaluate(req)
                    self.assertEqual(result["status"], "error")
                    self.assertFalse(result["evaluation_valid"])
                    json.dumps(result, allow_nan=False)
                    values[operand] = original
        tool.Succeeded = False
        result = evaluate(req)
        self.assertEqual(result["status"], "error")
        self.assertNotIn("wavefront_summary", result)
        self.assertTrue(tool.closed)

    def test_evaluation_request_bounds(self):
        from pydantic import BaseModel, Field, ValidationError
        model = next(node for node in TREE.body if isinstance(node, ast.ClassDef) and node.name == "EvaluateRequest")
        namespace = {"BaseModel": BaseModel, "Field": Field}
        exec(compile(ast.Module(body=[model], type_ignores=[]), "<evaluate-model>", "exec"), namespace)
        request = namespace["EvaluateRequest"]
        self.assertEqual(request().mtf_sampling, 3)
        for payload in ({"mtf_sampling": 0}, {"mtf_sampling": 5}, {"mtf_sampling": True},
                        {"mtf_sampling": 2.5}, {"num_freq_points": 0},
                        {"max_frequency": 0}, {"max_frequency": float("inf")}, {"max_frequency": float("nan")}):
            with self.subTest(payload=payload), self.assertRaises(ValidationError):
                request(**payload)

    def test_optimization_rebuilds_constraints_without_duplicate_rows(self):
        operands = SimpleNamespace(**{name: name for name in ("EFFL", "AXCL", "TOTR", "OPLT", "TTHI", "OPGT")})
        modes = SimpleNamespace(Automatic=object(), Fixed_10_Cycles=object())
        fake_zp = SimpleNamespace(constants=SimpleNamespace(
            Editors=SimpleNamespace(MFE=SimpleNamespace(MeritOperandType=operands)),
            Tools=SimpleNamespace(Optimization=SimpleNamespace(
                OptimizationAlgorithm=SimpleNamespace(DampedLeastSquares=object()), OptimizationCycles=modes,
            )),
        ))
        rows = ["old EFFL", "old AXCL", "DMFS", "old wizard rows"]
        class MeritFunction:
            SEQOptimizationWizard = SimpleNamespace(StartAt=0, IsAddFavoriteOperandsUsed=True)
            @property
            def NumberOfOperands(self):
                return len(rows)
            def RemoveOperandsAt(self, start, count):
                del rows[start - 1:start - 1 + count]
            def CalculateMeritFunction(self):
                return 0.2
            def GetOperandValue(self, operand, *_):
                return 100.0
        mfe = MeritFunction()
        def apply():
            self.assertEqual(rows, [])
            self.assertEqual(mfe.SEQOptimizationWizard.StartAt, 1)
            self.assertFalse(mfe.SEQOptimizationWizard.IsAddFavoriteOperandsUsed)
            rows.extend(["DMFS", "wizard rows"])
        mfe.SEQOptimizationWizard.Apply = apply
        def insert(mfe, index, operand, target, weight, int1=None, int2=None, data1=None, data2=None):
            rows.insert(index - 1, (operand, int1, int2))
        tool = SpotTool()
        oss = SimpleNamespace(
            MFE=mfe, LDE=SimpleNamespace(NumberOfSurfaces=4),
            SystemData=SimpleNamespace(Wavelengths=SimpleNamespace(NumberOfWavelengths=3)),
            Tools=SimpleNamespace(OpenLocalOptimization=lambda: tool),
        )
        optimize = server_function(
            "run_optimization", zp=fake_zp, zm=SimpleNamespace(connect=lambda: oss), OptimizeRequest=object,
            _get_primary_wavelength_index=lambda _: 2, _insert_operand_row=insert, _extract_system_state=lambda _: {},
        )
        req = SimpleNamespace(use_wavefront=False, target_effl=100.0, effl_weight=10.0,
                              minimize_axial_color=True, max_totr=150.0, min_bfl=50.0, custom_operands=[], cycles=0)
        expected = [("EFFL", None, 2), ("AXCL", 1, 3), ("TOTR", None, None),
                    ("OPLT", 3, None), ("TTHI", 2, 2), ("OPGT", 5, None), "DMFS", "wizard rows"]
        for _ in range(2):
            self.assertEqual(optimize(req)["status"], "ok")
            self.assertEqual(rows, expected)
            self.assertTrue(tool.closed)
        req.custom_operands = [SimpleNamespace(operand_type="UNKNOWN")]
        self.assertEqual(optimize(req)["status"], "error")
        self.assertEqual(rows, expected)

    def test_cli_evaluation_sampling_payload(self):
        import argparse
        from unittest.mock import Mock
        source = (ROOT / "zemax_cli.py").read_text(encoding="utf-8")
        node = next(node for node in ast.parse(source).body if isinstance(node, ast.FunctionDef) and node.name == "main")
        requests = Mock()
        requests.post.return_value.json.return_value = {"status": "ok"}
        namespace = {"argparse": argparse, "json": json, "requests": requests, "SERVER_URL": "http://127.0.0.1:8765"}
        exec(compile(ast.Module(body=[node], type_ignores=[]), "<cli-main>", "exec"), namespace)
        for args, expected in (([], 3), (["--mtf-sampling", "4"], 4)):
            with patch.object(sys, "argv", ["zemax_cli.py", "evaluate", "--max-freq", "20", "--points", "2", *args]), patch("builtins.print"):
                namespace["main"]()
            requests.post.assert_called_with(
                "http://127.0.0.1:8765/evaluate", json={"max_frequency": 20.0, "num_freq_points": 2, "mtf_sampling": expected},
            )

    def test_patch_half_aperture_automatic_and_fixed(self):
        for changes, automatic in (({"semi_diameter_auto": True}, True), ({"semi_diameter": 10.0}, False)):
            with self.subTest(changes=changes):
                route, req, surf, cell, editors = surface_patch_fixture(**changes)
                self.assertEqual(route(req)["status"], "ok")
                expected = editors.SolveType.Automatic if automatic else editors.SolveType.Fixed
                cell.IsSolveTypeSupported.assert_called_once_with(expected)
                cell.CreateSolveType.assert_called_once_with(expected)
                cell.SetSolveData.assert_called_once_with(cell.CreateSolveType.return_value)
                self.assertIs(cell.Solve, expected)
                self.assertEqual(surf.SemiDiameter, 12.5 if automatic else 10.0)

    def test_patch_half_aperture_failure_preserves_other_parameters(self):
        cases = ("conflict", "unsupported", "invalid", "missing", "status", "readback")
        for case in cases:
            with self.subTest(case=case):
                route, req, surf, cell, editors = surface_patch_fixture(semi_diameter_auto=True, radius=80.0)
                if case == "conflict":
                    req.semi_diameter = 10.0
                elif case == "unsupported":
                    cell.IsSolveTypeSupported.return_value = False
                elif case == "invalid":
                    cell.CreateSolveType.return_value.IsValid = False
                elif case == "missing":
                    cell.CreateSolveType.return_value = None
                elif case == "status":
                    cell.SetSolveData.side_effect = lambda _: object()
                elif case == "readback":
                    cell.Solve = object()
                    cell.SetSolveData.side_effect = lambda _: editors.SolveStatus.Success
                self.assertEqual(route(req)["status"], "error")
                self.assertEqual(surf.Radius, 100.0)
                if case in ("conflict", "unsupported", "invalid", "missing"):
                    cell.SetSolveData.assert_not_called()

    def test_patch_stop_move_and_reject_clear(self):
        route, req, surf, cell, _ = surface_patch_fixture(is_stop=True)
        self.assertEqual(route(req)["status"], "ok")
        self.assertEqual(surf.stop_writes, [True])
        req.is_stop, req.radius, req.semi_diameter_auto = False, 80.0, True
        self.assertEqual(route(req)["status"], "error")
        self.assertEqual(surf.stop_writes, [True])
        self.assertEqual(surf.Radius, 100.0)
        cell.SetSolveData.assert_not_called()
        surf.stop = False
        self.assertEqual(route(req)["status"], "ok")
        self.assertEqual(surf.stop_writes, [True])

    def test_patch_unsupported_stop_and_surface_bounds(self):
        for index in (0, 3, 4):
            route, req, surf, _, _ = surface_patch_fixture(surface_index=index, is_stop=True)
            self.assertEqual(route(req)["status"], "error")
            self.assertEqual(surf.stop_writes, [])
        route, req, surf, cell, _ = surface_patch_fixture(is_stop=True, semi_diameter_auto=True, radius=80.0)
        surf.TypeData.CanBeStop = False
        self.assertEqual(route(req)["status"], "error")
        self.assertEqual(surf.stop_writes, [])
        self.assertEqual(surf.Radius, 100.0)
        cell.SetSolveData.assert_not_called()

    def test_patch_request_half_aperture_bounds(self):
        from pydantic import BaseModel, Field, ValidationError
        from typing import Optional
        model = next(node for node in TREE.body if isinstance(node, ast.ClassDef) and node.name == "SurfacePatchRequest")
        namespace = {"BaseModel": BaseModel, "Field": Field, "Optional": Optional}
        exec(compile(ast.Module(body=[model], type_ignores=[]), "<patch-model>", "exec"), namespace)
        request = namespace["SurfacePatchRequest"]
        for value in (0, -1, float("nan"), float("inf")):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                request(surface_index=1, semi_diameter=value)
        for payload in ({"semi_diameter_auto": 1}, {"is_stop": 1}):
            with self.subTest(payload=payload), self.assertRaises(ValidationError):
                request(surface_index=1, **payload)
        self.assertTrue(request(surface_index=1, semi_diameter_auto=True).semi_diameter_auto)

    def test_load_and_build_do_not_create_analysis_windows(self):
        oss = SimpleNamespace(load=Mock(), Analyses=Mock())
        load = server_function(
            "load_zmx_file", FileRequest=object, zm=SimpleNamespace(connect=lambda: oss),
            os=SimpleNamespace(path=SimpleNamespace(abspath=lambda p: p, exists=lambda _: True)),
            _extract_system_state=lambda _: {},
        )
        self.assertEqual(load(SimpleNamespace(filepath="saved.zmx"))["status"], "ok")
        oss.load.assert_called_once_with("saved.zmx")
        oss.Analyses.New_Analysis.assert_not_called()
        for name in ("build_system", "load_zmx_file"):
            node = next(node for node in TREE.body if isinstance(node, ast.FunctionDef) and node.name == name)
            self.assertFalse(any(isinstance(child, ast.Attribute) and child.attr == "Analyses"
                                 for child in ast.walk(node)))

    def test_cli_patch_aperture_and_stop_payloads(self):
        import argparse
        source = (ROOT / "zemax_cli.py").read_text(encoding="utf-8")
        node = next(node for node in ast.parse(source).body if isinstance(node, ast.FunctionDef) and node.name == "main")
        requests = Mock()
        requests.post.return_value.json.return_value = {"status": "ok"}
        namespace = {"argparse": argparse, "json": json, "requests": requests, "SERVER_URL": "http://127.0.0.1:8765"}
        exec(compile(ast.Module(body=[node], type_ignores=[]), "<cli-main>", "exec"), namespace)
        cases = [([], {}), (["--semi-diameter", "10"], {"semi_diameter": 10.0}),
                 (["--semi-diameter-auto", "--stop"], {"semi_diameter_auto": True, "is_stop": True})]
        for flags, expected in cases:
            with patch.object(sys, "argv", ["zemax_cli.py", "patch", "--index", "2", *flags]), patch("builtins.print"):
                namespace["main"]()
            requests.post.assert_called_with(
                "http://127.0.0.1:8765/patch_surface", json={"surface_index": 2, **expected},
            )
        requests.post.reset_mock()
        with patch.object(sys, "argv", ["zemax_cli.py", "patch", "--index", "2", "--semi-diameter", "10", "--semi-diameter-auto"]), patch("sys.stderr"), self.assertRaises(SystemExit):
            namespace["main"]()
        requests.post.assert_not_called()

    def test_state_health_and_merit_sentinel(self):
        surface = SimpleNamespace(Radius=float("inf"), Thickness=float("inf"), Material="", SemiDiameter=12.5, Comment="", IsStop=False)
        oss = with_tool(SpotTool())
        oss.LDE = SimpleNamespace(NumberOfSurfaces=1, GetSurfaceAt=lambda _: surface)
        oss.MFE = SimpleNamespace(CalculateMeritFunction=lambda: 0.2)
        state = NAMESPACE["_extract_system_state"](oss)
        self.assertTrue(state["ray_trace_valid"])
        json.dumps(state, allow_nan=False)
        oss.MFE = SimpleNamespace(CalculateMeritFunction=lambda: 9e9)
        self.assertFalse(NAMESPACE["_extract_system_state"](oss)["ray_trace_valid"])


if __name__ == "__main__":
    unittest.main()
