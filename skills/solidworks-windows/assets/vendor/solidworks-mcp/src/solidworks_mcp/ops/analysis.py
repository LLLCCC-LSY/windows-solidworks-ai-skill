"""
Read-only model analysis.

These tools never modify geometry, and they are the source of the **stable indices**
every selection-based feature tool consumes. The enumerations here and in
``com/selection.py`` are the same code, so an index reported by ``analyze_model``
always resolves to the same entity when passed back in.

``get_selection`` closes the loop the other way: it maps whatever the user clicked
in the SolidWorks window back onto those same indices, so "fillet this edge" works
from a GUI pick.
"""

from __future__ import annotations

import logging
import math

from ..com import compat, selection
from ..com.compat import byref_int, pm
from ..com.connection import SolidWorksError, SolidWorksSession
from ..util import units
from ..util.result import ToolError, one_of, require

logger = logging.getLogger(__name__)

ANALYSIS_TYPES = {
    "summary", "mass_properties", "geometry", "bodies", "faces", "edges",
    "features", "sketch", "configurations",
}


def analyze_model(
    session: SolidWorksSession,
    analysis_type: str = "summary",
    *,
    name: str | None = None,
    near: list | None = None,
    limit: int | None = None,
    axis: list | None = None,
    unit: str | None = None,
    converter: units.UnitConverter | None = None,
) -> dict:
    """Inspect the active part without modifying it.

    Args:
        analysis_type:
            * ``summary`` (default) -- topology counts, volume, mass, bounding box
              and material in one cheap call. Start here.
            * ``mass_properties`` -- volume, surface area, mass, centre of gravity.
            * ``geometry`` -- body, face, edge and vertex counts.
            * ``bodies`` -- per-body counts, volume, area and centroid.
            * ``faces`` -- every solid face with its stable index ``i``; planar faces
              also carry a normal, area and a representative point. Feed ``i`` to
              ``create_sketch(face_index=...)`` or
              ``extrude_feature(up_to_face_index=...)``.
            * ``edges`` -- every solid edge with its stable index ``i``, endpoints,
              midpoint and length. Feed ``i`` to ``add_edge_feature(edge_indices=...)``.
            * ``features`` -- the feature tree in history order with each feature's
              type, suppression state and driving dimensions (names plus values),
              a per-sketch summary, plus equations. This is the "understand the
              part" read, and the source of the dimension names
              ``modify_dimension`` needs.
            * ``sketch`` -- one named sketch's full segment geometry. Requires ``name``.
            * ``configurations`` -- configuration names and the active one.
        name: Sketch feature name, for ``analysis_type="sketch"``.
        near: ``[x, y, z]`` -- for faces and edges, return only the entities nearest
            this point, each annotated with its distance. Turns a 5000-face import
            into a handful of relevant candidates.
        limit: With ``near``, keep only the closest ``limit`` entities.
        axis: ``[x, y, z]`` -- for faces and edges, keep only entities roughly
            parallel to this direction. Faces filter by their planar normal, edges
            by their chord direction.
        unit: Unit for ``near`` and for reported lengths.
    """
    doc = session.require_doc("PART")
    converter = converter or units.UnitConverter(session.config.default_unit)
    kind = one_of(analysis_type, "analysis_type", ANALYSIS_TYPES)
    precision = session.config.precision

    if kind == "summary":
        return _summary(session, doc, converter, unit, precision)
    if kind == "mass_properties":
        return _mass_properties(session, doc, converter, unit, precision)
    if kind == "geometry":
        return {"analysis_type": kind, **selection.body_summary(doc)}
    if kind == "bodies":
        return _bodies(doc, precision)
    if kind in {"faces", "edges"}:
        return _topology(session, doc, kind, converter, unit, near, limit, axis, precision)
    if kind == "features":
        return _features(session, doc, converter, precision)
    if kind == "configurations":
        from .documents import list_configurations
        return {"analysis_type": kind, **list_configurations(session)}

    # sketch
    require(
        name, "name",
        hint="The sketch feature name, e.g. name='Sketch1'. "
             "analyze_model(analysis_type='features') lists them.",
    )
    return _sketch(doc, name, precision)


# ---------------------------------------------------------------------------

