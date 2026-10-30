"""
3D feature creation and editing.

Every feature call here goes through :func:`~solidworks_mcp.com.compat.invoke_first`
with a newest-API-first candidate list. That is the mechanism giving one code
path SolidWorks 2018-2026 coverage: ``FeatureExtrusion3`` on modern builds falls
back to ``FeatureExtrusion2`` with its 23- and then 20-argument vectors on older
ones, and the report says which signature won.

Two selection subtleties are handled centrally, both learned the hard way:

* **Post-exit re-selection.** Exiting a sketch leaves the *sketch* implicitly
  selected when it sat on a plane, but leaves the *face* selected when it sat on
  a model face. So the profile is always re-selected by name after exiting;
  without it, a face-sketched cut finds no profile and returns null.
* **Direction recovery.** A null cut is most often the wrong direction -- the cut
  went away from material. The caller's explicit ``reverse`` is honoured first,
  then the opposite direction is retried before failing.
"""

from __future__ import annotations

import logging

from ..com import compat, enums, selection
from ..com.compat import byref_str, nothing, pm
from ..com.connection import SolidWorksError, SolidWorksSession
from ..util import units
from ..util.result import ToolError, one_of, require, require_positive

logger = logging.getLogger(__name__)

FEATURE_TYPES = {"boss", "cut", "revolve", "sweep", "loft", "thin"}
EDGE_FEATURE_TYPES = {"fillet", "chamfer"}
PATTERN_TYPES = {"linear", "circular", "mirror"}
REFERENCE_TYPES = {"plane", "axis", "point"}
EDIT_ACTIONS = {"suppress", "unsuppress", "delete", "rename", "rollback_to", "rollback_end"}


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _exit_sketch_capturing_profile(doc) -> str | None:
    """Exit any active sketch and return its name.

    The name is the profile handle for everything downstream: sketch names resolve
    reliably after exit even though sketch *coordinates* do not.
    """
    manager = pm(doc, "SketchManager", default=None)
    if manager is None:
        return None
    sketch = pm(manager, "ActiveSketch", default=None)
    if sketch is None:
        return None
    name = compat.feature_name(sketch, default=None)
    compat.invoke_any(manager, [("InsertSketch", (True,))])
    return name


def _reselect_profile(doc, profile_name: str | None) -> None:
    """Re-select a profile sketch by name after exiting it.

    Required for face-mounted sketches (see the module docstring) and harmless for
    plane-mounted ones.
    """
    selection.clear(doc)
    if profile_name:
        selection.select_by_id(doc, profile_name, enums.SEL_STRING_SKETCH)


def _find_flat_pattern(doc):
    """The part's Flat-Pattern feature, if it is sheet metal."""
    return compat.find_feature_by_type(doc, "FlatPattern")


class _FlatPatternRollback:
    """Temporarily roll the tree back before Flat-Pattern.

    A sheet-metal part keeps a (usually suppressed) Flat-Pattern as the **last**
    top-level feature with the rollback bar after it. Inserting a cut or mirror
    there would place it after Flat-Pattern, which is illegal, so SolidWorks
    silently returns null. Rolling back before Flat-Pattern, inserting, then
    rolling forward is exactly what the SolidWorks UI does automatically.
    """

    def __init__(self, doc):
        self.doc = doc
        self.rolled = False

    def __enter__(self):
        flat = _find_flat_pattern(self.doc)
        if flat is None:
            return self
        manager = pm(self.doc, "FeatureManager", default=None)
        if manager is None:
            return self
        report = compat.invoke_bool(manager, [
            ("EditRollback", (enums.ROLLBACK_TO_BEFORE_FEATURE, compat.feature_name(flat))),
        ])
        self.rolled = report.ok
        return self

    def __exit__(self, *exc_info):
        if self.rolled:
            manager = pm(self.doc, "FeatureManager", default=None)
            if manager is not None:
                compat.invoke_bool(manager, [("EditRollback", (enums.ROLLBACK_TO_END, ""))])
        return False

    @property
    def is_sheet_metal(self) -> bool:
        return _find_flat_pattern(self.doc) is not None


def _commit(session: SolidWorksSession, doc) -> None:
    """Rebuild so analysis tools observe the new geometry, if configured to."""
    if session.config.rebuild_after_features:
        compat.invoke_any(doc, [("EditRebuild3", ()), ("EditRebuild", ())])


# ---------------------------------------------------------------------------
# extrude_feature -- boss / cut / revolve / sweep / loft / thin
# ---------------------------------------------------------------------------

def extrude_feature(
    session: SolidWorksSession,
    feature_type: str = "boss",
    depth: float | None = None,
    *,
    angle: float = 360.0,
    axis: list | None = None,
    path_sketch: str | None = None,
    profiles: list | None = None,
    reverse: bool = False,
    through_all: bool = False,
    mid_plane: bool = False,
    up_to_face_index: int | None = None,
    draft_angle: float = 0.0,
    draft_outward: bool = False,
    thickness: float | None = None,
    merge_result: bool = True,
    unit: str | None = None,
    converter: units.UnitConverter | None = None,
) -> dict:
    """Turn the active sketch into a 3D feature.

    Consolidates what the reference Python project split into ``extrude_sketch``
    and ``cut_extrude`` (and did not offer at all for revolve, sweep or loft).
    The active sketch is consumed as the profile and sketch mode is exited
    automatically.

    Args:
        feature_type:
            * ``boss`` -- solid extrusion (default)
            * ``cut`` -- material removal; needs an existing solid body
            * ``revolve`` -- solid of revolution; needs ``axis`` and ``angle``
            * ``sweep`` -- swept solid; needs ``path_sketch``
            * ``loft`` -- lofted solid; needs ``profiles`` (2 or more sketches)
            * ``thin`` -- thin-walled extrusion; needs ``thickness``
        depth: Extrusion/cut depth in ``unit``. Required for blind and mid-plane
            boss/cut; ignored for through-all and up-to-face.
        angle: Revolve angle in **degrees** (360 = full revolve). Also the draft
            angle is separate -- see ``draft_angle``.
        axis: ``[x1, y1, x2, y2]`` -- two endpoints of the revolve centreline in
            sketch coordinates. The segment is converted to construction geometry
            so SolidWorks treats it as the axis and the closed loop as the profile.
        path_sketch: Sweep only -- the name of the sketch holding the path. Create
            the *profile* sketch last so it is the active one.
        profiles: Loft only -- sketch names in order, at least two.
        reverse: Flip the feature direction. Needed for a cut or boss sketched on
            a model face where material lies on the opposite side.
        through_all: Through-all end condition; ``depth`` is ignored.
        mid_plane: Extrude symmetrically about the sketch plane; ``depth`` is the
            **total** width. Conflicts with ``through_all`` and ``up_to_face_index``.
        up_to_face_index: Terminate exactly on this face (index from
            ``analyze_model(faces)``); ``depth`` is ignored.
        draft_angle: Draft in degrees, 0 for none.
        draft_outward: Draft outward rather than inward.
        thickness: Wall thickness for ``feature_type="thin"``, in ``unit``.
        merge_result: Merge into the existing body rather than creating a new one.
        unit: Unit for all lengths in this call.
    """
    doc = session.require_doc("PART")
    converter = converter or units.UnitConverter(session.config.default_unit)
    kind = one_of(feature_type, "feature_type", FEATURE_TYPES)

    if mid_plane and (through_all or up_to_face_index is not None):
        raise ToolError(
            "INVALID_PARAMETER",
            "mid_plane conflicts with through_all and up_to_face_index -- pick one end condition.",
        )

    needs_depth = kind in {"boss", "cut", "thin"} and not through_all and up_to_face_index is None
    if needs_depth:
        require_positive(
            depth, "depth",
            hint="Pass through_all=true for a through-all instead, or up_to_face_index to "
                 "terminate on a face.",
        )
    if kind == "thin":
        require_positive(
            thickness, "thickness",
            hint="feature_type='thin' builds a thin-walled extrusion, so it needs the wall "
                 "thickness. Use feature_type='boss' for a solid one.",
        )

    depth_m = converter.to_meters(depth, unit) if depth is not None else 0.0
    manager = pm(doc, "FeatureManager", default=None)
    if manager is None:
        raise SolidWorksError("NO_FEATURE_MANAGER", "The active document exposes no FeatureManager.")

    # Capture the profile before exiting. Revolve is the exception: its axis is
    # picked by coordinate, which only resolves while the sketch is still open.
    profile_name = None
    if kind == "revolve":
        sketch_manager = pm(doc, "SketchManager", default=None)
        profile_name = compat.feature_name(
            pm(sketch_manager, "ActiveSketch", default=None), default=None
        ) if sketch_manager else None
    else:
        profile_name = _exit_sketch_capturing_profile(doc)

    if kind == "revolve":
        feature, signature = _make_revolve(doc, manager, converter, angle, axis, unit)
    elif kind == "sweep":
        feature, signature = _make_sweep(doc, manager, profile_name, path_sketch)
    elif kind == "loft":
        feature, signature = _make_loft(doc, manager, profiles)
    elif kind == "cut":
        feature, signature = _make_cut(
            doc, manager, profile_name, depth_m, reverse, through_all, mid_plane,
            up_to_face_index, draft_angle, draft_outward,
        )
    else:  # boss or thin
        feature, signature = _make_boss(
            doc, manager, profile_name, depth_m, reverse, through_all, mid_plane,
            up_to_face_index, draft_angle, draft_outward, merge_result,
            converter.to_meters(thickness, unit) if kind == "thin" else None,
        )

    if feature is None:
        raise SolidWorksError(
            "FEATURE_FAILED",
            f"Creating the {kind} feature returned null. "
            + _failure_hint(kind, profile_name),
            {"attempted_signatures": signature, "profile_sketch": profile_name},
        )

    _commit(session, doc)
    return {
        "feature": compat.feature_name(feature),
        "feature_type": kind,
        "profile_sketch": profile_name,
        "api": signature,
        "body": selection.body_summary(doc),
    }


