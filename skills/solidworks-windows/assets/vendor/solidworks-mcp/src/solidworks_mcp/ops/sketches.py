"""
Sketch creation and 2D geometry.

The reference Python project exposed ``draw_line``, ``draw_circle``,
``draw_rectangle``, ``draw_arc`` and ``draw_polygon`` as five separate tools,
each a single COM call -- so drawing a 12-segment profile cost 12 round-trips.
The reference C# project had one entity tool plus a batch variant.

Here there is exactly one: :func:`add_entities` takes a list of typed entity
records and creates them all under a single ``AddToDB`` bracket. That both
removes the redundancy and makes multi-segment profiles a single call.

``AddToDB=True`` matters for accuracy: with it off, SolidWorks applies input
inference and snapping to programmatically-created geometry, which quietly skews
radii on shallow arcs and moves endpoints onto nearby entities.
"""

from __future__ import annotations

import logging

from ..com import compat, enums, selection
from ..com.compat import double_array, pm
from ..com.connection import SolidWorksError, SolidWorksSession
from ..util import units
from ..util.result import ToolError, one_of

logger = logging.getLogger(__name__)

ENTITY_TYPES = {
    "line", "centerline", "rectangle", "center_rectangle", "circle", "arc",
    "arc_center", "ellipse", "spline", "polygon", "slot", "point", "fillet",
    "chamfer",
}


# ---------------------------------------------------------------------------
# Sketch lifecycle
# ---------------------------------------------------------------------------

def create_sketch(
    session: SolidWorksSession,
    plane: str | None = None,
    face_index: int | None = None,
    face_point: list | None = None,
    unit: str | None = None,
    converter: units.UnitConverter | None = None,
) -> dict:
    """Open a new sketch on a reference plane or an existing model face.

    Face selection accepts an **index** (preferred) or a coordinate. The index
    comes from ``analyze_model(analysis_type="faces")`` and is robust where a
    coordinate pick is ambiguous -- revolve end-caps, coplanar neighbours, or any
    point where several faces nearly meet.

    Any already-active sketch is closed first, so a caller never has to track
    sketch mode manually.

    Args:
        plane: Plane name -- ``"Front Plane"``, ``"front"``, ``"XY"``, or a
            created plane like ``"Plane1"``. Localised default-plane names are
            resolved by tree ordinal, so this works on non-English installs.
        face_index: Face index from ``analyze_model(faces)``. Takes priority over
            ``face_point``.
        face_point: ``[x, y, z]`` of a point lying on the target planar face.
        unit: Unit for ``face_point``.
    """
    doc = session.require_doc("PART", "ASSEMBLY")
    converter = converter or units.UnitConverter(session.config.default_unit)

    manager = pm(doc, "SketchManager", default=None)
    if manager is None:
        raise SolidWorksError("NO_SKETCH_MANAGER", "The active document exposes no SketchManager.")

    # Close any open sketch so the new one is not nested.
    if pm(manager, "ActiveSketch", default=None) is not None:
        compat.invoke_any(manager, [("InsertSketch", (True,))])

    selection.clear(doc)
    target_description: str

    if face_index is not None:
        ok, error = selection.select_faces_by_index(doc, [int(face_index)])
        if not ok:
            raise ToolError(
                "FACE_NOT_FOUND", error,
                hint="Call analyze_model(analysis_type='faces') to list valid face indices.",
            )
        target_description = f"face index {face_index}"
    elif face_point is not None:
        if len(face_point) != 3:
            raise ToolError(
                "INVALID_PARAMETER",
                f"face_point needs exactly 3 coordinates, got {len(face_point)}.",
            )
        x, y, z = converter.points_to_meters(face_point, unit)
        if not selection.select_by_id(doc, "", enums.SEL_STRING_FACE, x, y, z):
            raise ToolError(
                "FACE_NOT_FOUND",
                f"No face found at or near {face_point} ({units.parse_unit(unit, converter.default).value}).",
                hint="Prefer face_index from analyze_model(faces) -- coordinate picks are "
                     "ambiguous where faces meet.",
            )
        target_description = f"face at {face_point}"
    else:
        if not plane:
            raise ToolError(
                "MISSING_PARAMETER",
                "Provide plane, face_index, or face_point.",
                hint="e.g. plane='Front Plane', or face_index=3 from analyze_model(faces).",
            )
        if not selection.select_plane(doc, plane):
            raise ToolError(
                "PLANE_NOT_FOUND",
                f"No plane named '{plane}' in this document.",
                hint="Use 'Front Plane', 'Top Plane', 'Right Plane', or a plane created by "
                     "add_reference_geometry. analyze_model(features) lists existing planes.",
            )
        target_description = plane

    report = compat.invoke_any(manager, [("InsertSketch", (True,))])
    if not report.ok:
        compat.invoke_any(doc, [("InsertSketch2", (True,))])

    sketch = pm(manager, "ActiveSketch", default=None)
    if sketch is None:
        raise SolidWorksError(
            "SKETCH_CREATE_FAILED",
            f"A sketch was requested on {target_description} but none became active. "
            f"The selected entity may not be a planar face.",
        )

    return {
        "sketch": compat.feature_name(sketch, default="Sketch"),
        "on": target_description,
        "plane": read_sketch_plane(sketch, session.config.precision),
    }