def _mass_property_values(doc) -> list[float]:
    """Raw mass properties, tolerating both API generations.

    ``GetMassProperties2`` layout: ``[cx, cy, cz, volume, surface_area, mass, ...moments]``.
    """
    status = byref_int()
    values = compat.as_floats(pm(doc, "GetMassProperties2", status, default=None))
    if len(values) >= 5:
        return values
    return compat.as_floats(pm(doc, "GetMassProperties", default=None))


def _mass_properties(session, doc, converter, unit, precision) -> dict:
    values = _mass_property_values(doc)
    if len(values) < 5:
        raise SolidWorksError(
            "ANALYSIS_FAILED",
            "Mass properties returned no usable data. The document needs at least one solid body.",
        )

    resolved = units.parse_unit(unit, converter.default)
    linear = units.TO_METERS[resolved]

    return {
        "analysis_type": "mass_properties",
        "volume_m3": round(values[3], 12),
        "surface_area_m2": round(values[4], 12),
        "mass_kg": round(values[5], 9) if len(values) > 5 else None,
        "center_of_gravity_m": [round(v, precision) for v in values[0:3]],
        "center_of_gravity": {
            "value": [round(v / linear, 6) for v in values[0:3]],
            "unit": resolved.value,
        },
        "note": "Mass is only meaningful once a material is assigned (set_material).",
    }


def _summary(session, doc, converter, unit, precision) -> dict:
    """Everything a caller usually wants first, in one round-trip."""
    topology = selection.body_summary(doc)
    values = _mass_property_values(doc)

    material = pm(doc, "GetMaterialPropertyName2", "", compat.byref_str(), default=None)
    if not material:
        material = pm(doc, "GetMaterialPropertyName", default=None)

    box = compat.as_floats(pm(doc, "GetPartBox", True, default=None))
    bounding_box = None
    if len(box) >= 6:
        resolved = units.parse_unit(unit, converter.default)
        linear = units.TO_METERS[resolved]
        bounding_box = {
            "min_m": [round(v, precision) for v in box[0:3]],
            "max_m": [round(v, precision) for v in box[3:6]],
            "size": {
                "value": [round((box[i + 3] - box[i]) / linear, 6) for i in range(3)],
                "unit": resolved.value,
            },
        }

    # Count real features only, matching analysis_type='features'. Including the
    # dozen-plus UI container nodes here would disagree with that tool's count.
    feature_count = 0
    sketch_count = 0
    for feature in compat.iter_features(doc):
        type_name = compat.feature_type(feature)
        if compat.is_tree_container(type_name):
            continue
        feature_count += 1
        if type_name == "ProfileFeature":
            sketch_count += 1

    manager = pm(doc, "SketchManager", default=None)
    active_sketch = pm(manager, "ActiveSketch", default=None) if manager else None

    return {
        "analysis_type": "summary",
        "document": pm(doc, "GetTitle", default=None),
        "path": pm(doc, "GetPathName", default="") or None,
        "topology": topology,
        "volume_m3": round(values[3], 12) if len(values) > 3 else None,
        "surface_area_m2": round(values[4], 12) if len(values) > 4 else None,
        "mass_kg": round(values[5], 9) if len(values) > 5 else None,
        "material": str(material) if material else None,
        "bounding_box": bounding_box,
        "feature_count": feature_count,
        "sketch_count": sketch_count,
        "active_sketch": (
            compat.feature_name(active_sketch, default="Sketch") if active_sketch else None
        ),
        "feature_errors": compat.error_count(doc),
        "is_sheet_metal": compat.find_feature_by_type(doc, "FlatPattern") is not None,
    }


def _bodies(doc, precision) -> dict:
    entries = []
    for index, body in enumerate(selection.solid_bodies(doc)):
        record = {
            "i": index,
            "name": pm(body, "Name", default=None),
            "faces": pm(body, "GetFaceCount", default=None),
            "edges": pm(body, "GetEdgeCount", default=None),
            "vertices": pm(body, "GetVertexCount", default=None),
        }
        props = compat.as_floats(pm(body, "GetMassProperties", 1.0, default=None))
        if len(props) >= 5:
            record["centroid_m"] = [round(v, precision) for v in props[0:3]]
            record["volume_m3"] = round(props[3], 12)
            record["area_m2"] = round(props[4], 12)
        entries.append(record)

    return {
        "analysis_type": "bodies",
        "body_count": len(entries),
        "bodies": entries,
        "note": "Feed 'i' to save_body_as_part(body_index=...).",
    }


