"""
Entity selection and topology enumeration.

Selection is where CAD automation usually breaks. ``SelectByID2`` with
coordinates picks *the nearest* entity within a tolerance, which is fine for a
lone face on a cube and unusable for a concave inner corner, a small-radius
fillet edge, or a revolve end-cap where several faces meet near one point.

The fix -- carried over from the C# reference implementation and generalised
here -- is **stable index selection**: enumerate solid bodies in a deterministic
order, flatten their faces/edges, and select the i-th entity object directly via
``IEntity.Select4``. Because :func:`flatten_faces` and :func:`flatten_edges` are
the *same* enumeration the analysis tools report indices from, an index handed
back to a feature tool always resolves to the entity the caller meant.

Coordinate selection stays available as a fallback for interactive use.
"""

from __future__ import annotations

import logging

from . import compat, enums
from .compat import nothing, pm

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Basic selection
# ---------------------------------------------------------------------------

def clear(doc) -> None:
    """Drop the current selection list."""
    try:
        doc.ClearSelection2(True)
    except Exception:
        try:
            doc.ClearSelection()
        except Exception:
            pass


def select_by_id(
    doc,
    name: str,
    kind: str,
    x: float = 0.0,
    y: float = 0.0,
    z: float = 0.0,
    append: bool = False,
    mark: int = 0,
) -> bool:
    """``SelectByID2`` with the callout argument marshalled correctly.

    Passing Python ``None`` for the ``Callout`` parameter raises ``com_error`` on
    some SolidWorks builds; a typed ``VT_DISPATCH`` null works on all of them.

    ``SelectByID2``'s own return value is not trustworthy -- verified live on a
    drawing view, it reported ``False`` for a coordinate pick that had actually
    selected an edge (confirmed by the selection count increasing and a
    dependent ``AddHoleCallout()`` succeeding right after). Same silently-wrong-
    boolean contract as ``ShowConfiguration2`` elsewhere in this codebase, so
    success is verified by the selection count changing, not by the call's own
    report of it.
    """
    extension = pm(doc, "Extension", default=None)
    if extension is None:
        return False
    manager = pm(doc, "SelectionManager", default=None)
    before = pm(manager, "GetSelectedObjectCount2", -1, default=0) if manager is not None else 0
    for callout in (nothing(), None):
        try:
            extension.SelectByID2(name, kind, x, y, z, append, mark, callout, 0)
            if manager is not None:
                after = pm(manager, "GetSelectedObjectCount2", -1, default=0)
                if after is not None and after > before:
                    return True
            else:
                # No SelectionManager to verify against -- fall back to trusting
                # the call not having raised.
                return True
        except Exception as exc:
            logger.debug("SelectByID2(%s, %s) failed with callout=%r: %s", name, kind, callout, exc)
    return False


def select_entity(entity, append: bool = False, mark: int = 0, doc=None) -> bool:
    """Select a COM entity object directly, bypassing coordinate picking.

    ``IEntity.Select4`` takes a ``SelectData`` carrying the mark. Some builds
    accept a null ``SelectData`` and a mark of 0; when a non-zero mark is needed
    (mates, pattern seeds, up-to-surface references) a real ``SelectData`` must
    be created from the selection manager, hence the optional ``doc``.
    """
    if entity is None:
        return False

    select_data = None
    if mark and doc is not None:
        try:
            manager = pm(doc, "SelectionManager", default=None)
            if manager is not None:
                select_data = manager.CreateSelectData()
                select_data.Mark = mark
        except Exception:
            select_data = None

    for candidate_data in (select_data, nothing(), None):
        try:
            return bool(entity.Select4(append, candidate_data))
        except Exception:
            continue

    # Pre-2010 fallback: IEntity.Select with mark.
    try:
        return bool(entity.Select(append, mark))
    except Exception:
        return False


def select_feature(feature, append: bool = False, mark: int = 0) -> bool:
    """Select a feature object (works for any feature type, unlike ``BODYFEATURE``).

    ``SelectByID2(name, "BODYFEATURE", ...)`` silently fails for sketches and
    reference geometry; selecting the resolved ``IFeature`` handles every type.
    """
    if feature is None:
        return False
    try:
        return bool(feature.Select2(append, mark))
    except Exception:
        try:
            return bool(feature.Select(append))
        except Exception:
            return False