def _failure_hint(kind: str, profile: str | None) -> str:
    if kind == "cut":
        return (
            "Check that a solid body exists, the profile is closed, and the cut direction hits "
            "material -- pass reverse=true for a sketch on a model face."
        )
    if kind == "revolve":
        return "Check the axis coordinates match a real centreline segment and the profile is closed."
    if kind == "sweep":
        return "Check the path sketch name is exact and the profile sketch was the active one."
    if kind == "loft":
        return "Check every named profile sketch exists and the profiles do not self-intersect."
    return f"Check the profile sketch{f' ({profile})' if profile else ''} is a closed, valid contour."


def _end_condition(through_all: bool, mid_plane: bool, up_to_face: bool) -> int:
    if up_to_face:
        return enums.END_UP_TO_SURFACE
    if mid_plane:
        return enums.END_MID_PLANE
    if through_all:
        return enums.END_THROUGH_ALL
    return enums.END_BLIND


def _select_up_to_face(doc, index: int | None) -> None:
    """Append-select the end-condition reference face with mark 1.

    Mark 1 is the reference mark ``FeatureExtrusion3``/``FeatureCut4`` read the
    up-to-surface face from.
    """
    if index is None:
        return
    ok, error = selection.select_faces_by_index(doc, [int(index)], mark=1, append=True)
    if not ok:
        raise ToolError(
            "FACE_NOT_FOUND", error,
            hint="Call analyze_model(analysis_type='faces') for valid indices.",
        )


def _make_boss(doc, manager, profile, depth_m, reverse, through_all, mid_plane,
               up_to_face_index, draft_angle, draft_outward, merge, thickness_m):
    end_condition = _end_condition(through_all, mid_plane, up_to_face_index is not None)
    draft_rad = units.deg_to_rad(draft_angle)
    has_draft = abs(draft_rad) > 1e-12

    _reselect_profile(doc, profile)
    _select_up_to_face(doc, up_to_face_index)

    if thickness_m:
        # Thin feature: FeatureExtrusionThin2 keeps the same leading arguments and
        # appends the wall-thickness group.
        report = compat.invoke_first(manager, [
            ("FeatureExtrusionThin2", (
                True, False, reverse, end_condition, 0, depth_m, 0.0,
                has_draft, False, draft_outward, False, draft_rad, 0.0,
                False, False, False, False, merge, True, True,
                enums.START_SKETCH_PLANE, 0.0, False,
                thickness_m, 0.0, 0,  # T1, T2, thin-wall type (0 = one direction)
                0.0, False, False,
            )),
        ])
        if report.ok:
            return report.value, report.signature
        raise SolidWorksError(
            "FEATURE_FAILED",
            "No thin-extrusion API responded on this SolidWorks build.",
            {"attempts": report.diagnostic()},
        )

    report = compat.invoke_first(manager, [
        # SolidWorks 2014+ -- 23 arguments.
        ("FeatureExtrusion3", (
            True, False, reverse, end_condition, 0, depth_m, 0.0,
            has_draft, False, draft_outward, False, draft_rad, 0.0,
            False, False, False, False, merge, True, True,
            enums.START_SKETCH_PLANE, 0.0, False,
        )),
        # Older builds -- FeatureExtrusion2 at 23 then 20 arguments.
        ("FeatureExtrusion2", (
            True, False, reverse, end_condition, 0, depth_m, depth_m,
            has_draft, False, draft_outward, False, draft_rad, 0.0,
            False, False, False, False, merge, True, True,
            enums.START_SKETCH_PLANE, 0.0, False,
        )),
        ("FeatureExtrusion2", (
            True, False, reverse, end_condition, 0, depth_m, depth_m,
            has_draft, False, draft_outward, False, draft_rad, 0.0,
            False, False, False, merge, True, True, True,
        )),
    ])
    return report.value, report.signature or report.diagnostic()


def _make_cut(doc, manager, profile, depth_m, reverse, through_all, mid_plane,
              up_to_face_index, draft_angle, draft_outward):
    end_condition = _end_condition(through_all, mid_plane, up_to_face_index is not None)
    draft_rad = units.deg_to_rad(draft_angle)
    has_draft = abs(draft_rad) > 1e-12

    with _FlatPatternRollback(doc) as rollback:
        is_sheet_metal = rollback.rolled or rollback.is_sheet_metal

        # Sheet metal needs the Normal Cut option (perpendicular to the sheet
        # faces) and the opposite default direction, because its profile sits on
        # a sheet face rather than a boundary plane.
        normal_cut = is_sheet_metal
        direction = reverse if is_sheet_metal else not reverse

        def attempt(dir_flag):
            _reselect_profile(doc, profile)
            _select_up_to_face(doc, up_to_face_index)
            return compat.invoke_first(manager, [
                ("FeatureCut4", (
                    True, False, dir_flag, end_condition, 0, depth_m, 0.0,
                    has_draft, False, draft_outward, False, draft_rad, 0.0,
                    False, False, False, False, normal_cut, True, True,
                    False, False, False,
                    enums.START_SKETCH_PLANE, 0.0, False, False,
                )),
                ("FeatureCut3", (
                    True, False, dir_flag, end_condition, 0, depth_m, 0.0,
                    has_draft, False, draft_outward, False, draft_rad, 0.0,
                    False, False, False, False, normal_cut, True, True,
                    False, False, False,
                    enums.START_SKETCH_PLANE, 0.0, False,
                )),
                ("FeatureCut2", (
                    True, False, dir_flag, end_condition, 0, depth_m, 0.0,
                    has_draft, False, draft_outward, False, draft_rad, 0.0,
                    False, False, False, True, True, True,
                )),
            ])

        report = attempt(direction)
        if not report.ok:
            # Wrong direction is the overwhelmingly common cause of a null cut:
            # the cut ran away from material so nothing was removed. Retry
            # flipped. The caller's explicit `reverse` was honoured first, so
            # this recovers a failure without overriding intent.
            logger.debug("cut returned null; retrying with flipped direction")
            report = attempt(not direction)

        return report.value, report.signature or report.diagnostic()