def _topology(session, doc, kind, converter, unit, near, limit, axis, precision) -> dict:
    """Faces or edges with stable indices, optionally filtered by proximity/direction."""
    entities = selection.flatten_faces(doc) if kind == "faces" else selection.flatten_edges(doc)
    reader = selection.face_geometry if kind == "faces" else selection.edge_geometry

    if not entities:
        return {
            "analysis_type": kind, f"{kind[:-1]}_count": 0, kind: [],
            "note": "The part has no solid bodies.",
        }

    near_point = None
    if near:
        if len(near) < 3:
            raise ToolError("INVALID_PARAMETER", "near needs [x, y, z].")
        near_point = converter.points_to_meters(near[:3], unit)

    axis_vector = None
    if axis:
        if len(axis) < 3:
            raise ToolError("INVALID_PARAMETER", "axis needs [x, y, z].")
        length = math.sqrt(sum(float(v) ** 2 for v in axis[:3]))
        if length > 0:
            axis_vector = [float(v) / length for v in axis[:3]]

    records = []
    for index, entity in enumerate(entities):
        record = {"i": index}
        record.update(reader(entity, precision))

        if axis_vector is not None and not _matches_axis(record, kind, axis_vector):
            continue

        if near_point is not None:
            reference = record.get("point") or record.get("mid") or record.get("start")
            if reference is None:
                continue
            record["distance_m"] = round(
                math.sqrt(sum((reference[i] - near_point[i]) ** 2 for i in range(3))), precision
            )

        records.append(record)

    if near_point is not None:
        records.sort(key=lambda r: r.get("distance_m", float("inf")))
        if limit:
            records = records[: int(limit)]
    elif limit:
        records = records[: int(limit)]

    truncated = False
    cap = session.config.max_entities
    if len(records) > cap:
        records = records[:cap]
        truncated = True

    consumers = (
        "create_sketch(face_index=...) or extrude_feature(up_to_face_index=...)"
        if kind == "faces"
        else "add_edge_feature(edge_indices=[...])"
    )
    note = f"Feed 'i' to {consumers}. Coordinates are in metres."
    if truncated:
        # Say so explicitly: a silently capped list reads as "this is everything".
        note += f" Output was capped at {cap} entries -- narrow it with near/limit/axis."

    return {
        "analysis_type": kind,
        f"{kind[:-1]}_count": len(records),
        f"total_{kind}": len(entities),
        kind: records,
        "truncated": truncated,
        "filtered": bool(near_point is not None or axis_vector is not None),
        "note": note,
    }


def _matches_axis(record: dict, kind: str, axis_vector: list[float], tolerance: float = 0.02) -> bool:
    """Whether an entity's direction is roughly parallel to ``axis_vector``."""
    if kind == "faces":
        direction = record.get("normal") or record.get("axis")
    else:
        start, end = record.get("start"), record.get("end")
        if start and end:
            direction = [end[i] - start[i] for i in range(3)]
        else:
            direction = record.get("circle", {}).get("axis")

    if not direction:
        return False
    length = math.sqrt(sum(v ** 2 for v in direction))
    if length == 0:
        return False
    unit_direction = [v / length for v in direction]
    dot = abs(sum(unit_direction[i] * axis_vector[i] for i in range(3)))
    return dot >= 1.0 - tolerance