def select_feature_by_name(doc, name: str, append: bool = False, mark: int = 0) -> bool:
    """Select a named feature, trying the fast typed path then the tree walk."""
    if select_by_id(doc, name, enums.SEL_STRING_BODYFEATURE, append=append, mark=mark):
        return True
    if select_by_id(doc, name, enums.SEL_STRING_SKETCH, append=append, mark=mark):
        return True
    feature = compat.find_feature(doc, name)
    return select_feature(feature, append=append, mark=mark)


# ---------------------------------------------------------------------------
# Language-independent plane resolution
# ---------------------------------------------------------------------------

def select_plane(doc, plane_name: str, append: bool = False, mark: int = 0) -> bool:
    """Select a reference plane, tolerating localised default-plane names.

    A German SolidWorks calls the front plane "Vorne", so
    ``SelectByID2("Front Plane", "PLANE", ...)`` fails there. When the name lookup
    misses and the requested name is recognisably one of the three defaults, the
    plane is resolved by its **ordinal** among ``RefPlane`` features instead --
    the first three ``RefPlane`` entries are always Front, Top, Right regardless
    of locale.

    Args:
        plane_name: ``"Front Plane"``, ``"front"``, ``"XY"``, ``"Plane1"``, ...
    """
    canonical = enums.PLANE_ALIASES.get(plane_name.strip().lower(), plane_name)

    for candidate in (canonical, plane_name):
        if select_by_id(doc, candidate, enums.SEL_STRING_PLANE, append=append, mark=mark):
            return True

    lowered = plane_name.strip().lower()
    ordinal = None
    for token, index in enums.DEFAULT_PLANE_ORDINALS.items():
        if token in lowered:
            ordinal = index
            break
    if ordinal is None:
        return False

    seen = 0
    for feature in compat.iter_features(doc):
        if compat.feature_type(feature) != "RefPlane":
            continue
        if seen == ordinal:
            if not append:
                clear(doc)
            logger.debug("Resolved plane %r by ordinal %d (%s)", plane_name, ordinal,
                         compat.feature_name(feature))
            return select_feature(feature, append=append, mark=mark)
        seen += 1
    return False


# ---------------------------------------------------------------------------
# Topology enumeration -- the source of stable indices
# ---------------------------------------------------------------------------

def solid_bodies(doc) -> list:
    """Every solid body of a part, in deterministic order.

    ``GetBodies2(swSolidBody, visibleOnly=True)`` is the enumeration all index
    reporting and index selection share. Do not reorder it -- the indices the
    analysis tools hand out are positions in this list.
    """
    bodies = pm(doc, "GetBodies2", enums.BODY_SOLID, True, default=None)
    if bodies is None:
        bodies = pm(doc, "GetBodies", enums.BODY_SOLID, default=None)
    return compat.as_list(bodies)


def flatten_faces(doc) -> list:
    """All faces of all solid bodies, flattened in body-then-face order.

    The returned list index *is* the ``face_index`` accepted by
    ``create_sketch``, ``extrude_feature(up_to_face_index=...)`` and
    ``sheet_metal_feature(fixed_face_index=...)``, and reported by
    ``analyze_model(analysis_type="faces")``.
    """
    faces = []
    for body in solid_bodies(doc):
        faces.extend(compat.as_list(pm(body, "GetFaces", default=None)))
    return faces


def flatten_edges(doc) -> list:
    """All edges of all solid bodies, flattened in body-then-edge order.

    The returned list index *is* the ``edge_index`` accepted by
    ``add_edge_feature`` and ``sheet_metal_feature``, and reported by
    ``analyze_model(analysis_type="edges")``.
    """
    edges = []
    for body in solid_bodies(doc):
        edges.extend(compat.as_list(pm(body, "GetEdges", default=None)))
    return edges


def select_faces_by_index(doc, indices, mark: int = 0, append: bool = False) -> tuple[bool, str]:
    """Select faces by their :func:`flatten_faces` positions.

    Returns:
        ``(ok, error_message)``. The message names the out-of-range index and the
        actual face count, so the caller can re-run ``analyze_model(faces)``.
    """
    faces = flatten_faces(doc)
    if not faces:
        return False, "The active part has no solid bodies, so it has no faces to select."
    for offset, index in enumerate(indices):
        if index < 0 or index >= len(faces):
            return False, (
                f"face_index {index} is out of range: the part has {len(faces)} faces "
                f"(0-{len(faces) - 1}). Call analyze_model(analysis_type='faces') for valid indices."
            )
        if not select_entity(faces[index], append=append or offset > 0, mark=mark, doc=doc):
            return False, f"Face index {index} exists but could not be selected."
    return True, ""