def _make_revolve(doc, manager, converter, angle, axis, unit):
    if not axis or len(axis) < 4:
        raise ToolError(
            "MISSING_PARAMETER",
            "revolve requires axis=[x1, y1, x2, y2] -- the two endpoints of the centreline.",
        )

    sketch_manager = pm(doc, "SketchManager", default=None)
    if sketch_manager is None or pm(sketch_manager, "ActiveSketch", default=None) is None:
        raise ToolError(
            "SKETCH_NOT_ACTIVE",
            "revolve needs the profile-and-axis sketch to still be open.",
            hint="Call create_sketch, draw the closed profile plus the axis line, then revolve.",
        )

    x1, y1, x2, y2 = converter.points_to_meters(axis[:4], unit)
    angle_rad = units.snap_full_turn(units.deg_to_rad(angle))

    selection.clear(doc)
    if not selection.select_by_id(
        doc, "", enums.SEL_STRING_SKETCHSEGMENT, (x1 + x2) / 2.0, (y1 + y2) / 2.0, 0.0
    ):
        raise ToolError(
            "AXIS_NOT_FOUND",
            f"No sketch segment found at the midpoint of the axis {list(axis[:4])}.",
            hint="axis must be the two endpoints of a line that exists in the sketch.",
        )

    # Converting the axis line to construction geometry is what makes SolidWorks
    # treat it as the axis of revolution and the remaining closed loop as the sole
    # profile -- mirroring the manual workflow.
    manager_selection = pm(doc, "SelectionManager", default=None)
    if manager_selection is not None:
        segment = pm(manager_selection, "GetSelectedObject6", 1, -1, default=None)
        if segment is not None:
            try:
                segment.ConstructionGeometry = True
            except Exception:
                pass

    profile_name = _exit_sketch_capturing_profile(doc)
    _reselect_profile(doc, profile_name)

    report = compat.invoke_first(manager, [
        ("FeatureRevolve2", (
            True, True, False, False, False, False, 0, 0,
            angle_rad, 0.0, False, False, 0.0, 0.0, 0, 0.0, 0.0, True, True, True,
        )),
        ("FeatureRevolve", (angle_rad, True, 0.0, 0, 0)),
    ])
    return report.value, report.signature or report.diagnostic()


def _make_sweep(doc, manager, profile_name, path_sketch):
    require(
        path_sketch, "path_sketch",
        hint="Name the sketch containing the sweep path, e.g. path_sketch='Sketch2'.",
    )
    if not profile_name:
        raise ToolError(
            "PROFILE_NOT_FOUND",
            "sweep needs an active profile sketch when it is called.",
            hint="Create the profile sketch last so it is the active one, and pass the "
                 "path sketch by name.",
        )

    # A sweep needs BOTH selections: profile at mark 1, path at mark 4.
    # Selecting only the path leaves the API with no profile and it returns null.
    selection.clear(doc)
    if not selection.select_by_id(doc, profile_name, enums.SEL_STRING_SKETCH, mark=1):
        raise ToolError("PROFILE_NOT_FOUND", f"Profile sketch '{profile_name}' could not be selected.")
    if not selection.select_by_id(doc, path_sketch, enums.SEL_STRING_SKETCH, append=True, mark=4):
        available = [
            compat.feature_name(f) for f in compat.iter_features(doc)
            if compat.feature_type(f) == "ProfileFeature"
        ]
        raise ToolError(
            "SKETCH_NOT_FOUND",
            f"Path sketch '{path_sketch}' not found.",
            hint=f"Sketches in this document: {', '.join(available) or '(none)'}.",
            details={"available_sketches": available},
        )

    report = compat.invoke_first(manager, [
        ("InsertProtrusionSwept4", (
            False, False, 0, False, False, 0, 0,
            False, 0.0, 0.0, 0, 0, True, True, True, 0.0, False, False, 0.0, 0,
        )),
        ("InsertProtrusionSwept3", (
            False, False, 0, False, False, 0, 0, False, 0.0, 0.0, 0, 0, True, True, True,
        )),
        ("InsertProtrusionSwept2", (False, False, 0, False, False, 0, 0)),
    ])
    return report.value, report.signature or report.diagnostic()


def _make_loft(doc, manager, profiles):
    if not profiles or len(profiles) < 2:
        raise ToolError(
            "INVALID_PARAMETER",
            f"loft requires at least 2 profile sketch names, got {len(profiles or [])}.",
            hint='e.g. profiles=["Sketch1", "Sketch2"]',
        )

    _exit_sketch_capturing_profile(doc)
    selection.clear(doc)
    for index, name in enumerate(profiles):
        if not selection.select_by_id(doc, str(name), enums.SEL_STRING_SKETCH, append=index > 0):
            raise ToolError(
                "SKETCH_NOT_FOUND",
                f"Profile sketch '{name}' (profiles[{index}]) not found.",
            )

    report = compat.invoke_first(manager, [
        ("InsertProtrusionBlend2", (
            False, False, False, 0.1, 0, 0, 0.0, 0.0, False, False,
            False, 0.0, 0.0, 0, True, True, True, 0,
        )),
        ("InsertProtrusionBlend", (
            False, False, False, 0.1, 0, 0, 0.0, 0.0, False, False, False,
        )),
    ])
    return report.value, report.signature or report.diagnostic()


# ---------------------------------------------------------------------------
# add_edge_feature -- fillet / chamfer
# ---------------------------------------------------------------------------

