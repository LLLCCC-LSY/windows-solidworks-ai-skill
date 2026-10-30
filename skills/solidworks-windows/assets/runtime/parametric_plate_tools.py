"""Native dimension/equation based plate recipe; no arbitrary scripting tool."""

from __future__ import annotations

import json
import math
import os
import re

import pythoncom

from solidworks_mcp.com import compat, constants, selection
from solidworks_mcp.util import result as R

PARAMETER_NAMES = ("L", "W", "T", "D", "E")
NATIVE_PLATES = []  # (IUnknown identity, native dimension bindings); STA thread only.


def validate_parameters(length_mm, width_mm, thickness_mm, hole_diameter_mm, edge_distance_mm):
    values = dict(zip(PARAMETER_NAMES, map(float, (length_mm, width_mm, thickness_mm, hole_diameter_mm, edge_distance_mm))))
    if not all(math.isfinite(value) and value > 0 for value in values.values()):
        raise ValueError("All five dimensions must be finite and positive.")
    if values["E"] <= values["D"] / 2:
        raise ValueError("The hole must remain inside the outer boundary: E > D/2.")
    if min(values["L"], values["W"]) <= 2 * values["E"] + values["D"]:
        raise ValueError("Opposite holes must remain separate: L,W > 2E+D.")
    return values


def suspend_dimension_input(session):
    index = constants.table().get("swInputDimValOnCreate")
    if index is None:
        raise RuntimeError("Cannot verify the native dimension-input preference constant.")
    original = bool(session.app.GetUserPreferenceToggle(index))
    session.app.SetUserPreferenceToggle(index, False)
    if session.app.GetUserPreferenceToggle(index):
        raise RuntimeError("Could not suppress the dimension-input dialog.")
    return index, original


def restore_dimension_input(session, token):
    index, original = token
    session.app.SetUserPreferenceToggle(index, original)
    if bool(session.app.GetUserPreferenceToggle(index)) != original:
        raise RuntimeError("The original dimension-input preference could not be restored.")


def equations(manager):
    count = int(compat.pm(manager, "GetCount"))
    member = manager._oleobj_.GetIDsOfNames("Equation")
    return [str(manager._oleobj_.Invoke(member, 0, pythoncom.DISPATCH_PROPERTYGET, True, index)) for index in range(count)]


def _add_equation(manager, expression):
    index = int(manager.Add2(-1, expression, True))
    if index < 0:
        raise RuntimeError("SolidWorks rejected equation: " + expression)
    return index


def _put_equation(manager, index, expression):
    member = manager._oleobj_.GetIDsOfNames("Equation")
    manager._oleobj_.Invoke(member, 0, pythoncom.DISPATCH_PROPERTYPUT, False, index, expression)


def _parameters_from_equations(texts):
    result = {}
    indices = {}
    for index, text in enumerate(texts):
        match = re.fullmatch(r'\s*"([LWTDE])"\s*=\s*([-+0-9.eE]+)\s*mm\s*', text)
        if match:
            result[match[1]] = float(match[2])
            indices[match[1]] = index
    if set(result) != set(PARAMETER_NAMES):
        raise ValueError("The active part does not contain this recipe's five numeric mm variables.")
    return result, indices


def _select(doc, objects):
    selection.clear(doc)
    for index, obj in enumerate(objects):
        if not selection.select_entity(obj, append=index > 0, doc=doc):
            raise RuntimeError("Could not select a sketch entity for its native dimension.")


def _dimension(doc, objects, method, location, value):
    _select(doc, objects)
    display = getattr(doc, method)(*location)
    if display is None:
        raise RuntimeError(method + " returned no dimension.")
    dimension = compat.read_dimension(display)
    if dimension is None:
        raise RuntimeError("Cannot read the new native dimension.")
    dimension.SystemValue = float(value) / 1000
    native_name = str(compat.pm(dimension, "FullName"))
    short_name = "@".join(native_name.split("@")[:2])
    selection.clear(doc)
    return short_name