def _features(session, doc, converter, precision) -> dict:
    """The feature tree with dimensions -- the source of ``modify_dimension`` names."""
    features = []
    containers = 0
    for feature in compat.iter_features(doc):
        type_name = compat.feature_type(feature)

        # Skip the UI container nodes every tree starts with (Comments, Sensors,
        # Solid Bodies, Equations, ...). They are never actionable and would be a
        # dozen wasted entries in front of the real tree.
        if compat.is_tree_container(type_name):
            containers += 1
            continue

        record: dict = {"name": compat.feature_name(feature), "type": type_name}

        if bool(pm(feature, "IsSuppressed", default=False)):
            record["suppressed"] = True

        dimensions = []
        for display in compat.iter_display_dimensions(feature):
            dimension = compat.read_dimension(display)
            if dimension is None:
                continue
            full_name = pm(dimension, "FullName", default=None) or pm(dimension, "Name",
                                                                    default=None)
            si_value = compat.dimension_value(dimension)
            entry = {"name": full_name}
            if si_value is not None:
                try:
                    entry["value_si"] = round(float(si_value), precision)
                    entry["value_mm"] = round(float(si_value) * 1000.0, 6)
                except (TypeError, ValueError):
                    pass
            dimensions.append(entry)
        if dimensions:
            record["dimensions"] = dimensions

        if type_name == "ProfileFeature":
            record["sketch"] = _sketch_summary(feature, precision)

        features.append(record)

    equations = []
    manager = pm(doc, "GetEquationMgr", default=None)
    if manager is not None:
        count = pm(manager, "GetCount", default=0) or 0
        try:
            count = int(count)
        except (TypeError, ValueError):
            count = 0
        for index in range(count):
            text = pm(manager, "Equation", index, default=None)
            if text:
                equations.append({"index": index, "equation": str(text)})

    return {
        "analysis_type": "features",
        "feature_count": len(features),
        "features": features,
        "equations": equations,
        "hidden_container_nodes": containers,
        "note": "Features are in tree (history) order. Pass a dimension 'name' to "
                "modify_dimension to build a variant. "
                f"{containers} UI container node(s) (Comments, Sensors, Solid Bodies, "
                "Equations, ...) were omitted.",
    }


def _sketch_summary(sketch_feature, precision: int) -> dict:
    """Segment counts for a sketch, plus the circle parameters when it is a single circle.

    A summary rather than a coordinate dump: a part with twenty sketches would
    otherwise flood the response with geometry nobody asked for. Use
    ``analysis_type="sketch"`` for the full detail of one sketch.
    """
    sketch = pm(sketch_feature, "GetSpecificFeature2", default=None)
    if sketch is None:
        return {"readable": False}

    segments = compat.as_list(pm(sketch, "GetSketchSegments", default=None))
    counts: dict[str, int] = {}
    kinds = {0: "line", 1: "arc", 2: "ellipse", 3: "spline", 4: "text", 5: "parabola"}

    circle_info = None
    for segment in segments:
        try:
            code = int(pm(segment, "GetType", default=-1))
        except (TypeError, ValueError):
            code = -1
        label = kinds.get(code, "unknown")
        if bool(pm(segment, "ConstructionGeometry", default=False)):
            label += "_construction"
        counts[label] = counts.get(label, 0) + 1

    if len(segments) == 1:
        segment = segments[0]
        try:
            if int(pm(segment, "GetType", default=-1)) == 1:
                centre = pm(segment, "GetCenterPoint2", default=None)
                radius = pm(segment, "GetRadius", default=None)
                if centre is not None and radius is not None:
                    circle_info = {
                        "cx": round(pm(centre, "X", default=0.0), precision),
                        "cy": round(pm(centre, "Y", default=0.0), precision),
                        "radius_m": round(float(radius), precision),
                    }
        except (TypeError, ValueError):
            pass

    summary = {"segment_count": len(segments), "counts": counts}
    if circle_info:
        summary["circle"] = circle_info
    status = pm(sketch, "GetConstrainedStatus", default=None)
    summary["constrained"] = compat.sketch_constraint_status(status)
    return summary