def add_edge_feature(
    session: SolidWorksSession,
    feature_type: str,
    size: float,
    *,
    edge_indices: list | None = None,
    edge_points: list | None = None,
    face_indices: list | None = None,
    chamfer_type: str = "distance_angle",
    angle: float = 45.0,
    distance2: float | None = None,
    flip: bool = False,
    propagate: bool = True,
    unit: str | None = None,
    converter: units.UnitConverter | None = None,
) -> dict:
    """Round or chamfer solid-body edges (or all edges of a face).

    Merges the reference project's ``fillet_edges`` and ``chamfer_edges``, and
    fixes their central weakness: they required the user to pre-select edges in
    the GUI (or drop into ``execute_python``) because they had no selection
    mechanism of their own.

    Selection priority is ``edge_indices`` (robust), then ``face_indices`` (every
    edge of those faces), then ``edge_points`` (coordinate picks). Indices come
    from ``analyze_model(analysis_type="edges"|"faces")``. Prefer them: a
    coordinate pick cannot disambiguate crowded or concave edges.

    Args:
        feature_type: ``"fillet"`` or ``"chamfer"``.
        size: Fillet radius, or the chamfer's first-face setback, in ``unit``.
        edge_indices: Edge indices from ``analyze_model(edges)``.
        face_indices: Face indices -- fillets or chamfers every edge of each face.
        edge_points: ``[[x, y, z], ...]`` points on the target edges.
        chamfer_type: ``"distance_angle"`` (setback plus ``angle``) or
            ``"distance_distance"`` (``size`` plus ``distance2``, directional).
        angle: Chamfer angle in degrees for ``distance_angle``.
        distance2: Second-face setback for ``distance_distance``, in ``unit``.
        flip: Swap which face receives which setback / reverse the direction.
        propagate: Continue the fillet around tangent edges.
        unit: Unit for ``size`` and ``distance2``.
    """
    doc = session.require_doc("PART")
    converter = converter or units.UnitConverter(session.config.default_unit)
    kind = one_of(feature_type, "feature_type", EDGE_FEATURE_TYPES)
    require_positive(size, "size")

    size_m = converter.to_meters(size, unit)
    manager = pm(doc, "FeatureManager", default=None)

    # Edge features act on the solid, never inside a sketch.
    _exit_sketch_capturing_profile(doc)
    selection.clear(doc)

    if edge_indices:
        ok, error = selection.select_edges_by_index(doc, [int(i) for i in edge_indices])
        if not ok:
            raise ToolError(
                "EDGE_NOT_FOUND", error,
                hint="Call analyze_model(analysis_type='edges') for valid indices.",
            )
        picked = f"{len(edge_indices)} edge(s) by index"
    elif face_indices:
        ok, error = selection.select_faces_by_index(doc, [int(i) for i in face_indices])
        if not ok:
            raise ToolError("FACE_NOT_FOUND", error)
        picked = f"{len(face_indices)} face(s) by index"
    elif edge_points:
        for index, point in enumerate(edge_points):
            if len(point) < 3:
                raise ToolError(
                    "INVALID_PARAMETER", f"edge_points[{index}] needs x, y and z."
                )
            x, y, z = converter.points_to_meters(point[:3], unit)
            if not selection.select_by_id(
                doc, "", enums.SEL_STRING_EDGE, x, y, z, append=index > 0
            ):
                raise ToolError(
                    "EDGE_NOT_FOUND",
                    f"No edge found at or near edge_points[{index}] = {list(point)}.",
                    hint="Prefer edge_indices from analyze_model(edges) -- coordinate picks "
                         "cannot disambiguate crowded or concave edges.",
                )
        picked = f"{len(edge_points)} edge(s) by coordinate"
    else:
        raise ToolError(
            "MISSING_PARAMETER",
            "Provide edge_indices, face_indices, or edge_points.",
            hint="Run analyze_model(analysis_type='edges') first, then pass the indices you want.",
        )

    if kind == "fillet":
        # UniformRadius is not optional: with Propagate alone, R1 is left
        # unapplied and FeatureFillet3 returns null on recent builds.
        options = enums.FILLET_UNIFORM_RADIUS | (enums.FILLET_PROPAGATE if propagate else 0)
        report = compat.invoke_first(manager, [
            ("FeatureFillet3", (
                options, size_m, 0.0, 0.0, enums.FILLET_TYPE_SIMPLE, 0, 0,
                nothing(), nothing(), nothing(), nothing(), nothing(), nothing(), nothing(),
            )),
            ("FeatureFillet2", (
                options, size_m, 0.0, 0.0, enums.FILLET_TYPE_SIMPLE, 0, 0,
                nothing(), nothing(), nothing(), nothing(),
            )),
            ("SimpleFillet2", (0, size_m)),
        ])
        detail = {"radius": converter.describe(size_m, unit), "propagate": propagate}
    else:
        chamfer_mode = one_of(chamfer_type, "chamfer_type",
                              {"distance_angle", "distance_distance", "equal_distance"})
        options = enums.CHAMFER_FLIP_DIRECTION if flip else 0

        if chamfer_mode == "distance_distance":
            second = require_positive(
                distance2, "distance2",
                hint="chamfer_type='distance_distance' needs the second-face setback.",
            )
            second_m = converter.to_meters(second, unit)
            report = compat.invoke_first(manager, [
                ("InsertFeatureChamfer", (
                    options, enums.CHAMFER_DISTANCE_DISTANCE, size_m, 0.0, second_m,
                    0.0, 0.0, 0.0,
                )),
            ])
            detail = {
                "distance1": converter.describe(size_m, unit),
                "distance2": converter.describe(second_m, unit),
                "chamfer_type": chamfer_mode,
                "flip": flip,
            }
        elif chamfer_mode == "equal_distance":
            report = compat.invoke_first(manager, [
                ("InsertFeatureChamfer", (
                    options, enums.CHAMFER_EQUAL_DISTANCE, size_m, 0.0, size_m, 0.0, 0.0, 0.0,
                )),
            ])
            detail = {"distance": converter.describe(size_m, unit), "chamfer_type": chamfer_mode}
        else:
            angle_rad = units.deg_to_rad(angle)
            report = compat.invoke_first(manager, [
                ("InsertFeatureChamfer", (
                    options, enums.CHAMFER_ANGLE_DISTANCE, size_m, angle_rad,
                    0.0, 0.0, 0.0, 0.0,
                )),
            ])
            detail = {
                "distance": converter.describe(size_m, unit),
                "angle_degrees": angle,
                "chamfer_type": chamfer_mode,
                "flip": flip,
            }

    if not report.ok:
        raise SolidWorksError(
            "FEATURE_FAILED",
            f"The {kind} returned null. The selected edges may belong to no solid body, or "
            f"the size may be too large for the local geometry "
            f"(a fillet radius cannot exceed the adjacent face's smallest dimension).",
            {"selected": picked, "attempts": report.diagnostic()},
        )

    _commit(session, doc)
    return {
        "feature": compat.feature_name(report.value),
        "feature_type": kind,
        "selected": picked,
        "api": report.signature,
        "body": selection.body_summary(doc),
        **detail,
    }


# ---------------------------------------------------------------------------
# create_rib
# ---------------------------------------------------------------------------

def create_rib(
    session: SolidWorksSession,
    thickness: float,
    *,
    two_sided: bool = True,
    reverse_thickness: bool = False,
    reverse_material: bool = False,
    normal_to_sketch: bool = False,
    draft_angle: float = 0.0,
    unit: str | None = None,
    converter: units.UnitConverter | None = None,
) -> dict:
    """Create a rib from the active sketch, which is consumed as the profile.

    The profile is **open** geometry -- typically a single line whose extensions
    meet existing walls, such as the diagonal of an L-bracket. SolidWorks extends
    and thickens it.

    ``InsertRib`` returns void, so creation is verified by comparing the last
    tree feature name before and after. When nothing appeared, the material
    direction is flipped and retried -- the usual cause is that the chosen side
    had nothing to fill.

    Args:
        thickness: Rib thickness in ``unit``.
        two_sided: Thicken symmetrically about the sketch.
        reverse_thickness: Single-sided only -- thicken the other side.
        reverse_material: Flip which way the rib material grows.
        normal_to_sketch: Extrude normal to the sketch instead of parallel to it.
        draft_angle: Draft in degrees, 0 for none.
        unit: Unit for ``thickness``.
    """
    doc = session.require_doc("PART")
    converter = converter or units.UnitConverter(session.config.default_unit)
    require_positive(thickness, "thickness")
    thickness_m = converter.to_meters(thickness, unit)

    sketch_manager = pm(doc, "SketchManager", default=None)
    sketch = pm(sketch_manager, "ActiveSketch", default=None) if sketch_manager else None
    if sketch is None:
        raise ToolError(
            "SKETCH_NOT_ACTIVE",
            "create_rib consumes the active sketch, but no sketch is open.",
            hint="Call create_sketch, draw the open rib profile (usually one line), then create_rib.",
        )
    profile_name = compat.feature_name(sketch, default=None)

    manager = pm(doc, "FeatureManager", default=None)
    before = compat.last_feature_name(doc)

    compat.invoke_any(sketch_manager, [("InsertSketch", (True,))])
    draft_rad = units.deg_to_rad(draft_angle)
    has_draft = abs(draft_rad) > 1e-12

    def attempt(material_flip: bool) -> str | None:
        _reselect_profile(doc, profile_name)
        compat.invoke_any(manager, [
            ("InsertRib", (
                two_sided, reverse_thickness, thickness_m, 0,
                material_flip, has_draft, bool(draft_angle > 0), draft_rad,
                normal_to_sketch, False,
            )),
        ])
        compat.invoke_any(doc, [("EditRebuild3", ())])
        return compat.last_feature_name(doc)

    after = attempt(reverse_material)
    flipped = False
    if after == before:
        logger.debug("rib created nothing; retrying with flipped material direction")
        after = attempt(not reverse_material)
        flipped = after != before

    if after == before:
        raise SolidWorksError(
            "RIB_FAILED",
            "InsertRib created no feature with either material direction. The profile must be "
            "open geometry whose extensions reach existing walls, and the thickness must fit "
            "the surrounding material.",
            {"profile_sketch": profile_name, "thickness_m": thickness_m},
        )

    return {
        "feature": after,
        "profile_sketch": profile_name,
        "thickness": converter.describe(thickness_m, unit),
        "two_sided": two_sided,
        "material_direction_flipped": flipped,
        "body": selection.body_summary(doc),
    }