def define_plate_sketch(session, parameters, use_equations=True):
    doc = session.active_doc()
    manager = compat.pm(doc, "SketchManager")
    sketch = compat.pm(manager, "ActiveSketch")
    if sketch is None:
        raise ValueError("The plate profile sketch must be active.")
    segments = compat.as_list(compat.pm(sketch, "GetSketchSegments"))
    lines = [segment for segment in segments if compat.pm(segment, "GetType") == 0]
    circles = [segment for segment in segments if compat.pm(segment, "GetType") == 1]
    if len(lines) != 4 or len(circles) != 4:
        raise ValueError("This recipe requires exactly four rectangle edges and four circles.")
    endpoints = [compat.pm(line, accessor) for line in lines for accessor in ("GetStartPoint2", "GetEndPoint2")]

    def xy(point):
        return round(float(compat.pm(point, "X")), 8), round(float(compat.pm(point, "Y")), 8)

    points = {xy(point): point for point in endpoints}
    length, width = parameters["L"] / 1000, parameters["W"] / 1000
    origin = points[(0.0, 0.0)]
    lower_right = points[(round(length, 8), 0.0)]
    upper_left = points[(0.0, round(width, 8))]
    upper_right = points[(round(length, 8), round(width, 8))]
    bindings = {name: [] for name in PARAMETER_NAMES}
    token = suspend_dimension_input(session)
    try:
        # Anchor only one corner; all plate sizes and hole coordinates remain driven.
        _select(doc, [origin])
        doc.SketchAddConstraints("sgFIXED")
        selection.clear(doc)
        eq = compat.pm(doc, "GetEquationMgr")
        if use_equations:
            for name in PARAMETER_NAMES:
                _add_equation(eq, f'"{name}" = {parameters[name]:.12g}mm')
        dimension_equations = []
        length_name = _dimension(doc, [origin, lower_right], "AddHorizontalDimension2", (length / 2, -0.012, 0.0), parameters["L"])
        dimension_equations.append((length_name, '"L"'))
        bindings["L"].append(length_name)
        width_name = _dimension(doc, [origin, upper_left], "AddVerticalDimension2", (-0.012, width / 2, 0.0), parameters["W"])
        dimension_equations.append((width_name, '"W"'))
        bindings["W"].append(width_name)
        for circle in circles:
            center = compat.pm(circle, "GetCenterPoint2")
            x, y = xy(center)
            x_expression = '"E"' if x < length / 2 else '"L"-"E"'
            y_expression = '"E"' if y < width / 2 else '"W"-"E"'
            corner = origin if use_equations else (origin if x < length / 2 and y < width / 2 else
                     lower_right if y < width / 2 else upper_left if x < length / 2 else upper_right)
            corner_x, corner_y = xy(corner)
            x_name = _dimension(doc, [corner, center], "AddHorizontalDimension2", ((x+corner_x) / 2, y - 0.007, 0.0), x * 1000 if use_equations else parameters["E"])
            y_name = _dimension(doc, [corner, center], "AddVerticalDimension2", (x + 0.007, (y+corner_y) / 2, 0.0), y * 1000 if use_equations else parameters["E"])
            diameter_name = _dimension(doc, [circle], "AddDiameterDimension2", (x + 0.006, y + 0.006, 0.0), parameters["D"])
            dimension_equations.extend(((x_name, x_expression), (y_name, y_expression), (diameter_name, '"D"')))
            bindings["E"].extend((x_name, y_name))
            bindings["D"].append(diameter_name)
        if use_equations:
            for name, expression in dimension_equations:
                _add_equation(eq, f'"{name}" = {expression}')
            compat.pm(eq, "EvaluateAll")
        status = compat.sketch_constraint_status(compat.pm(sketch, "GetConstrainedStatus"))
        if status != "fully_defined":
            raise RuntimeError("The dimensioned profile is " + str(status))
        return {"sketch": compat.feature_name(sketch), "sketch_status": status,
                "parameters_mm": parameters, "sketch_dimensions": len(dimension_equations),
                "equations": equations(eq), "native_dimension_bindings": bindings,
                "global_equations_enabled": use_equations}
    finally:
        selection.clear(doc)
        restore_dimension_input(session, token)