def _sketch(doc, name: str, precision: int) -> dict:
    """One sketch's full segment geometry."""
    feature = compat.find_feature(doc, name)
    if feature is None:
        available = [
            compat.feature_name(f) for f in compat.iter_features(doc)
            if compat.feature_type(f) == "ProfileFeature"
        ]
        raise ToolError(
            "SKETCH_NOT_FOUND",
            f"No sketch named '{name}'.",
            hint=f"Sketches in this document: {', '.join(available) or '(none)'}.",
            details={"available_sketches": available},
        )

    sketch = pm(feature, "GetSpecificFeature2", default=None)
    if sketch is None:
        raise ToolError("SKETCH_NOT_FOUND", f"'{name}' is not a sketch feature.")

    kinds = {0: "line", 1: "arc", 2: "ellipse", 3: "spline", 4: "text", 5: "parabola"}
    segments = []
    for index, segment in enumerate(compat.as_list(pm(sketch, "GetSketchSegments", default=None))):
        try:
            code = int(pm(segment, "GetType", default=-1))
        except (TypeError, ValueError):
            code = -1
        record: dict = {
            "i": index,
            "kind": kinds.get(code, "unknown"),
            "construction": bool(pm(segment, "ConstructionGeometry", default=False)),
        }
        length = pm(segment, "GetLength", default=None)
        if length is not None:
            try:
                record["length_m"] = round(float(length), precision)
            except (TypeError, ValueError):
                pass

        if code == 0:
            start, end = pm(segment, "GetStartPoint2", default=None), pm(
                segment, "GetEndPoint2", default=None)
            if start is not None and end is not None:
                record["start"] = [round(pm(start, "X", default=0.0), precision),
                                  round(pm(start, "Y", default=0.0), precision)]
                record["end"] = [round(pm(end, "X", default=0.0), precision),
                                 round(pm(end, "Y", default=0.0), precision)]
        elif code == 1:
            centre = pm(segment, "GetCenterPoint2", default=None)
            radius = pm(segment, "GetRadius", default=None)
            if centre is not None:
                record["center"] = [round(pm(centre, "X", default=0.0), precision),
                                    round(pm(centre, "Y", default=0.0), precision)]
            if radius is not None:
                try:
                    record["radius_m"] = round(float(radius), precision)
                except (TypeError, ValueError):
                    pass
            start, end = pm(segment, "GetStartPoint2", default=None), pm(
                segment, "GetEndPoint2", default=None)
            if start is not None and end is not None:
                record["start"] = [round(pm(start, "X", default=0.0), precision),
                                  round(pm(start, "Y", default=0.0), precision)]
                record["end"] = [round(pm(end, "X", default=0.0), precision),
                                 round(pm(end, "Y", default=0.0), precision)]

        segments.append(record)

    from .sketches import read_sketch_plane

    return {
        "analysis_type": "sketch",
        "name": name,
        "segment_count": len(segments),
        "segments": segments,
        "plane": read_sketch_plane(sketch, precision),
        "note": "Coordinates are sketch-local, in metres.",
    }


# ---------------------------------------------------------------------------
# get_selection
# ---------------------------------------------------------------------------

def get_selection(session: SolidWorksSession) -> dict:
    """Report what the user currently has selected in the SolidWorks window.

    The inverse of index-based selection, and the bridge between a human pointing
    at geometry and a tool call. When the user clicks a face or edge while
    describing what they want, this maps each selected entity back to the **same
    stable index** ``analyze_model`` reports -- so a selected face becomes
    ``create_sketch(face_index=i)`` and a selected edge becomes
    ``add_edge_feature(edge_indices=[i])``.

    Read-only, and it does not clear the selection. But call it *before* any tool
    that does clear it -- ``create_sketch``, ``add_edge_feature``, any cut.
    """
    doc = session.active_doc()
    manager = pm(doc, "SelectionManager", default=None)
    if manager is None:
        raise SolidWorksError("NO_SELECTION_MANAGER", "The document exposes no SelectionManager.")

    count = pm(manager, "GetSelectedObjectCount2", -1, default=None)
    if count is None:
        count = pm(manager, "GetSelectedObjectCount", default=0)
    try:
        count = int(count)
    except (TypeError, ValueError):
        count = 0

    is_part = session.doc_type(doc) == "PART"
    faces = selection.flatten_faces(doc) if is_part else []
    edges = selection.flatten_edges(doc) if is_part else []
    precision = session.config.precision

    # swSelectType_e values for the kinds worth mapping.
    type_names = {
        1: "edge", 2: "face", 3: "vertex", 4: "plane", 5: "axis",
        6: "sketch_segment", 9: "sketch", 20: "component", 26: "body", 37: "reference_curve",
    }

    entries = []
    for position in range(1, count + 1):
        type_code = pm(manager, "GetSelectedObjectType3", position, -1, default=None)
        if type_code is None:
            type_code = pm(manager, "GetSelectedObjectType2", position, default=None)
        try:
            type_code = int(type_code)
        except (TypeError, ValueError):
            type_code = -1

        entity = pm(manager, "GetSelectedObject6", position, -1, default=None)
        if entity is None:
            entity = pm(manager, "GetSelectedObject5", position, default=None)

        record: dict = {"type": type_names.get(type_code, f"code_{type_code}")}

        if type_code == 2 and entity is not None:  # face
            index = selection.index_of_entity(faces, entity)
            record["i"] = index
            record.update(selection.face_geometry(entity, precision))
            record["use_with"] = (
                f"create_sketch(face_index={index})" if index >= 0
                else "index could not be resolved -- the face may belong to a hidden body"
            )
        elif type_code == 1 and entity is not None:  # edge
            index = selection.index_of_entity(edges, entity)
            record["i"] = index
            record.update(selection.edge_geometry(entity, precision))
            record["use_with"] = (
                f"add_edge_feature(edge_indices=[{index}])" if index >= 0
                else "index could not be resolved"
            )
        elif type_code == 3 and entity is not None:  # vertex
            record["point"] = selection.vertex_point(entity, precision)
        elif type_code in {4, 5, 9, 20, 37} and entity is not None:
            record["name"] = (
                pm(entity, "Name2", default=None) or pm(entity, "Name", default=None)
            )

        entries.append(record)

    return {
        "selected_count": len(entries),
        "selection": entries,
        "document": pm(doc, "GetTitle", default=None),
        "note": "Indices match analyze_model(faces|edges). i = -1 means the entity could not be "
                "matched to a visible solid body.",
    }