# ---------------------------------------------------------------------------
# create_shell -- neither reference project had this
# ---------------------------------------------------------------------------

def create_shell(
    session: SolidWorksSession,
    thickness: float,
    *,
    face_indices: list | None = None,
    outward: bool = False,
    unit: str | None = None,
    converter: units.UnitConverter | None = None,
) -> dict:
    """Hollow the solid to a uniform wall, optionally removing faces.

    A standard modelling operation for any cast or moulded part, and absent from
    both reference projects.

    ``InsertFeatureShell`` lives on ``IModelDoc2`` itself, **not**
    ``IFeatureManager`` (verified via the interface's own type info -- calling it on
    the feature manager raises "not exposed"). It also returns void regardless of
    outcome, the same contract as ``InsertRib``, so success is confirmed by
    comparing the last tree feature name before and after rather than by its
    return value.

    Args:
        thickness: Wall thickness in ``unit``.
        face_indices: Faces to remove (open the shell through), from
            ``analyze_model(faces)``. Omit for a closed hollow body.
        outward: Add the wall outward instead of inward, growing the part.
        unit: Unit for ``thickness``.
    """
    doc = session.require_doc("PART")
    converter = converter or units.UnitConverter(session.config.default_unit)
    require_positive(thickness, "thickness")
    thickness_m = converter.to_meters(thickness, unit)

    _exit_sketch_capturing_profile(doc)
    selection.clear(doc)

    removed = 0
    if face_indices:
        ok, error = selection.select_faces_by_index(doc, [int(i) for i in face_indices])
        if not ok:
            raise ToolError("FACE_NOT_FOUND", error)
        removed = len(face_indices)

    before = compat.last_feature_name(doc)
    compat.invoke_any(doc, [("InsertFeatureShell", (thickness_m, outward))])
    _commit(session, doc)
    after = compat.last_feature_name(doc)

    if after == before:
        raise SolidWorksError(
            "SHELL_FAILED",
            "InsertFeatureShell created no feature. The wall thickness may exceed what the "
            "geometry allows (it must be smaller than half the part's thinnest section), or a "
            "selected face may not be removable.",
            {"thickness_m": thickness_m},
        )

    return {
        "feature": after,
        "thickness": converter.describe(thickness_m, unit),
        "faces_removed": removed,
        "outward": outward,
        "body": selection.body_summary(doc),
    }


# ---------------------------------------------------------------------------
# create_hole -- neither reference project had this
# ---------------------------------------------------------------------------

def create_hole(
    session: SolidWorksSession,
    face_index: int,
    position: list,
    diameter: float,
    *,
    depth: float | None = None,
    through_all: bool = False,
    unit: str | None = None,
    converter: units.UnitConverter | None = None,
) -> dict:
    """Cut a simple round hole on a face at a given point.

    ``SimpleHole2`` in one call, versus the sketch-circle-then-cut-extrude
    sequence both reference projects required. The hole stays a recognisable
    Hole feature in the tree, so drawings can auto-generate hole callouts for it.

    ``SimpleHole2`` itself carries **no position parameter at all** -- verified
    live via the interface's own type info, it is shaped exactly like
    ``FeatureExtrusion3`` (diameter, end conditions, draft, no coordinates). The
    hole's location is instead whatever point the face was selected *at*: this
    mirrors the SolidWorks UI, where clicking a spot on a face is what places the
    hole there. So ``position`` both validates ``face_index`` and IS the
    placement mechanism -- selection uses coordinates deliberately here, the one
    exception to this server's usual index-first selection rule.

    Args:
        face_index: Target face, from ``analyze_model(faces)``. Cross-checked
            against ``position`` after selection; a mismatch is reported clearly
            rather than silently drilling the wrong face.
        position: ``[x, y, z]`` -- the hole centre, in model coordinates, on that face.
        diameter: Hole diameter in ``unit``.
        depth: Blind depth in ``unit``. Required unless ``through_all``.
        through_all: Cut through the whole body; ``depth`` is ignored.
        unit: Unit for ``diameter``, ``depth`` and ``position``.
    """
    doc = session.require_doc("PART")
    converter = converter or units.UnitConverter(session.config.default_unit)
    require_positive(diameter, "diameter")
    if not through_all:
        require_positive(
            depth, "depth", hint="Pass through_all=true for a through hole instead."
        )
    if len(position) < 3:
        raise ToolError("INVALID_PARAMETER", "position needs x, y and z.")

    diameter_m = converter.to_meters(diameter, unit)
    depth_m = converter.to_meters(depth, unit) if depth else 0.0
    x, y, z = converter.points_to_meters(position[:3], unit)

    _exit_sketch_capturing_profile(doc)
    selection.clear(doc)

    # Coordinate-based pick: the click point IS the hole's placement, so this
    # cannot go through the usual index-based face selection.
    if not selection.select_by_id(doc, "", enums.SEL_STRING_FACE, x, y, z):
        raise ToolError(
            "FACE_NOT_FOUND",
            f"No face found at or near {list(position[:3])}.",
            hint="position must lie exactly on the target face. Call "
                 "analyze_model(analysis_type='faces') for a point known to be on it.",
        )

    # Confirm the click landed on the face the caller intended -- a position that
    # is technically on *a* face but not face_index would otherwise drill silently
    # in the wrong place.
    faces = selection.flatten_faces(doc)
    manager_sel = pm(doc, "SelectionManager", default=None)
    picked = pm(manager_sel, "GetSelectedObject6", 1, -1, default=None) if manager_sel else None
    picked_index = selection.index_of_entity(faces, picked) if picked is not None else -1
    if picked_index != int(face_index):
        raise ToolError(
            "FACE_MISMATCH",
            f"position {list(position[:3])} resolved to face index {picked_index}, "
            f"not the requested face_index {face_index}.",
            hint="Use the point reported by analyze_model(analysis_type='faces') for the "
                 "face you actually meant, or pass its real face_index.",
            details={"resolved_face_index": picked_index, "requested_face_index": face_index},
        )

    end_condition = enums.END_THROUGH_ALL if through_all else enums.END_BLIND
    manager = pm(doc, "FeatureManager", default=None)
    # Verified 23-argument SimpleHole2 shape (no position args at all):
    # Dia, Sd, Flip, Dir, T1, T2, D1, D2, Dchk1, Dchk2, Ddir1, Ddir2, Dang1, Dang2,
    # OffsetReverse1/2, TranslateSurface1/2, UseFeatScope, UseAutoSelect,
    # AssemblyFeatureScope, AutoSelectComponents, PropagateFeatureToParts.
    report = compat.invoke_first(manager, [
        ("SimpleHole2", (
            diameter_m, True, False, False, end_condition, 0, depth_m, 0.0,
            False, False, False, False, 0.0, 0.0,
            False, False, False, False,
            True, True, False, False, False,
        )),
        # SimpleHole (pre-2014): same leading shape, minus the trailing 3
        # assembly-scope arguments.
        ("SimpleHole", (
            diameter_m, True, False, False, end_condition, 0, depth_m, 0.0,
            False, False, False, False, 0.0, 0.0,
            False, False, False, False,
            True, True,
        )),
    ])

    if not report.ok:
        raise SolidWorksError(
            "HOLE_FAILED",
            "SimpleHole2 returned null even though the face was selected. The diameter may not "
            "fit within the face's bounds at that point.",
            {"attempts": report.diagnostic()},
        )

    _commit(session, doc)
    return {
        "feature": compat.feature_name(report.value),
        "diameter": converter.describe(diameter_m, unit),
        "depth": None if through_all else converter.describe(depth_m, unit),
        "through_all": through_all,
        "face_index": face_index,
        "body": selection.body_summary(doc),
    }