def select_edges_by_index(doc, indices, mark: int = 0, append: bool = False) -> tuple[bool, str]:
    """Select edges by their :func:`flatten_edges` positions."""
    edges = flatten_edges(doc)
    if not edges:
        return False, "The active part has no solid bodies, so it has no edges to select."
    for offset, index in enumerate(indices):
        if index < 0 or index >= len(edges):
            return False, (
                f"edge_index {index} is out of range: the part has {len(edges)} edges "
                f"(0-{len(edges) - 1}). Call analyze_model(analysis_type='edges') for valid indices."
            )
        if not select_entity(edges[index], append=append or offset > 0, mark=mark, doc=doc):
            return False, f"Edge index {index} exists but could not be selected."
    return True, ""


def edge_at_index(doc, index: int):
    """The edge object at ``index``, or ``None``. Used where the object itself is
    needed rather than a selection (``InsertSketchForEdgeFlange``)."""
    edges = flatten_edges(doc)
    if 0 <= index < len(edges):
        return edges[index]
    return None


def face_at_index(doc, index: int):
    """The face object at ``index``, or ``None``."""
    faces = flatten_faces(doc)
    if 0 <= index < len(faces):
        return faces[index]
    return None


def index_of_entity(candidates: list, entity) -> int:
    """Position of ``entity`` in ``candidates``, or ``-1``.

    COM identity comparison is unreliable across proxies, so this compares the
    underlying ``IUnknown`` pointer where available and falls back to ``==``.
    This is how ``get_selection`` maps a user's GUI click back to a stable index.
    """
    for index, candidate in enumerate(candidates):
        try:
            if candidate is entity or candidate == entity:
                return index
        except Exception:
            pass
        try:
            if int(candidate) == int(entity):  # IDispatch pointer identity
                return index
        except Exception:
            pass
    return -1


# ---------------------------------------------------------------------------
# Geometry readers
# ---------------------------------------------------------------------------

def _clean(values, precision: int) -> list[float]:
    """Round coordinates and normalise ``-0.0`` to ``0.0``."""
    return [round(v, precision) + 0.0 for v in values]


def face_geometry(face, precision: int = 6) -> dict:
    """Describe a face: planarity, area, its outward normal, and a point on it.

    The normal and representative point are what let a caller sketch on, or
    terminate an extrusion at, a face they located by analysis -- so both have to be
    the *face's* values, not the underlying surface's:

    * **Normal** comes from ``IFace2.Normal``. The surface's ``PlaneParams`` normal
      ignores face sense, so on a simple plate the top and bottom faces reported the
      *same* normal and "pick the upward face" was impossible.
    * **Point** is the face's own centre, via ``GetClosestPointOn`` on its bounding
      box centre. ``PlaneParams`` returns the infinite plane's root, which is not
      necessarily inside the face's bounded region at all.
    """
    info: dict = {"planar": False}

    area = pm(face, "GetArea", default=None)
    if area is not None:
        try:
            info["area"] = round(float(area), 9)
        except (TypeError, ValueError):
            pass

    # A point guaranteed to lie on the face itself.
    box = compat.as_floats(pm(face, "GetBox", default=None))
    if len(box) >= 6:
        centre = [(box[i] + box[i + 3]) / 2.0 for i in range(3)]
        on_face = compat.as_floats(pm(face, "GetClosestPointOn", *centre, default=None))
        if len(on_face) >= 3:
            info["point"] = _clean(on_face[0:3], precision)
        info["box"] = {
            "min": _clean(box[0:3], precision),
            "max": _clean(box[3:6], precision),
        }

    surface = pm(face, "GetSurface", default=None)
    if surface is None:
        return info

    try:
        if bool(pm(surface, "IsPlane", default=False)):
            info["planar"] = True
            # The face's true outward normal, independent of the underlying surface.
            # Only meaningful for a planar face -- a curved face has no single
            # normal, and IFace2.Normal degenerates to a zero vector there rather
            # than raising, which would otherwise look like a real (if odd) value.
            normal = compat.as_floats(pm(face, "Normal", default=None))
            if len(normal) >= 3:
                info["normal"] = _clean(normal[0:3], precision)
            else:
                params = compat.as_floats(pm(surface, "PlaneParams", default=None))
                if len(params) >= 3:
                    info["normal"] = _clean(params[0:3], precision)
            # PlaneParams' root point is a fallback only -- the face-level point
            # above (guaranteed to lie within the face's bounds) is preferred.
            params = compat.as_floats(pm(surface, "PlaneParams", default=None))
            if len(params) >= 6:
                info.setdefault("point", _clean(params[3:6], precision))
    except Exception:
        pass

    try:
        if bool(pm(surface, "IsCylinder", default=False)):
            info["cylindrical"] = True
            params = compat.as_floats(pm(surface, "CylinderParams", default=None))
            if len(params) >= 7:
                info["axis_point"] = [round(v, precision) for v in params[0:3]]
                info["axis"] = [round(v, precision) for v in params[3:6]]
                info["radius"] = round(params[6], precision)
    except Exception:
        pass

    return info