def edit_sketch(session: SolidWorksSession, sketch_name: str) -> dict:
    """Reopen an existing sketch for editing.

    Args:
        sketch_name: Exact sketch feature name, e.g. ``"Sketch1"``.
    """
    doc = session.require_doc("PART", "ASSEMBLY")
    manager = pm(doc, "SketchManager", default=None)

    if pm(manager, "ActiveSketch", default=None) is not None:
        compat.invoke_any(manager, [("InsertSketch", (True,))])

    selection.clear(doc)
    if not selection.select_by_id(doc, sketch_name, enums.SEL_STRING_SKETCH):
        available = [
            compat.feature_name(f) for f in compat.iter_features(doc)
            if compat.feature_type(f) == "ProfileFeature"
        ]
        raise ToolError(
            "SKETCH_NOT_FOUND",
            f"No sketch named '{sketch_name}'.",
            hint=f"Sketches in this document: {', '.join(available) or '(none)'}.",
            details={"available_sketches": available},
        )

    compat.invoke_any(doc, [("EditSketch", ())])
    sketch = pm(manager, "ActiveSketch", default=None)
    if sketch is None:
        raise SolidWorksError(
            "SKETCH_EDIT_FAILED",
            f"EditSketch ran but '{sketch_name}' did not become active. "
            f"It may not be a sketch feature.",
        )

    return {
        "sketch": compat.feature_name(sketch, default=sketch_name),
        "plane": read_sketch_plane(sketch, session.config.precision),
        "segments": len(compat.as_list(pm(sketch, "GetSketchSegments", default=None))),
    }


def exit_sketch(session: SolidWorksSession) -> dict:
    """Close the active sketch, if one is open.

    Idempotent -- reports ``was_open: false`` rather than failing when no sketch
    is active. Feature tools exit sketch mode themselves, so this is only needed
    to abandon a sketch without consuming it.
    """
    doc = session.require_doc("PART", "ASSEMBLY")
    manager = pm(doc, "SketchManager", default=None)
    sketch = pm(manager, "ActiveSketch", default=None) if manager else None

    if sketch is None:
        return {"was_open": False, "sketch": None}

    name = compat.feature_name(sketch, default="Sketch")
    report = compat.invoke_any(manager, [("InsertSketch", (True,))])
    if not report.ok:
        compat.invoke_any(doc, [("InsertSketch2", (True,))])
    return {"was_open": True, "sketch": name}