# ---------------------------------------------------------------------------
# add_reference_geometry
# ---------------------------------------------------------------------------

def add_reference_geometry(
    session: SolidWorksSession,
    geometry_type: str,
    *,
    base_plane: str | None = None,
    offset: float = 0.0,
    entity1: str | None = None,
    entity1_type: str = "PLANE",
    entity2: str | None = None,
    entity2_type: str = "PLANE",
    point: list | None = None,
    hide: bool = True,
    unit: str | None = None,
    converter: units.UnitConverter | None = None,
) -> dict:
    """Create a reference plane, axis or point.

    Reference axes are a prerequisite for circular patterns; offset planes are how
    you sketch away from the default three.

    Args:
        geometry_type: ``"plane"``, ``"axis"`` or ``"point"``.
        base_plane: Plane only -- the plane to offset from.
        offset: Plane only -- offset distance in ``unit``. **Signed**: a negative
            value offsets the other way (``InsertRefPlane``'s distance argument is
            unsigned, so the sign is translated into the flip constraint bit --
            without that, a negative offset lands the new plane on top of its
            parent).
        entity1: Axis only -- first plane or edge name.
        entity1_type: ``"PLANE"`` or ``"EDGE"``.
        entity2: Axis only -- second plane or edge name.
        entity2_type: ``"PLANE"`` or ``"EDGE"``.
        point: Point only -- ``[x, y, z]`` of an existing vertex.
        hide: Hide the created geometry so it does not clutter the view. Selection
            by name still works on hidden reference geometry.
        unit: Unit for ``offset`` and ``point``.
    """
    doc = session.require_doc("PART", "ASSEMBLY")
    converter = converter or units.UnitConverter(session.config.default_unit)
    kind = one_of(geometry_type, "geometry_type", REFERENCE_TYPES)
    manager = pm(doc, "FeatureManager", default=None)

    _exit_sketch_capturing_profile(doc)
    selection.clear(doc)

    if kind == "plane":
        require(
            base_plane, "base_plane",
            hint="Name the plane to offset from, e.g. base_plane='Front Plane'.",
        )
        if not selection.select_plane(doc, base_plane):
            raise ToolError(
                "PLANE_NOT_FOUND",
                f"No plane named '{base_plane}'.",
                hint="Use a default plane name or one created earlier.",
            )
        offset_m = converter.to_meters(offset, unit)
        constraint = enums.REFPLANE_DISTANCE
        if offset_m < 0:
            constraint |= enums.REFPLANE_OPTION_FLIP
        report = compat.invoke_first(manager, [
            ("InsertRefPlane", (constraint, abs(offset_m), 0, 0, 0, 0)),
        ])
        detail = {"base_plane": base_plane, "offset": converter.describe(offset_m, unit)}

    elif kind == "axis":
        require(entity1, "entity1", hint="e.g. entity1='Top Plane'")
        require(entity2, "entity2", hint="e.g. entity2='Right Plane'")
        for index, (name, entity_type, mark) in enumerate(
            ((entity1, entity1_type, 1), (entity2, entity2_type, 2))
        ):
            upper = str(entity_type).upper()
            selected = (
                selection.select_plane(doc, name, append=index > 0, mark=mark)
                if upper == "PLANE"
                else selection.select_by_id(doc, name, upper, append=index > 0, mark=mark)
            )
            if not selected:
                raise ToolError(
                    "ENTITY_NOT_FOUND",
                    f"Entity '{name}' of type '{upper}' not found.",
                )
        before = compat.last_feature_name(doc)
        report = compat.invoke_bool(doc, [("InsertAxis2", (False,)), ("InsertAxis", ())])
        if report.ok:
            # InsertAxis2 returns a bool, so the created feature comes from the tree.
            report.value = compat.find_feature(doc, compat.last_feature_name(doc) or "")
            if compat.last_feature_name(doc) == before:
                report.value = None
        detail = {"entity1": entity1, "entity2": entity2}

    else:  # point
        if not point or len(point) < 3:
            raise ToolError("INVALID_PARAMETER", "point needs [x, y, z] of an existing vertex.")
        x, y, z = converter.points_to_meters(point[:3], unit)
        if not selection.select_by_id(doc, "", enums.SEL_STRING_VERTEX, x, y, z):
            raise ToolError(
                "VERTEX_NOT_FOUND",
                f"No vertex found at {list(point)}.",
                hint="The coordinates must match an existing vertex. "
                     "analyze_model(analysis_type='edges') reports edge endpoints.",
            )
        report = compat.invoke_first(manager, [
            ("InsertReferencePoint", (enums.REFPOINT_INTERSECTION, 0, 0.0, 1)),
        ])
        detail = {"point": point}

    if not report.ok or report.value is None:
        raise SolidWorksError(
            "REFERENCE_GEOMETRY_FAILED",
            f"Could not create the reference {kind}. Verify the referenced entities are valid "
            f"and no sketch is open.",
            {"attempts": report.diagnostic()},
        )

    name = compat.feature_name(report.value)

    if hide and kind in {"plane", "axis"}:
        # Reference geometry is scaffolding for downstream features; leaving it
        # visible clutters the model view. Selection by name still works when
        # hidden, so nothing downstream breaks. Best effort only.
        try:
            selection.clear(doc)
            if selection.select_feature(report.value):
                compat.invoke_any(doc, [("BlankRefGeom", ())])
            selection.clear(doc)
        except Exception:
            pass

    return {"feature": name, "geometry_type": kind, "hidden": hide and kind != "point", **detail}


# ---------------------------------------------------------------------------
# create_pattern
# ---------------------------------------------------------------------------