def edge_geometry(edge, precision: int = 6) -> dict:
    """Describe an edge: endpoints, midpoint, length, and circle parameters.

    The midpoint is the coordinate a caller would pass to a coordinate-based
    pick; the index is the robust alternative. Closed (circular) edges have no
    distinct endpoints, so they report ``closed: True`` plus circle parameters.
    """
    info: dict = {}

    params = compat.as_floats(pm(edge, "GetCurveParams2", default=None))
    if len(params) < 8:
        params = compat.as_floats(pm(edge, "GetCurveParams", default=None))

    start_param = end_param = None
    if len(params) >= 8:
        info["start"] = [round(v, precision) for v in params[0:3]]
        info["end"] = [round(v, precision) for v in params[3:6]]
        start_param, end_param = params[6], params[7]

    curve = pm(edge, "GetCurve", default=None)
    if curve is not None:
        if start_param is not None and end_param is not None:
            midpoint = compat.as_floats(
                pm(curve, "Evaluate", (start_param + end_param) / 2.0, default=None)
            )
            if len(midpoint) >= 3:
                info["mid"] = [round(v, precision) for v in midpoint[0:3]]
            length = pm(curve, "GetLength3", start_param, end_param, default=None)
            if length is not None:
                try:
                    info["length"] = round(float(length), precision)
                except (TypeError, ValueError):
                    pass
        try:
            if bool(pm(curve, "IsCircle", default=False)):
                circle = compat.as_floats(pm(curve, "CircleParams", default=None))
                if len(circle) >= 7:
                    info["circle"] = {
                        "center": [round(v, precision) for v in circle[0:3]],
                        "axis": [round(v, precision) for v in circle[3:6]],
                        "radius": round(circle[6], precision),
                    }
        except Exception:
            pass
        try:
            if bool(pm(curve, "IsLine", default=False)):
                info["line"] = True
        except Exception:
            pass

    if "start" not in info:
        # Closed edge: no start/end vertices exist.
        info["closed"] = True
        if "circle" in info:
            info["mid"] = info["circle"]["center"]

    return info


def vertex_point(vertex, precision: int = 6) -> list[float] | None:
    """The 3D coordinates of a vertex."""
    point = compat.as_floats(pm(vertex, "Point", default=None))
    if len(point) >= 3:
        return [round(v, precision) for v in point[0:3]]
    return None


def body_summary(doc) -> dict:
    """Topology + volume counts for the active part.

    Returned in-band by every geometry-changing tool so a caller can confirm a
    feature landed without a separate analysis round-trip.
    """
    bodies = solid_bodies(doc)
    faces = edges = vertices = 0
    volume = 0.0
    for body in bodies:
        faces += int(pm(body, "GetFaceCount", default=0) or 0)
        edges += int(pm(body, "GetEdgeCount", default=0) or 0)
        vertices += int(pm(body, "GetVertexCount", default=0) or 0)
        props = compat.as_floats(pm(body, "GetMassProperties", 1.0, default=None))
        if len(props) >= 4:
            volume += props[3]
    return {
        "bodies": len(bodies),
        "faces": faces,
        "edges": edges,
        "vertices": vertices,
        "volume": round(volume, 12),
    }