def read_sketch_plane(sketch, precision: int = 6) -> dict | None:
    """The sketch's plane frame: origin plus the local X and Y axis directions.

    Needed whenever a caller must place 2D coordinates into a sketch whose frame
    SolidWorks chose (a generated edge-flange profile sketch, for instance).
    """
    try:
        model_to_sketch = pm(sketch, "ModelToSketchTransform", default=None)
        if model_to_sketch is None:
            return None
        inverse = pm(model_to_sketch, "Inverse", default=None) or model_to_sketch
        data = compat.as_floats(pm(inverse, "ArrayData", default=None))
        if len(data) < 12:
            return None
        return {
            "x_axis": [round(v, precision) for v in data[0:3]],
            "y_axis": [round(v, precision) for v in data[3:6]],
            "normal": [round(v, precision) for v in data[6:9]],
            "origin": [round(v, precision) for v in data[9:12]],
        }
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Entity creation
# ---------------------------------------------------------------------------

def add_entities(
    session: SolidWorksSession,
    entities: list,
    unit: str | None = None,
    converter: units.UnitConverter | None = None,
) -> dict:
    """Create one or many sketch entities in the active sketch.

    Records are created in list order under one ``AddToDB`` bracket, which
    suppresses SolidWorks' input inference so coordinates and radii land exactly
    as given.

    Supported ``entity_type`` values and their fields (lengths in ``unit``,
    angles always in degrees):

    * ``line`` / ``centerline`` -- ``x1, y1, x2, y2``
    * ``rectangle`` -- ``x1, y1, x2, y2`` (opposite corners)
    * ``center_rectangle`` -- ``cx, cy, x1, y1`` (centre plus a corner)
    * ``circle`` -- ``cx, cy, radius``
    * ``arc`` -- ``x1, y1`` start, ``x2, y2`` end, ``xm, ym`` a point on the arc
    * ``arc_center`` -- ``cx, cy`` centre, ``x1, y1`` start, ``x2, y2`` end,
      ``direction`` +1 CCW / -1 CW. Radius-exact; prefer this for shallow arcs.
    * ``ellipse`` -- ``cx, cy``, ``x1, y1`` major-axis point, ``x2, y2`` minor
    * ``spline`` -- ``points``: ``[x1, y1, x2, y2, ...]``, at least 2 points
    * ``polygon`` -- ``cx, cy, radius, sides``, optional ``inscribed``
    * ``slot`` -- ``x1, y1, x2, y2`` centre-line ends, ``radius``
    * ``point`` -- ``x, y``
    * ``fillet`` -- ``vx, vy`` the vertex to round, ``radius``
    * ``chamfer`` -- ``vx, vy`` the vertex to cut, ``distance``

    Any record may set ``construction: true`` to convert the result to reference
    geometry (the correct way to create a revolve axis, for example).

    Args:
        entities: List of entity records as described above.
        unit: Unit for all coordinates in this call.

    Returns:
        Per-type counts, plus -- when a single entity was requested -- the real
        geometry read back from SolidWorks for in-band verification.
    """
    doc = session.require_doc("PART", "ASSEMBLY", "DRAWING")
    converter = converter or units.UnitConverter(session.config.default_unit)

    manager = pm(doc, "SketchManager", default=None)
    if manager is None or pm(manager, "ActiveSketch", default=None) is None:
        raise ToolError(
            "SKETCH_NOT_ACTIVE",
            "No sketch is open, so there is nothing to draw into.",
            hint="Call create_sketch (or edit_sketch) first.",
        )

    if not entities:
        raise ToolError("MISSING_PARAMETER", "entities must contain at least one record.")

    scale = units.TO_METERS[units.parse_unit(unit, converter.default)]

    previous_add_to_db = pm(manager, "AddToDB", default=None)
    try:
        manager.AddToDB = True
    except Exception:
        previous_add_to_db = None

    created: list[dict] = []
    counts: dict[str, int] = {}
    try:
        for index, record in enumerate(entities):
            if not isinstance(record, dict):
                raise ToolError(
                    "INVALID_PARAMETER",
                    f"entities[{index}] must be an object, got {type(record).__name__}.",
                )
            entity_type = one_of(record.get("entity_type"), f"entities[{index}].entity_type",
                                 ENTITY_TYPES)
            try:
                segments = _create_entity(manager, doc, entity_type, record, scale)
            except ToolError:
                raise
            except Exception as exc:
                raise SolidWorksError(
                    "SKETCH_ENTITY_FAILED",
                    f"entities[{index}] ({entity_type}) raised {type(exc).__name__}: {exc}. "
                    f"{len(created)} earlier entities were created and remain in the sketch.",
                    {"created_before_failure": len(created), "failed_index": index},
                ) from None

            if not segments:
                raise SolidWorksError(
                    "SKETCH_ENTITY_FAILED",
                    f"entities[{index}] ({entity_type}) returned no geometry. "
                    f"{len(created)} earlier entities were created and remain in the sketch. "
                    f"Sketch operations are not transactional.",
                    {"created_before_failure": len(created), "failed_index": index},
                )

            if record.get("construction"):
                for segment in segments:
                    try:
                        segment.ConstructionGeometry = True
                    except Exception:
                        pass

            counts[entity_type] = counts.get(entity_type, 0) + 1
            created.append({"entity_type": entity_type, "segments": len(segments),
                            "objects": segments})
    finally:
        if previous_add_to_db is not None:
            try:
                manager.AddToDB = previous_add_to_db
            except Exception:
                pass

    doc_precision = session.config.precision
    result = {
        "created": len(created),
        "segment_count": sum(item["segments"] for item in created),
        "counts": counts,
        "sketch": compat.feature_name(pm(manager, "ActiveSketch", default=None), default="Sketch"),
    }

    # Read the real geometry back for a single-entity call. This is the in-band
    # verification that catches a snapped or inferred coordinate immediately,
    # rather than several features later when the model is already wrong.
    if len(created) == 1 and created[0]["segments"] == 1:
        result["geometry"] = _read_segment(created[0]["objects"][0], doc_precision, scale)

    return result