def create_pattern(
    session: SolidWorksSession,
    pattern_type: str,
    *,
    features: list | None = None,
    count: int = 2,
    spacing: float | None = None,
    direction: str = "X",
    flip: bool = False,
    count2: int = 1,
    spacing2: float | None = None,
    axis: str | None = None,
    angle: float = 90.0,
    equal_spacing: bool = True,
    plane: str = "Right Plane",
    geometry_pattern: bool = False,
    unit: str | None = None,
    converter: units.UnitConverter | None = None,
) -> dict:
    """Repeat features linearly, circularly, or mirror them about a plane.

    Args:
        pattern_type: ``"linear"``, ``"circular"`` or ``"mirror"``.
        features: Seed feature names, e.g. ``["Boss-Extrude1"]``. Linear and
            circular patterns use the first; mirror uses all of them.
        count: Instances **including** the seed.
        spacing: Distance between instances in ``unit`` (linear).
        direction: ``"X"``, ``"Y"`` or ``"Z"`` -- the linear pattern direction,
            resolved through the corresponding default plane's normal.
        flip: Pattern toward the negative axis.
        count2: Second-direction instance count (linear).
        spacing2: Second-direction spacing in ``unit``.
        axis: Circular only -- the reference axis name, e.g. ``"Axis1"``. Create
            one first with ``add_reference_geometry(geometry_type="axis")``.
        angle: Circular only, in degrees. With ``equal_spacing`` this is the
            **total** spread (360 for a full ring); without it, the angle
            **between** adjacent instances.
        equal_spacing: See ``angle``.
        plane: Mirror only -- the mirror plane name.
        geometry_pattern: Pattern the geometry without re-solving each feature.
            Faster, but returns null on some configurations, so it is not the
            default.
        unit: Unit for ``spacing`` and ``spacing2``.
    """
    doc = session.require_doc("PART")
    converter = converter or units.UnitConverter(session.config.default_unit)
    kind = one_of(pattern_type, "pattern_type", PATTERN_TYPES)

    if not features:
        raise ToolError(
            "MISSING_PARAMETER",
            "features is required -- the feature name(s) to pattern.",
            hint='e.g. features=["Boss-Extrude1"]. analyze_model(analysis_type=\'features\') '
                 'lists the tree.',
        )

    _exit_sketch_capturing_profile(doc)
    manager = pm(doc, "FeatureManager", default=None)

    if kind == "linear":
        seed = str(features[0])
        spacing_m = converter.to_meters(spacing if spacing is not None else 10.0, unit)
        spacing2_m = converter.to_meters(spacing2 if spacing2 is not None else 10.0, unit)

        selection.clear(doc)
        if not selection.select_feature_by_name(doc, seed, mark=4):
            raise ToolError(
                "FEATURE_NOT_FOUND",
                f"Seed feature '{seed}' not found.",
                hint="The name must match the feature tree exactly.",
                details={"available": compat.feature_names(doc)},
            )

        # Direction comes from a default plane's normal: X->Right, Y->Top, Z->Front.
        direction_plane = {"Y": "Top Plane", "Z": "Front Plane"}.get(
            str(direction).upper(), "Right Plane"
        )
        if not selection.select_plane(doc, direction_plane, append=True, mark=1):
            raise ToolError(
                "DIRECTION_NOT_FOUND",
                f"Could not resolve the direction plane '{direction_plane}'.",
            )

        # DName1/DName2 are BSTR dimension names, not IDispatch. Passing a typed
        # null dispatch there raises "Type mismatch" on every signature -- an empty
        # string is how you say "no driving dimension".
        report = compat.invoke_first(manager, [
            ("FeatureLinearPattern4", (
                int(count), spacing_m, int(count2), spacing2_m, flip, False,
                "", "", geometry_pattern, False, False, False, False,
            )),
            ("FeatureLinearPattern3", (
                int(count), spacing_m, int(count2), spacing2_m, flip, False,
                "", "", geometry_pattern, False,
            )),
            ("FeatureLinearPattern2", (
                int(count), spacing_m, int(count2), spacing2_m, flip, False,
                "", "", geometry_pattern,
            )),
        ])
        detail = {
            "seed": seed,
            "count": count,
            "spacing": converter.describe(spacing_m, unit),
            "direction": str(direction).upper(),
            "count2": count2,
        }

    elif kind == "circular":
        seed = str(features[0])
        require(
            axis, "axis",
            hint="Create a reference axis first with "
                 "add_reference_geometry(geometry_type='axis'), then pass its name.",
        )
        selection.clear(doc)
        if not selection.select_feature_by_name(doc, seed, mark=4):
            raise ToolError(
                "FEATURE_NOT_FOUND", f"Seed feature '{seed}' not found.",
                details={"available": compat.feature_names(doc)},
            )
        selected_axis = (
            selection.select_by_id(doc, axis, enums.SEL_STRING_AXIS, append=True, mark=1)
            or selection.select_by_id(
                doc, axis, enums.SEL_STRING_REFERENCE_CURVE, append=True, mark=1)
        )
        if not selected_axis:
            raise ToolError(
                "AXIS_NOT_FOUND",
                f"Reference axis '{axis}' not found.",
                hint="Create one with add_reference_geometry(geometry_type='axis').",
            )

        angle_rad = units.deg_to_rad(angle)
        # DName is a BSTR dimension name -- see the linear branch.
        report = compat.invoke_first(manager, [
            ("FeatureCircularPattern5", (
                int(count), angle_rad, False, "", False, equal_spacing, False, False,
            )),
            ("FeatureCircularPattern4", (
                int(count), angle_rad, False, "", False, equal_spacing, False,
            )),
            ("FeatureCircularPattern3", (
                int(count), angle_rad, False, "", False, equal_spacing,
            )),
        ])
        detail = {
            "seed": seed,
            "count": count,
            "axis": axis,
            "angle_degrees": angle,
            "equal_spacing": equal_spacing,
        }

    else:  # mirror
        names = [str(name) for name in features]
        with _FlatPatternRollback(doc):
            selection.clear(doc)
            for name in names:
                # Mirror reads features at mark 1 and the plane at mark 2.
                if not selection.select_feature_by_name(doc, name, append=True, mark=1):
                    raise ToolError(
                        "FEATURE_NOT_FOUND",
                        f"Feature '{name}' not found for mirroring.",
                        details={"available": compat.feature_names(doc)},
                    )
            if not selection.select_plane(doc, plane, append=True, mark=2):
                raise ToolError(
                    "PLANE_NOT_FOUND",
                    f"Mirror plane '{plane}' not found.",
                    hint="Use a default plane name or a plane created by add_reference_geometry.",
                )
            report = compat.invoke_first(manager, [
                ("InsertMirrorFeature2", (False, geometry_pattern, False, False, 0)),
                ("InsertMirrorFeature", (False, geometry_pattern, False, False)),
            ])
        detail = {"mirrored": names, "plane": plane}

    if not report.ok:
        raise SolidWorksError(
            "PATTERN_FAILED",
            f"The {kind} pattern returned null. Common causes: instances that would land "
            f"disconnected from the body (SolidWorks skips those and can fail the feature), "
            f"a seed that is not a solid feature, or an invalid direction/axis.",
            {"attempts": report.diagnostic(), **detail},
        )

    _commit(session, doc)
    return {
        "feature": compat.feature_name(report.value),
        "pattern_type": kind,
        "api": report.signature,
        "body": selection.body_summary(doc),
        **detail,
    }


# ---------------------------------------------------------------------------
# set_material
# ---------------------------------------------------------------------------

