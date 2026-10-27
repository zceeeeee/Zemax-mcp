"""Offline checks: extract helpers without importing ZOSPy or connecting to OpticStudio."""
import ast
import json
import math
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch


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
            MFE=SimpleNamespace(GetOperandValue=operand_value),
            SystemData=SimpleNamespace(Fields=fields, Wavelengths=SimpleNamespace(
                NumberOfWavelengths=3, GetWavelength=lambda i: SimpleNamespace(IsPrimary=i == 2)
            )),
        )
        route = next(node for node in ast.parse(SOURCE).body
                     if isinstance(node, ast.FunctionDef) and node.name == "evaluate_image_quality")
        route.decorator_list = []
        namespace = dict(NAMESPACE, zp=fake_zp, zm=SimpleNamespace(connect=lambda: oss), EvaluateRequest=object)
        with patch.dict(NAMESPACE, zp=fake_zp):
            exec(compile(ast.Module(body=[route], type_ignores=[]), "<evaluate-route>", "exec"), namespace)
            result = namespace["evaluate_image_quality"](SimpleNamespace(max_frequency=20.0, num_freq_points=2))
        self.assertEqual(result["status"], "ok")
        self.assertIn(("RWCE", (4, 2, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)), calls)
        self.assertIn(("RWCE", (4, 2, 0.6, 0.8, 0.0, 0.0, 0.0, 0.0)), calls)
        self.assertIn(("MTFA", (2, 0, 0.0, 20.0, 1.0, 0.0, 0.0, 0.0)), calls)
        self.assertIn(("MTFT", (2, 0, 2, 20.0, 0.0, 0.0, 0.0, 0.0)), calls)
        self.assertIn(("EFFL", (0, 2, 0, 0.0, 0.0, 0.0, 0.0, 0.0)), calls)
        json.dumps(result, allow_nan=False)

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