def _create_entity(manager, doc, entity_type: str, record: dict, scale: float) -> list:
    """Dispatch one entity record to its SketchManager call. Returns segments."""

    def coordinate(key: str, default=None) -> float:
        value = record.get(key, default)
        if value is None:
            raise ToolError(
                "MISSING_PARAMETER",
                f"'{entity_type}' requires '{key}'.",
                hint=f"See the add_sketch_entities documentation for {entity_type} fields.",
            )
        return float(value) * scale

    def raw(key: str, default=None):
        return record.get(key, default)

    if entity_type == "line":
        return compat.as_list(manager.CreateLine(
            coordinate("x1"), coordinate("y1"), 0.0,
            coordinate("x2"), coordinate("y2"), 0.0))

    if entity_type == "centerline":
        return compat.as_list(manager.CreateCenterLine(
            coordinate("x1"), coordinate("y1"), 0.0,
            coordinate("x2"), coordinate("y2"), 0.0))

    if entity_type == "rectangle":
        return compat.as_list(manager.CreateCornerRectangle(
            coordinate("x1"), coordinate("y1"), 0.0,
            coordinate("x2"), coordinate("y2"), 0.0))

    if entity_type == "center_rectangle":
        return compat.as_list(manager.CreateCenterRectangle(
            coordinate("cx"), coordinate("cy"), 0.0,
            coordinate("x1"), coordinate("y1"), 0.0))

    if entity_type == "circle":
        radius = coordinate("radius")
        cx, cy = coordinate("cx", record.get("x")), coordinate("cy", record.get("y"))
        # CreateCircleByRadius is radius-exact; CreateCircle infers the radius
        # from a perimeter point and can round on older builds.
        report = compat.invoke_first(manager, [
            ("CreateCircleByRadius", (cx, cy, 0.0, radius)),
            ("CreateCircle", (cx, cy, 0.0, cx + radius, cy, 0.0)),
        ])
        return compat.as_list(report.value)

    if entity_type == "arc":
        return compat.as_list(manager.Create3PointArc(
            coordinate("x1"), coordinate("y1"), 0.0,
            coordinate("x2"), coordinate("y2"), 0.0,
            coordinate("xm"), coordinate("ym"), 0.0))

    if entity_type == "arc_center":
        direction = int(raw("direction", 1) or 1)
        return compat.as_list(manager.CreateArc(
            coordinate("cx"), coordinate("cy"), 0.0,
            coordinate("x1"), coordinate("y1"), 0.0,
            coordinate("x2"), coordinate("y2"), 0.0,
            1 if direction >= 0 else -1))

    if entity_type == "ellipse":
        return compat.as_list(manager.CreateEllipse(
            coordinate("cx"), coordinate("cy"), 0.0,
            coordinate("x1"), coordinate("y1"), 0.0,
            coordinate("x2"), coordinate("y2"), 0.0))

    if entity_type == "spline":
        points = raw("points")
        if not points or len(points) < 4:
            raise ToolError(
                "INVALID_PARAMETER",
                "'spline' requires points=[x1,y1,x2,y2,...] with at least 2 points.",
            )
        if len(points) % 2:
            raise ToolError(
                "INVALID_PARAMETER",
                f"'spline' points must be an even-length flat list, got {len(points)} values.",
            )
        # SolidWorks wants 3D triples even in a 2D sketch.
        triples: list[float] = []
        for i in range(0, len(points), 2):
            triples.extend([float(points[i]) * scale, float(points[i + 1]) * scale, 0.0])
        report = compat.invoke_first(manager, [
            ("CreateSpline", (double_array(triples),)),
            ("CreateSpline2", (double_array(triples), True)),
        ])
        return compat.as_list(report.value)

    if entity_type == "polygon":
        sides = int(raw("sides", 6) or 6)
        if not 3 <= sides <= 100:
            raise ToolError(
                "INVALID_PARAMETER", f"'polygon' sides must be 3-100, got {sides}."
            )
        cx, cy, radius = coordinate("cx"), coordinate("cy"), coordinate("radius")
        inscribed = bool(raw("inscribed", False))
        return compat.as_list(manager.CreatePolygon(
            cx, cy, 0.0, cx + radius, cy, 0.0, sides, inscribed))

    if entity_type == "slot":
        radius = coordinate("radius")
        report = compat.invoke_first(manager, [
            ("CreateSketchSlot", (
                0,   # swSketchSlotCreationType_e: straight slot from centre-to-centre
                0,   # swSketchSlotLengthType_e: centre-to-centre length
                radius * 2.0,
                coordinate("x1"), coordinate("y1"), 0.0,
                coordinate("x2"), coordinate("y2"), 0.0,
                0.0, 0.0, 0.0,
                1, False,
            )),
        ])
        if report.ok:
            return compat.as_list(report.value)
        # Older builds have no slot API: compose the same shape from two arcs
        # and two tangent lines so the tool still works.
        return _compose_slot(manager, coordinate("x1"), coordinate("y1"),
                             coordinate("x2"), coordinate("y2"), radius)

    if entity_type == "point":
        return compat.as_list(manager.CreatePoint(
            coordinate("x", record.get("cx")), coordinate("y", record.get("cy")), 0.0))

    if entity_type == "fillet":
        radius = coordinate("radius")
        selection.clear(doc)
        if not selection.select_by_id(
            doc, "", enums.SEL_STRING_SKETCHPOINT,
            coordinate("vx"), coordinate("vy"), 0.0
        ):
            raise ToolError(
                "VERTEX_NOT_FOUND",
                f"No sketch vertex found at ({record.get('vx')}, {record.get('vy')}).",
                hint="A sketch fillet needs the shared endpoint of two segments.",
            )
        report = compat.invoke_first(manager, [
            ("CreateFillet", (radius, 1)),   # 1 = keep constraints
            ("CreateFillet", (radius,)),
        ])
        return compat.as_list(report.value) or [True]

    if entity_type == "chamfer":
        distance = coordinate("distance")
        selection.clear(doc)
        if not selection.select_by_id(
            doc, "", enums.SEL_STRING_SKETCHPOINT,
            coordinate("vx"), coordinate("vy"), 0.0
        ):
            raise ToolError(
                "VERTEX_NOT_FOUND",
                f"No sketch vertex found at ({record.get('vx')}, {record.get('vy')}).",
            )
        report = compat.invoke_first(manager, [
            # swSketchChamferType_e 0 = distance-distance (equal)
            ("CreateChamfer", (0, distance, distance)),
            ("CreateChamfer", (0, distance, 0.0)),
        ])
        return compat.as_list(report.value) or [True]

    raise ToolError("INVALID_PARAMETER", f"Unhandled entity_type '{entity_type}'.")