def link_thickness(session, feature_name, use_equations=True):
    doc = session.active_doc()
    feature = compat.find_feature(doc, feature_name)
    dimensions = list(compat.iter_display_dimensions(feature))
    if len(dimensions) != 1:
        raise RuntimeError("Expected exactly one extrusion thickness dimension.")
    dimension = compat.read_dimension(dimensions[0])
    short_name = "@".join(str(compat.pm(dimension, "FullName")).split("@")[:2])
    eq = compat.pm(doc, "GetEquationMgr")
    if use_equations:
        _add_equation(eq, f'"{short_name}" = "T"')
        compat.pm(eq, "EvaluateAll")
    compat.pm(doc, "EditRebuild3")
    return short_name


def register(mcp, upstream, require_managed, remember, lock):
    @mcp.tool()
    def create_parametric_plate(length_mm: float = 120, width_mm: float = 80,
                                thickness_mm: float = 10, hole_diameter_mm: float = 8,
                                edge_distance_mm: float = 15, use_equations: bool = False) -> str:
        """Create a fully dimensioned four-hole plate with five numeric input parameters.

        Defaults to native corner-relative dimensions; the tool synchronizes the
        repeated diameter and edge-margin dimensions. Optional use_equations=True
        instead uses native global L,W,T,D,E, requiring a working VBA/equation engine.
        Anchors only one origin point,
        and restores the user's dimension-popup preference. Does not save files.
        Stops on failure; partial geometry remains visible for diagnosis.
        """
        stage = "validate_parameters"
        with lock:
            try:
                params = validate_parameters(length_mm, width_mm, thickness_mm, hole_diameter_mm, edge_distance_mm)
                if use_equations and os.environ.get("SOLIDWORKS_ENABLE_EQUATIONS") != "true":
                    raise ValueError("Global-equation mode is disabled pending VBA 7.1 repair and verification. Use native dimensions (use_equations=False).")

                def checked(tool, arguments):
                    response = json.loads(getattr(upstream, tool)(**arguments))
                    if not response["ok"]:
                        raise RuntimeError(json.dumps(response, ensure_ascii=False))
                    return response["data"]

                stage = "new_document"
                checked(stage, {})
                upstream.SESSION.com.call(remember, True)
                stage = "create_sketch"
                checked(stage, {"plane": "Front Plane", "unit": "mm"})
                stage = "add_sketch_entities"
                entities = [{"entity_type": "rectangle", "x1": 0, "y1": 0, "x2": params["L"], "y2": params["W"]}]
                for x, y in ((params["E"], params["E"]), (params["L"]-params["E"], params["E"]),
                             (params["E"], params["W"]-params["E"]), (params["L"]-params["E"], params["W"]-params["E"])):
                    entities.append({"entity_type": "circle", "cx": x, "cy": y, "radius": params["D"]/2})
                checked(stage, {"entities": entities, "unit": "mm"})
                stage = "define_plate_sketch"
                defined = upstream.SESSION.com.call(define_plate_sketch, upstream.SESSION, params, use_equations)
                stage = "extrude_feature"
                boss = checked(stage, {"feature_type": "boss", "depth": params["T"], "unit": "mm"})
                stage = "link_thickness"
                thickness_name = upstream.SESSION.com.call(link_thickness, upstream.SESSION, boss["feature"], use_equations)
                defined["native_dimension_bindings"]["T"] = [thickness_name]
                if not use_equations:
                    def remember_native():
                        doc = require_managed()
                        NATIVE_PLATES.append((doc._oleobj_.QueryInterface(pythoncom.IID_IUnknown), defined["native_dimension_bindings"]))
                    upstream.SESSION.com.call(remember_native)
                stage = "verify_model"
                summary = checked("analyze_model", {"analysis_type": "summary", "unit": "mm"})
                if summary["feature_errors"] != 0:
                    raise RuntimeError("The model has feature errors.")
                return R.ok("create_parametric_plate", "Created a fully dimensioned plate with five numeric inputs.",
                            {**defined, "thickness_dimension": thickness_name, "summary": summary})
            except Exception as exc:
                return R.from_exception("create_parametric_plate", R.ToolError(
                    "PARAMETRIC_PLATE_FAILED", str(exc), hint="Stop and inspect the partial part before retrying.",
                    details={"stage": stage}))

    @mcp.tool()
    def set_plate_parameters(length_mm: float | None = None, width_mm: float | None = None,
                             thickness_mm: float | None = None, hole_diameter_mm: float | None = None,
                             edge_distance_mm: float | None = None) -> str:
        """Update five parameters on a plate created by this session; rebuild and read back.

        Only numeric mm values are accepted. Invalid/touching/overlapping holes
        are rejected before editing. Save the resulting version to a new file.
        """
        with lock:
            try:
                upstream.SESSION.ensure(launch=False)

                def update():
                    doc = require_managed()
                    identity = doc._oleobj_.QueryInterface(pythoncom.IID_IUnknown)
                    native_bindings = next((bindings for known, bindings in NATIVE_PLATES if known == identity), None)
                    if native_bindings:
                        def read_native():
                            result = {}
                            for name, names in native_bindings.items():
                                values = [float(compat.dimension_value(compat.pm(doc, "Parameter", dimension))) * 1000 for dimension in names]
                                if not values or max(values) - min(values) > 1e-5:
                                    raise ValueError("Native dimensions are inconsistent for parameter " + name)
                                result[name] = values[0]
                            return result
                        before = read_native()
                        candidates = {name: value if value is not None else before[name] for name, value in zip(PARAMETER_NAMES, (length_mm, width_mm, thickness_mm, hole_diameter_mm, edge_distance_mm))}
                        values = validate_parameters(*(candidates[name] for name in PARAMETER_NAMES))
                        try:
                            for name in PARAMETER_NAMES:
                                if abs(values[name] - before[name]) > 1e-8:
                                    for dimension in native_bindings[name]:
                                        compat.pm(doc, "Parameter", dimension).SystemValue = values[name] / 1000
                            compat.pm(doc, "EditRebuild3")
                            effective = read_native()
                            if compat.error_count(doc) != 0 or any(abs(effective[name] - values[name]) > 1e-5 for name in PARAMETER_NAMES):
                                raise RuntimeError("Native dimension or rebuild verification failed.")
                            return {"before_mm": before, "parameters_mm": effective, "feature_errors": 0, "global_equations_enabled": False}
                        except Exception:
                            for name, names in native_bindings.items():
                                for dimension in names:
                                    compat.pm(doc, "Parameter", dimension).SystemValue = before[name] / 1000
                            compat.pm(doc, "EditRebuild3")
                            raise
                    eq = compat.pm(doc, "GetEquationMgr")
                    originals = equations(eq)
                    before, indices = _parameters_from_equations(originals)
                    candidates = dict(before)
                    for name, value in zip(PARAMETER_NAMES, (length_mm, width_mm, thickness_mm, hole_diameter_mm, edge_distance_mm)):
                        if value is not None:
                            candidates[name] = value
                    values = validate_parameters(*(candidates[name] for name in PARAMETER_NAMES))
                    try:
                        for name in PARAMETER_NAMES:
                            if values[name] != before[name]:
                                _put_equation(eq, indices[name], f'"{name}" = {values[name]:.12g}mm')
                        compat.pm(eq, "EvaluateAll")
                        compat.pm(doc, "EditRebuild3")
                        effective, _ = _parameters_from_equations(equations(eq))
                        if effective != values or compat.error_count(doc) != 0:
                            raise RuntimeError("Equation readback or rebuild validation failed.")
                        return {"before_mm": before, "parameters_mm": effective, "feature_errors": 0}
                    except Exception:
                        for name in PARAMETER_NAMES:
                            _put_equation(eq, indices[name], originals[indices[name]])
                        compat.pm(eq, "EvaluateAll")
                        compat.pm(doc, "EditRebuild3")
                        raise

                data = upstream.SESSION.com.call(update)
                message = ("Updated native driving dimensions and rebuilt the same part."
                           if data.get("global_equations_enabled") is False
                           else "Updated native global variables and rebuilt the same part.")
                return R.ok("set_plate_parameters", message, data)
            except Exception as exc:
                return R.from_exception("set_plate_parameters", exc)