def set_material(
    session: SolidWorksSession,
    material: str,
    library: str = "SolidWorks Materials",
    configuration: str = "",
) -> dict:
    """Assign a material to the active part.

    The material shows in mass properties and the feature tree, which makes it a
    prerequisite for any meaningful mass or centre-of-gravity figure.

    Args:
        material: Exact library name. SolidWorks is unforgiving here --
            ``"1060 Alloy"``, not ``"Aluminum 1060 Alloy"``; ``"AISI 1020"``;
            ``"ABS"``; ``"Nylon 6/10"``.
        library: Material database name.
        configuration: Configuration to apply to. Empty means all configurations.
    """
    doc = session.require_doc("PART")

    report = compat.invoke_any(doc, [
        ("SetMaterialPropertyName2", (configuration, library, material)),
        ("SetMaterialPropertyName", (library, material)),
    ])
    if not report.ok:
        raise SolidWorksError(
            "MATERIAL_FAILED",
            "No material-assignment API responded on this SolidWorks build.",
            {"attempts": report.diagnostic()},
        )

    database = byref_str()
    applied = pm(doc, "GetMaterialPropertyName2", configuration, database, default=None)
    if not applied:
        applied = pm(doc, "GetMaterialPropertyName", default=None)

    if not applied:
        raise SolidWorksError(
            "MATERIAL_NOT_FOUND",
            f"Material '{material}' was not found in library '{library}', so nothing was applied.",
            {
                "requested": material,
                "library": library,
                "hint": "Names must match the library exactly. Check the Material dialog in "
                        "SolidWorks for the precise spelling.",
            },
        )

    return {
        "material": str(applied),
        "library": database.value or library,
        "configuration": configuration or "all",
    }


# ---------------------------------------------------------------------------
# modify_dimension
# ---------------------------------------------------------------------------

def modify_dimension(
    session: SolidWorksSession,
    name: str,
    value: float,
    *,
    is_angle: bool = False,
    unit: str | None = None,
    converter: units.UnitConverter | None = None,
) -> dict:
    """Drive a named dimension to a new value -- the parametric-variant keystone.

    Pairs with ``analyze_model(analysis_type="features")``, which reports every
    feature's dimension full-names and current values. The effective value is
    read back after rebuilding, because an equation or a rebuild constraint may
    clamp what you asked for -- and silently getting a different number than you
    requested is the failure mode worth catching in-band.

    Args:
        name: Dimension full-name exactly as analysis reports it, e.g.
            ``"D1@Boss-Extrude1@bracket.Part"``.
        value: New value in ``unit``, or degrees when ``is_angle``.
        is_angle: Treat ``value`` as an angle.
        unit: Unit for a length value.
    """
    doc = session.active_doc()
    converter = converter or units.UnitConverter(session.config.default_unit)
    resolved_unit = units.parse_unit(unit, converter.default)

    _exit_sketch_capturing_profile(doc)

    dimension = pm(doc, "Parameter", name, default=None)
    if dimension is None:
        raise ToolError(
            "DIMENSION_NOT_FOUND",
            f"No dimension named '{name}'.",
            hint="Use the exact full-name from analyze_model(analysis_type='features'), "
                 "e.g. 'D1@Boss-Extrude1@part.Part'.",
        )

    si_value = units.deg_to_rad(value) if is_angle else converter.to_meters(value, resolved_unit)
    try:
        dimension.SystemValue = si_value
    except Exception as exc:
        raise SolidWorksError(
            "DIMENSION_SET_FAILED",
            f"Could not set '{name}' to {value}: {exc}. The dimension may be driven by an "
            f"equation or be read-only.",
        ) from None

    compat.invoke_any(doc, [("EditRebuild3", ())])

    effective = compat.dimension_value(dimension, default=si_value)

    drifted = abs(effective - si_value) > 1e-9
    return {
        "dimension": name,
        "requested": (
            {"value": value, "unit": "deg"} if is_angle
            else converter.describe(si_value, resolved_unit)
        ),
        "effective": (
            {"value": round(units.rad_to_deg(effective), 6), "unit": "deg"} if is_angle
            else converter.describe(effective, resolved_unit)
        ),
        "clamped": drifted,
        "note": (
            "SolidWorks applied a different value than requested -- an equation or "
            "relation is driving this dimension." if drifted else None
        ),
        "feature_errors": compat.error_count(doc),
        "body": selection.body_summary(doc) if session.doc_type(doc) == "PART" else None,
    }


# ---------------------------------------------------------------------------
# edit_feature
# ---------------------------------------------------------------------------

def edit_feature(
    session: SolidWorksSession,
    action: str,
    *,
    feature: str | None = None,
    new_name: str | None = None,
) -> dict:
    """Structurally edit a named feature: suppress, unsuppress, delete, rename,
    or move the rollback bar.

    Suppress, delete and rollback all change topology, so face and edge indices
    captured earlier will no longer refer to the same geometry -- re-run
    ``analyze_model`` afterwards.

    Args:
        action: ``"suppress"``, ``"unsuppress"``, ``"delete"``, ``"rename"``,
            ``"rollback_to"`` (roll the bar to just before ``feature``), or
            ``"rollback_end"`` (roll to the end of the tree).
        feature: Exact feature-tree name. Not needed for ``rollback_end``.
        new_name: Required for ``action="rename"``.
    """
    doc = session.active_doc()
    verb = one_of(action, "action", EDIT_ACTIONS)
    manager = pm(doc, "FeatureManager", default=None)

    _exit_sketch_capturing_profile(doc)

    if verb == "rollback_end":
        report = compat.invoke_bool(manager, [("EditRollback", (enums.ROLLBACK_TO_END, ""))])
        if not report.ok:
            raise SolidWorksError("ROLLBACK_FAILED", "Could not roll the tree forward to the end.")
        return {"action": verb, "features": len(compat.feature_names(doc))}

    require(feature, "feature", hint="The exact feature-tree name.")

    if verb == "rollback_to":
        report = compat.invoke_bool(manager, [
            ("EditRollback", (enums.ROLLBACK_TO_BEFORE_FEATURE, feature)),
        ])
        if not report.ok:
            raise ToolError(
                "ROLLBACK_FAILED",
                f"Could not roll back to before '{feature}'.",
                details={"available": compat.feature_names(doc)},
            )
        return {"action": verb, "feature": feature,
                "body": selection.body_summary(doc) if session.doc_type(doc) == "PART" else None}

    names: list[str] = []
    target = compat.find_feature(doc, feature, collect=names)
    if target is None:
        raise ToolError(
            "FEATURE_NOT_FOUND",
            f"No feature named '{feature}'.",
            hint="Names must match the feature tree exactly.",
            details={"available": names},
        )

    result_name = feature

    if verb in {"suppress", "unsuppress"}:
        state = enums.SUPPRESS_FEATURE if verb == "suppress" else enums.UNSUPPRESS_FEATURE
        report = compat.invoke_bool(target, [
            ("SetSuppression2", (state, enums.THIS_CONFIGURATION, nothing())),
            ("SetSuppression", (state,)),
        ])
        if not report.ok:
            raise SolidWorksError(
                "SUPPRESSION_FAILED",
                f"Could not {verb} '{feature}'. A dependent feature may block it.",
                {"attempts": report.diagnostic()},
            )

    elif verb == "rename":
        require(new_name, "new_name", hint="The new feature name.")
        try:
            target.Name = new_name
            result_name = new_name
        except Exception as exc:
            raise SolidWorksError(
                "RENAME_FAILED", f"Could not rename '{feature}': {exc}"
            ) from None

    else:  # delete
        # Selecting the resolved IFeature (rather than SelectByID2 with
        # "BODYFEATURE") deletes any feature type -- the typed path silently
        # fails for sketches and reference geometry.
        selection.clear(doc)
        if not selection.select_feature(target):
            raise ToolError(
                "FEATURE_NOT_FOUND", f"'{feature}' could not be selected for deletion."
            )
        compat.invoke_any(doc, [("EditDelete", ())])
        result_name = None

    compat.invoke_any(doc, [("EditRebuild3", ())])

    return {
        "action": verb,
        "feature": result_name,
        "previous_name": feature if verb == "rename" else None,
        "feature_errors": compat.error_count(doc),
        "body": selection.body_summary(doc) if session.doc_type(doc) == "PART" else None,
    }