def _compose_slot(manager, x1, y1, x2, y2, radius) -> list:
    """Build a straight slot from primitives, for builds lacking the slot API."""
    import math

    dx, dy = x2 - x1, y2 - y1
    length = math.hypot(dx, dy)
    if length == 0:
        raise ToolError(
            "INVALID_PARAMETER", "'slot' needs two distinct centre-line endpoints."
        )
    nx, ny = -dy / length * radius, dx / length * radius

    segments = []
    segments.extend(compat.as_list(manager.CreateLine(x1 + nx, y1 + ny, 0.0, x2 + nx, y2 + ny, 0.0)))
    segments.extend(compat.as_list(manager.CreateLine(x1 - nx, y1 - ny, 0.0, x2 - nx, y2 - ny, 0.0)))
    segments.extend(compat.as_list(manager.CreateArc(
        x2, y2, 0.0, x2 + nx, y2 + ny, 0.0, x2 - nx, y2 - ny, 0.0, 1)))
    segments.extend(compat.as_list(manager.CreateArc(
        x1, y1, 0.0, x1 - nx, y1 - ny, 0.0, x1 + nx, y1 + ny, 0.0, 1)))
    return segments


def _read_segment(segment, precision: int, scale: float) -> dict:
    """Read a created segment's real geometry back out of SolidWorks."""
    info: dict = {}
    try:
        type_code = int(pm(segment, "GetType", default=-1))
    except (TypeError, ValueError):
        type_code = -1

    # swSketchSegments_e: 0=LINE, 1=ARC, 2=ELLIPSE, 3=SPLINE, 4=TEXT, 5=PARABOLA
    kinds = {0: "line", 1: "arc", 2: "ellipse", 3: "spline", 4: "text", 5: "parabola"}
    info["kind"] = kinds.get(type_code, "unknown")
    info["construction"] = bool(pm(segment, "ConstructionGeometry", default=False))

    length = pm(segment, "GetLength", default=None)
    if length is not None:
        try:
            info["length_m"] = round(float(length), precision)
        except (TypeError, ValueError):
            pass

    if type_code == 0:
        start = pm(segment, "GetStartPoint2", default=None)
        end = pm(segment, "GetEndPoint2", default=None)
        if start is not None and end is not None:
            info["start"] = [round(pm(start, "X", default=0.0), precision),
                             round(pm(start, "Y", default=0.0), precision)]
            info["end"] = [round(pm(end, "X", default=0.0), precision),
                           round(pm(end, "Y", default=0.0), precision)]
    elif type_code == 1:
        centre = pm(segment, "GetCenterPoint2", default=None)
        if centre is not None:
            info["center"] = [round(pm(centre, "X", default=0.0), precision),
                              round(pm(centre, "Y", default=0.0), precision)]
        radius = pm(segment, "GetRadius", default=None)
        if radius is not None:
            try:
                info["radius_m"] = round(float(radius), precision)
            except (TypeError, ValueError):
                pass

    return info