# ---------------------------------------------------------------------------
# compare_documents
# ---------------------------------------------------------------------------

def compare_documents(
    session: SolidWorksSession,
    document_a: str,
    document_b: str,
    *,
    volume_tolerance_percent: float = 1.0,
    area_tolerance_percent: float = 1.0,
) -> dict:
    """Objectively compare two parts or assemblies.

    The verification half of any rebuild or round-trip workflow: build a part from a
    recipe, then prove it matches the original instead of eyeballing it. Neither
    reference project offered this from Python.

    Each argument is either an absolute file path (opened from disk) or the title of
    an already-open document, so an unsaved rebuild can be compared too.

    Args:
        document_a: Reference document -- deltas are measured relative to it.
        document_b: Candidate document.
        volume_tolerance_percent: Volume delta allowed for a pass.
        area_tolerance_percent: Surface-area delta allowed for a pass.

    Returns:
        Per-document measurements, the deltas, and a ``passed`` verdict: topology
        must match exactly and volume and area must be within tolerance.
    """
    from .documents import activate_document, open_document

    def measure(reference: str) -> dict:
        import os

        if os.path.isfile(reference):
            open_document(session, reference)
        else:
            activate_document(session, reference)
        doc = session.active_doc()
        values = _mass_property_values(doc)
        return {
            "document": pm(doc, "GetTitle", default=None),
            "type": session.doc_type(doc),
            "topology": selection.body_summary(doc),
            "volume_m3": round(values[3], 12) if len(values) > 3 else None,
            "surface_area_m2": round(values[4], 12) if len(values) > 4 else None,
            "center_of_gravity_m": (
                [round(v, 9) for v in values[0:3]] if len(values) >= 3 else None
            ),
        }

    measured_a = measure(document_a)
    measured_b = measure(document_b)

    def percent_delta(a, b):
        if not a or not b:
            return None
        return round(abs(b - a) / abs(a) * 100.0, 6)

    volume_delta = percent_delta(measured_a["volume_m3"], measured_b["volume_m3"])
    area_delta = percent_delta(measured_a["surface_area_m2"], measured_b["surface_area_m2"])

    topology_a = {k: v for k, v in measured_a["topology"].items() if k != "volume"}
    topology_b = {k: v for k, v in measured_b["topology"].items() if k != "volume"}
    topology_matches = topology_a == topology_b

    cg_distance = None
    if measured_a["center_of_gravity_m"] and measured_b["center_of_gravity_m"]:
        cg_distance = round(math.sqrt(sum(
            (measured_b["center_of_gravity_m"][i] - measured_a["center_of_gravity_m"][i]) ** 2
            for i in range(3)
        )), 9)

    passed = (
        topology_matches
        and volume_delta is not None and volume_delta <= volume_tolerance_percent
        and area_delta is not None and area_delta <= area_tolerance_percent
    )

    return {
        "passed": passed,
        "reference": measured_a,
        "candidate": measured_b,
        "topology_matches": topology_matches,
        "volume_delta_percent": volume_delta,
        "area_delta_percent": area_delta,
        "center_of_gravity_distance_m": cg_distance,
        "criteria": {
            "topology": "exact match",
            "volume_percent": volume_tolerance_percent,
            "area_percent": area_tolerance_percent,
        },
        "note": f"The candidate document ({measured_b['document']}) is left active.",
    }