# ---------------------------------------------------------------------------
# Constraints and dimensions
# ---------------------------------------------------------------------------

def add_constraint(
    session: SolidWorksSession,
    constraint_type: str,
    points: list,
    entity_types: list | None = None,
    unit: str | None = None,
    converter: units.UnitConverter | None = None,
) -> dict:
    """Apply a geometric relation to entities in the active sketch.

    Entities are picked by a point lying on each one -- the midpoint of a line,
    a point on a circle's perimeter.

    Args:
        constraint_type: ``horizontal``, ``vertical``, ``fix`` (one entity);
            ``coincident``, ``parallel``, ``perpendicular``, ``tangent``,
            ``equal``, ``concentric``, ``collinear``, ``midpoint`` (two);
            ``symmetric`` (three -- two entities plus the symmetry line).
        points: ``[[x1, y1], [x2, y2], ...]``, one entry per entity the relation
            needs.
        entity_types: Per-entity selection type, ``"SKETCHSEGMENT"`` (default) or
            ``"SKETCHPOINT"``. Point-based relations such as ``midpoint`` and
            ``coincident`` between endpoints need ``SKETCHPOINT``.
        unit: Unit for the coordinates.
    """
    doc = session.require_doc("PART", "ASSEMBLY", "DRAWING")
    converter = converter or units.UnitConverter(session.config.default_unit)

    manager = pm(doc, "SketchManager", default=None)
    if manager is None or pm(manager, "ActiveSketch", default=None) is None:
        raise ToolError(
            "SKETCH_NOT_ACTIVE", "No sketch is open.", hint="Call create_sketch first."
        )

    kind = one_of(constraint_type, "constraint_type", set(enums.SKETCH_CONSTRAINT_BY_NAME))
    token, required = enums.SKETCH_CONSTRAINT_BY_NAME[kind]

    if len(points) < required:
        raise ToolError(
            "MISSING_PARAMETER",
            f"'{kind}' needs {required} entity point(s), got {len(points)}.",
            hint=f"points=[[x,y], ...] with {required} entries.",
        )

    scale = units.TO_METERS[units.parse_unit(unit, converter.default)]
    types = list(entity_types or [])

    selection.clear(doc)
    for index, point in enumerate(points[:required]):
        if len(point) < 2:
            raise ToolError("INVALID_PARAMETER", f"points[{index}] needs an x and a y.")
        selection_type = (types[index] if index < len(types) else None) or enums.SEL_STRING_SKETCHSEGMENT
        selection_type = str(selection_type).upper()
        if not selection.select_by_id(
            doc, "", selection_type,
            float(point[0]) * scale, float(point[1]) * scale, 0.0,
            append=index > 0,
        ):
            raise ToolError(
                "ENTITY_NOT_FOUND",
                f"No {selection_type} found at or near points[{index}] = {list(point)}.",
                hint="The point must lie on the entity -- use a line's midpoint or a point "
                     "on a circle's perimeter. For endpoint relations pass "
                     "entity_types=['SKETCHPOINT', ...].",
            )

    report = compat.invoke_any(doc, [("SketchAddConstraints", (token,))])
    if not report.ok:
        raise SolidWorksError(
            "CONSTRAINT_FAILED",
            f"SketchAddConstraints('{token}') failed. The selected entities may not accept "
            f"a {kind} relation.",
            {"attempts": report.diagnostic()},
        )

    selection.clear(doc)
    return {
        "constraint": kind,
        "solidworks_token": token,
        "entities": required,
        "fully_defined": _sketch_status(manager),
    }


def add_dimension(
    session: SolidWorksSession,
    point: list,
    value: float,
    label_offset: list | None = None,
    unit: str | None = None,
    is_angle: bool = False,
    converter: units.UnitConverter | None = None,
) -> dict:
    """Apply a driving dimension to the sketch segment nearest ``point``.

    Works in both parts and drawings -- the reference C# project needed two
    separate tools for that.

    Args:
        point: ``[x, y]`` on the target segment (a line's midpoint works well).
            Sheet coordinates when the active document is a drawing.
        value: The value to drive the dimension to, in ``unit`` (or degrees when
            ``is_angle`` is set).
        label_offset: ``[dx, dy]`` offset for the dimension text. Defaults to
            15 mm below the pick point so labels do not stack.
        unit: Unit for coordinates and for ``value``.
        is_angle: Treat ``value`` as degrees rather than a length.
    """
    doc = session.active_doc()
    converter = converter or units.UnitConverter(session.config.default_unit)
    resolved_unit = units.parse_unit(unit, converter.default)
    scale = units.TO_METERS[resolved_unit]

    if len(point) < 2:
        raise ToolError("INVALID_PARAMETER", "point needs an x and a y.")

    is_drawing = session.doc_type(doc) == "DRAWING"
    manager = pm(doc, "SketchManager", default=None)
    if not is_drawing and (manager is None or pm(manager, "ActiveSketch", default=None) is None):
        raise ToolError(
            "SKETCH_NOT_ACTIVE", "No sketch is open.", hint="Call create_sketch first."
        )

    px, py = float(point[0]) * scale, float(point[1]) * scale
    offset = label_offset or [0.0, -15.0]
    ox, oy = float(offset[0]) * scale, float(offset[1]) * scale

    selection.clear(doc)
    # Commit pending sketch geometry: segments created moments ago are not always
    # queryable by SelectByID2 until the view refreshes.
    compat.invoke_any(doc, [("GraphicsRedraw2", ()), ("GraphicsRedraw", ())])

    # A drawing view's projected model geometry is EDGE-typed, not a sketch
    # segment -- SKETCHSEGMENT only exists for an actual sketch open on the
    # sheet. Verified live: SelectByID2 with SKETCHSEGMENT never found a
    # drawing view's rectangle/circle edges at all, always reporting
    # SEGMENT_NOT_FOUND regardless of how accurate the point was.
    select_kind = "EDGE" if is_drawing else enums.SEL_STRING_SKETCHSEGMENT
    if not selection.select_by_id(doc, "", select_kind, px, py, 0.0):
        raise ToolError(
            "SEGMENT_NOT_FOUND",
            f"No sketch segment found at or near {list(point)} {resolved_unit.value}.",
            hint="The point must lie on the segment. For model dimensions in a drawing, "
                 "annotate_drawing(action='model_dimensions') is far more reliable.",
        )

    display = pm(doc, "AddDimension2", px + ox, py + oy, 0.0, default=None)
    if display is None:
        raise SolidWorksError(
            "DIMENSION_FAILED",
            "AddDimension2 returned null. The selected entity may not be dimensionable.",
        )

    dimension = pm(display, "GetDimension2", 0, default=None) or pm(display, "GetDimension",
                                                                   default=None)
    if dimension is None:
        raise SolidWorksError(
            "DIMENSION_FAILED", "The dimension was created but its value could not be set."
        )

    si_value = units.deg_to_rad(value) if is_angle else float(value) * scale
    try:
        dimension.SystemValue = si_value
    except Exception as exc:
        raise SolidWorksError(
            "DIMENSION_FAILED", f"Could not drive the dimension to {value}: {exc}"
        ) from None

    # Read the sketch's constrained status BEFORE rebuilding: EditRebuild3 exits
    # sketch mode on this SolidWorks build, so ActiveSketch is already None by the
    # time a post-rebuild read would run -- which is why this reported `null` for
    # every call until the ordering was fixed.
    fully_defined = _sketch_status(manager) if manager else None

    compat.invoke_any(doc, [("EditRebuild3", ())])

    effective = compat.dimension_value(dimension, default=si_value)
    return {
        "dimension": pm(dimension, "FullName", default=None) or pm(dimension, "Name", default=None),
        "requested": {"value": value, "unit": "deg" if is_angle else resolved_unit.value},
        "effective": (
            round(units.rad_to_deg(effective), 6) if is_angle
            else converter.describe(effective, resolved_unit)
        ),
        "fully_defined": fully_defined,
    }


def _sketch_status(manager) -> str | None:
    """Whether the active sketch is under-, fully- or over-defined.

    Surfaced after every constraint and dimension so a caller can tell when a
    profile is ready to extrude -- something neither reference project reported.
    """
    sketch = pm(manager, "ActiveSketch", default=None)
    if sketch is None:
        return None
    status = pm(sketch, "GetConstrainedStatus", default=None)
    return compat.sketch_constraint_status(status)
