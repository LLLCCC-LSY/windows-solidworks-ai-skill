"""
Sheet-metal features.

This is the module that most needs the STA thread. The reliable modern pattern for
base flange and edge flange is ``CreateDefinition`` -> populate the feature-data
object -> ``CreateFeature``, and feature-data objects do not survive COM
marshalling from an MTA caller: they come back null with no error. That is why the
pure-Python reference project had no sheet-metal support at all, and why this one
does.

The legacy one-shot APIs (``InsertSheetMetalBaseFlange2``) are kept as fallbacks
for older builds, since ``CreateDefinition(swFmBaseFlange)`` is not available
everywhere.
"""

from __future__ import annotations

import logging

from ..com import compat, enums, selection
from ..com.compat import dispatch_array, nothing, pm
from ..com.connection import SolidWorksError, SolidWorksSession
from ..util import units
from ..util.result import ToolError, one_of, require_positive
from .sketches import read_sketch_plane

logger = logging.getLogger(__name__)

FEATURE_TYPES = {
    "base_flange",
    "edge_flange",
    "edge_flange_sketch",
    "edge_flange_finish",
    "sketched_bend",
    "flat_pattern",
    "fold",
}


def sheet_metal_feature(
    session: SolidWorksSession,
    feature_type: str,
    *,
    thickness: float | None = None,
    bend_radius: float | None = None,
    k_factor: float = 0.5,
    reverse_thickness: bool = False,
    symmetric_thickness: bool = False,
    edge_index: int | None = None,
    edge_point: list | None = None,
    flange_length: float | None = None,
    angle: float = 90.0,
    bend_position: str = "material_inside",
    use_default_radius: bool = False,
    flip: bool = False,
    fixed_face_index: int | None = None,
    fixed_point: list | None = None,
    clear_profile: bool = True,
    unit: str | None = None,
    converter: units.UnitConverter | None = None,
) -> dict:
    """Create or modify sheet-metal geometry on the active part.

    Args:
        feature_type:
            * ``base_flange`` -- convert the active sketch profile into the
              sheet-metal base. Needs ``thickness``.
            * ``edge_flange`` -- add a default-profile flange to an edge. Select it
              with ``edge_index`` (preferred) or ``edge_point``.
            * ``edge_flange_sketch`` -- step 1 of a **custom-profile** flange:
              generate the edge-linked profile sketch, optionally wipe the default
              rectangle, and leave it open for drawing. Returns the sketch's
              measured frame, because SolidWorks chooses that frame and the caller
              must place its 2D coordinates into it.
            * ``edge_flange_finish`` -- step 2: create the flange from the active
              custom profile sketch on the same edge.
            * ``sketched_bend`` -- bend the sheet about the bend line(s) in the
              active sketch. The pre-selected fixed face stays put; pick it with
              ``fixed_face_index`` (preferred) or ``fixed_point``.
            * ``flat_pattern`` -- unfold all bends.
            * ``fold`` -- re-fold (the inverse of ``flat_pattern``).
        thickness: Base flange sheet thickness in ``unit``.
        bend_radius: Bend radius in ``unit``. Defaults to the sheet thickness.
        k_factor: Bend-allowance K-factor for the base flange.
        reverse_thickness: Thicken to the opposite side of the sketch plane.
            This also sets the sheet's intrinsic orientation, which downstream
            bend directions fold against -- so when reproducing an existing part,
            match its flag rather than deriving it from face positions.
        symmetric_thickness: Thicken both ways (+/- t/2), mid-plane style.
        edge_index: Attach edge index from ``analyze_model(analysis_type="edges")``.
        edge_point: ``[x, y, z]`` on the attach edge (fallback selection).
        flange_length: Edge flange length in ``unit``.
        angle: Bend angle in degrees.
        bend_position: ``material_inside`` (default), ``material_outside``,
            ``bend_outside``, ``centerline``, or ``bend_sharp``.
        use_default_radius: Use the sheet's default bend radius.
        flip: Reverse the bend direction.
        fixed_face_index: Sketched bend -- the face that must not move.
        fixed_point: ``[x, y, z]`` on the fixed face (fallback selection).
        clear_profile: ``edge_flange_sketch`` only -- wipe the generated default
            rectangle so the caller can draw a custom outline.
        unit: Unit for all lengths in this call.
    """
    doc = session.require_doc("PART")
    converter = converter or units.UnitConverter(session.config.default_unit)
    kind = one_of(feature_type, "feature_type", FEATURE_TYPES)
    manager = pm(doc, "FeatureManager", default=None)
    sketch_manager = pm(doc, "SketchManager", default=None)

    if kind == "base_flange":
        return _base_flange(
            session, doc, manager, sketch_manager, converter, unit,
            thickness, bend_radius, k_factor, reverse_thickness, symmetric_thickness,
        )
    if kind == "edge_flange":
        return _edge_flange(
            session, doc, manager, converter, unit,
            edge_index, edge_point, flange_length, angle, bend_position,
            use_default_radius, bend_radius,
        )
    if kind == "edge_flange_sketch":
        return _edge_flange_sketch(
            session, doc, sketch_manager, converter, unit,
            edge_index, edge_point, angle, flip, clear_profile,
        )
    if kind == "edge_flange_finish":
        return _edge_flange_finish(
            session, doc, manager, sketch_manager, converter, unit,
            edge_index, edge_point, angle, bend_position, use_default_radius, bend_radius,
        )
    if kind == "sketched_bend":
        return _sketched_bend(
            session, doc, manager, sketch_manager, converter, unit,
            angle, bend_radius, use_default_radius, flip, bend_position,
            fixed_face_index, fixed_point,
        )
    return _set_flat_state(session, doc, flatten=(kind == "flat_pattern"))


# ---------------------------------------------------------------------------

def _base_flange(session, doc, manager, sketch_manager, converter, unit,
                 thickness, bend_radius, k_factor, reverse_thickness, symmetric):
    require_positive(thickness, "thickness", hint="The sheet thickness, e.g. thickness=2.")
    thickness_m = converter.to_meters(thickness, unit)
    radius_m = converter.to_meters(bend_radius, unit) if bend_radius else thickness_m

    sketch = pm(sketch_manager, "ActiveSketch", default=None) if sketch_manager else None
    if sketch is None:
        raise ToolError(
            "SKETCH_NOT_ACTIVE",
            "base_flange consumes the active sketch as the blank profile, but no sketch is open.",
            hint="Call create_sketch, draw the closed blank outline, then base_flange.",
        )
    profile_name = compat.feature_name(sketch, default=None)

    # A closed-profile base flange needs the WHOLE contour selected, not one
    # segment: selecting a single segment makes SolidWorks treat it as an open
    # profile, which then requires a non-zero extrude distance and otherwise
    # returns null. Selecting the sketch feature by name selects the contour.
    compat.invoke_any(sketch_manager, [("InsertSketch", (True,))])
    selection.clear(doc)
    if profile_name:
        selection.select_by_id(doc, profile_name, enums.SEL_STRING_SKETCH)

    feature = None
    route = None

    # Preferred: CreateDefinition gives a pre-populated IBaseFlangeFeatureData.
    # This path needs the STA thread; see the module docstring.
    definition = pm(manager, "CreateDefinition", enums.FEATURE_ID_BASE_FLANGE, default=None)
    if definition is not None:
        try:
            definition.OverrideThickness = True
            definition.Thickness = thickness_m
            # Set both flags: on a CLOSED profile the blank is an extrusion and
            # ReverseDirection is what flips its side, while ReverseThickness
            # alone has no effect. Setting both makes open profiles follow too.
            definition.ReverseThickness = reverse_thickness
            definition.ReverseDirection = reverse_thickness
            if symmetric:
                definition.SymmetricThickness = True
            definition.OverrideRadius = True
            definition.BendRadius = radius_m
            definition.OverrideKFactor = True
            definition.KFactor = k_factor
            feature = pm(manager, "CreateFeature", definition, default=None)
            route = "CreateDefinition + CreateFeature"
        except Exception as exc:
            logger.debug("base flange via CreateDefinition failed: %s", exc)

    if feature is None:
        report = compat.invoke_first(manager, [
            ("InsertSheetMetalBaseFlange2", (
                thickness_m, reverse_thickness, radius_m, 0.0, 0.0, False,
                enums.END_BLIND, enums.END_BLIND, 0, k_factor,
                True, enums.RELIEF_RECTANGULAR, 0.001, 0.001, 0.5, True,
                True, True, True,
            )),
            ("InsertSheetMetalBaseFlange", (
                thickness_m, reverse_thickness, radius_m, 0.0, 0.0, False,
                enums.END_BLIND, enums.END_BLIND, 0, k_factor,
            )),
        ])
        feature = report.value
        route = report.signature or report.diagnostic()

    if feature is None:
        raise SolidWorksError(
            "SHEET_METAL_FAILED",
            "The base flange returned null. Check the sketch is a single closed contour, and "
            "that the SolidWorks Sheet Metal capability is available on this install.",
            {"profile_sketch": profile_name, "route": route},
        )

    compat.invoke_any(doc, [("EditRebuild3", ())])
    return {
        "feature": compat.feature_name(feature),
        "feature_type": "base_flange",
        "profile_sketch": profile_name,
        "thickness": converter.describe(thickness_m, unit),
        "bend_radius": converter.describe(radius_m, unit),
        "k_factor": k_factor,
        "reverse_thickness": reverse_thickness,
        "symmetric_thickness": symmetric,
        "api": route,
        "body": selection.body_summary(doc),
    }


def _select_flange_edge(doc, converter, unit, edge_index, edge_point):
    """Resolve the attach edge, index-first. Returns the edge object."""
    if edge_index is not None:
        edge = selection.edge_at_index(doc, int(edge_index))
        if edge is None:
            edges = selection.flatten_edges(doc)
            raise ToolError(
                "EDGE_NOT_FOUND",
                f"edge_index {edge_index} is out of range (the part has {len(edges)} edges).",
                hint="Call analyze_model(analysis_type='edges') for valid indices.",
            )
        selection.clear(doc)
        selection.select_entity(edge, doc=doc)
        return edge

    if not edge_point or len(edge_point) < 3:
        raise ToolError(
            "MISSING_PARAMETER",
            "Provide edge_index (preferred) or edge_point=[x, y, z] for the attach edge.",
            hint="Run analyze_model(analysis_type='edges') and pass the index of the long "
                 "boundary edge of a sheet face -- not a thickness edge.",
        )

    x, y, z = converter.points_to_meters(edge_point[:3], unit)
    selection.clear(doc)
    if not selection.select_by_id(doc, "", enums.SEL_STRING_EDGE, x, y, z):
        raise ToolError(
            "EDGE_NOT_FOUND",
            f"No edge found at or near {list(edge_point)}.",
            hint="Prefer edge_index from analyze_model(edges).",
        )
    manager = pm(doc, "SelectionManager", default=None)
    return pm(manager, "GetSelectedObject6", 1, -1, default=None) if manager else None


def _edge_flange(session, doc, manager, converter, unit,
                 edge_index, edge_point, flange_length, angle, bend_position,
                 use_default_radius, bend_radius):
    length_m = converter.to_meters(flange_length if flange_length else 20.0, unit)
    angle_rad = units.deg_to_rad(angle)
    position = enums.FLANGE_POS_BY_NAME[
        one_of(bend_position, "bend_position", set(enums.FLANGE_POS_BY_NAME))
    ]

    edge = _select_flange_edge(doc, converter, unit, edge_index, edge_point)
    if edge is None:
        raise ToolError("EDGE_NOT_FOUND", "The attach edge could not be resolved.")

    # Edge flange is a genuinely two-step API: InsertSketchForEdgeFlange generates
    # the default profile sketch on the edge, then InsertSheetMetalEdgeFlange2
    # consumes the edge plus that sketch. Every one-shot variant returns null or
    # 'SketchNotSpecified' precisely because no profile sketch existed -- the API
    # does not generate one itself.
    profile = pm(doc, "InsertSketchForEdgeFlange", edge, angle_rad, False, default=None)
    if profile is None:
        raise SolidWorksError(
            "SHEET_METAL_FAILED",
            "InsertSketchForEdgeFlange returned null. The edge must be a straight boundary edge "
            "of a sheet face (a long edge, not a thickness edge).",
        )

    sketch_manager = pm(doc, "SketchManager", default=None)
    if pm(sketch_manager, "ActiveSketch", default=None) is not None:
        compat.invoke_any(sketch_manager, [("InsertSketch", (True,))])

    # Creating and exiting the sketch can clear the selection list; re-select via
    # the entity object so index and coordinate callers behave identically.
    selection.clear(doc)
    selection.select_entity(edge, doc=doc)

    options = (
        enums.EDGE_FLANGE_USE_RELIEF_RATIO | enums.EDGE_FLANGE_USE_DEFAULT_RELIEF
        | (enums.EDGE_FLANGE_USE_DEFAULT_RADIUS if use_default_radius else 0)
    )
    radius_m = converter.to_meters(bend_radius, unit) if bend_radius else 0.001

    report = compat.invoke_first(manager, [
        ("InsertSheetMetalEdgeFlange2", (
            dispatch_array([edge]), dispatch_array([profile]),
            options, angle_rad, radius_m, position, length_m,
            enums.RELIEF_RECTANGULAR, 0.5, 0.001, 0.001, 0, nothing(),
        )),
        ("InsertSheetMetalEdgeFlange", (
            options, angle_rad, radius_m, position, length_m,
            enums.RELIEF_RECTANGULAR, 0.5, 0.001, 0.001,
        )),
    ])

    if not report.ok:
        raise SolidWorksError(
            "SHEET_METAL_FAILED",
            "InsertSheetMetalEdgeFlange2 returned null. Check the edge belongs to a sheet-metal "
            "body and the flange length and angle are achievable.",
            {"attempts": report.diagnostic()},
        )

    feature = report.value
    length_applied = None
    # The flange length comes from the auto-generated profile sketch (a small
    # default), NOT from the length argument, whenever a sketch is supplied. Force
    # the requested length by editing the created feature's definition --
    # AccessSelections works on an existing feature, unlike a fresh
    # CreateDefinition.
    definition = pm(feature, "GetDefinition", default=None)
    if definition is not None:
        try:
            definition.AccessSelections(doc, nothing())
            definition.OffsetDistance = length_m
            if feature.ModifyDefinition(definition, doc, nothing()):
                length_applied = length_m
        except Exception as exc:
            logger.debug("edge flange length override failed: %s", exc)
            try:
                definition.ReleaseSelectionAccess()
            except Exception:
                pass

    compat.invoke_any(doc, [("EditRebuild3", ())])
    return {
        "feature": compat.feature_name(feature),
        "feature_type": "edge_flange",
        "flange_length": converter.describe(length_applied or length_m, unit),
        "length_override_applied": length_applied is not None,
        "angle_degrees": angle,
        "bend_position": bend_position,
        "api": report.signature,
        "body": selection.body_summary(doc),
    }


def _edge_flange_sketch(session, doc, sketch_manager, converter, unit,
                        edge_index, edge_point, angle, flip, clear_profile):
    edge = _select_flange_edge(doc, converter, unit, edge_index, edge_point)
    if edge is None:
        raise ToolError("EDGE_NOT_FOUND", "The attach edge could not be resolved.")

    profile = pm(doc, "InsertSketchForEdgeFlange", edge, units.deg_to_rad(angle), flip,
                 default=None)
    if profile is None:
        raise SolidWorksError(
            "SHEET_METAL_FAILED",
            "InsertSketchForEdgeFlange returned null. The edge must be a straight boundary edge "
            "of a sheet face.",
        )
    sketch_name = compat.feature_name(profile, default="Sketch")

    active = pm(sketch_manager, "ActiveSketch", default=None)
    if active is None:
        # InsertSketchForEdgeFlange creates the sketch but does not always leave
        # it in edit mode -- open it explicitly so the caller can draw.
        selection.clear(doc)
        selection.select_by_id(doc, sketch_name, enums.SEL_STRING_SKETCH)
        compat.invoke_any(doc, [("EditSketch", ())])
        active = pm(sketch_manager, "ActiveSketch", default=None)

    if active is None:
        raise SolidWorksError(
            "SHEET_METAL_FAILED",
            f"The generated profile sketch '{sketch_name}' could not be opened for editing.",
        )

    cleared = 0
    if clear_profile:
        segments = compat.as_list(pm(active, "GetSketchSegments", default=None))
        selection.clear(doc)
        for segment in segments:
            if selection.select_entity(segment, append=True, doc=doc):
                cleared += 1
        if cleared:
            compat.invoke_any(doc, [("EditDelete", ())])

    return {
        "feature_type": "edge_flange_sketch",
        "sketch": sketch_name,
        "cleared_default_segments": cleared,
        "plane": read_sketch_plane(active, session.config.precision),
        "next_step": "Draw the custom outline with add_sketch_entities using the returned "
                     "sketch frame, then call sheet_metal_feature("
                     "feature_type='edge_flange_finish') with the SAME edge_index.",
    }


def _edge_flange_finish(session, doc, manager, sketch_manager, converter, unit,
                        edge_index, edge_point, angle, bend_position,
                        use_default_radius, bend_radius):
    profile = pm(sketch_manager, "ActiveSketch", default=None) if sketch_manager else None
    if profile is None:
        raise ToolError(
            "SKETCH_NOT_ACTIVE",
            "edge_flange_finish needs the generated profile sketch to be open.",
            hint="Run sheet_metal_feature(feature_type='edge_flange_sketch') first, then draw "
                 "the profile, then finish.",
        )
    profile_name = compat.feature_name(profile, default="Sketch")
    compat.invoke_any(sketch_manager, [("InsertSketch", (True,))])

    edge = _select_flange_edge(doc, converter, unit, edge_index, edge_point)
    if edge is None:
        raise ToolError("EDGE_NOT_FOUND", "The attach edge could not be resolved.")

    position = enums.FLANGE_POS_BY_NAME[
        one_of(bend_position, "bend_position", set(enums.FLANGE_POS_BY_NAME))
    ]
    options = enums.EDGE_FLANGE_USE_RELIEF_RATIO | enums.EDGE_FLANGE_USE_DEFAULT_RELIEF
    if use_default_radius:
        options |= enums.EDGE_FLANGE_USE_DEFAULT_RADIUS
    radius_m = converter.to_meters(bend_radius, unit) if bend_radius else 0.001

    # The custom profile defines the flange outline and its length, so unlike the
    # default-profile path there is no length override afterwards.
    profile_feature = compat.find_feature(doc, profile_name) or profile
    report = compat.invoke_first(manager, [
        ("InsertSheetMetalEdgeFlange2", (
            dispatch_array([edge]), dispatch_array([profile_feature]),
            options, units.deg_to_rad(angle), radius_m, position, 0.0,
            enums.RELIEF_RECTANGULAR, 0.5, 0.001, 0.001, 0, nothing(),
        )),
    ])

    if not report.ok:
        raise SolidWorksError(
            "SHEET_METAL_FAILED",
            "InsertSheetMetalEdgeFlange2 returned null for the custom profile. The profile must "
            "be a valid open outline in the generated sketch's own frame.",
            {"profile_sketch": profile_name, "attempts": report.diagnostic()},
        )

    compat.invoke_any(doc, [("EditRebuild3", ())])
    return {
        "feature": compat.feature_name(report.value),
        "feature_type": "edge_flange_finish",
        "profile_sketch": profile_name,
        "angle_degrees": angle,
        "bend_position": bend_position,
        "body": selection.body_summary(doc),
    }


def _sketched_bend(session, doc, manager, sketch_manager, converter, unit,
                   angle, bend_radius, use_default_radius, flip, bend_position,
                   fixed_face_index, fixed_point):
    sketch = pm(sketch_manager, "ActiveSketch", default=None) if sketch_manager else None
    if sketch is None:
        raise ToolError(
            "SKETCH_NOT_ACTIVE",
            "sketched_bend needs the bend-line sketch to be open.",
            hint="Create a sketch on the sheet face, draw the bend line(s), then bend. "
                 "One feature is created with one bend per line.",
        )
    sketch_name = compat.feature_name(sketch, default="Sketch")

    angle_rad = units.deg_to_rad(angle)
    radius_m = converter.to_meters(bend_radius, unit) if bend_radius else 0.001
    position = enums.BEND_3D_POS_BY_NAME.get(str(bend_position).lower(), 0)

    feature = None
    # Two selection orders are tried: the UI flow selects the fixed face while
    # still inside the sketch; the recorded-macro flow exits first and selects the
    # sketch plus the face. Builds differ in which they accept.
    for attempt in range(2):
        if feature is not None:
            break
        append = attempt == 1
        if attempt == 1:
            if pm(sketch_manager, "ActiveSketch", default=None) is not None:
                compat.invoke_any(sketch_manager, [("InsertSketch", (True,))])
            selection.clear(doc)
            selection.select_by_id(doc, sketch_name, enums.SEL_STRING_SKETCH)

        if fixed_face_index is not None:
            ok, error = selection.select_faces_by_index(
                doc, [int(fixed_face_index)], append=append
            )
            if not ok:
                raise ToolError("FACE_NOT_FOUND", error)
        elif fixed_point and len(fixed_point) >= 3:
            x, y, z = converter.points_to_meters(fixed_point[:3], unit)
            if not selection.select_by_id(doc, "", enums.SEL_STRING_FACE, x, y, z, append=append):
                raise ToolError(
                    "FACE_NOT_FOUND",
                    f"No face found at or near {list(fixed_point)} for the fixed face.",
                )
        else:
            raise ToolError(
                "MISSING_PARAMETER",
                "sketched_bend needs the FIXED face -- the side that stays put.",
                hint="Pass fixed_face_index from analyze_model(faces), or fixed_point=[x, y, z].",
            )

        report = compat.invoke_first(manager, [
            ("InsertSheetMetal3dBend", (
                angle_rad, use_default_radius, radius_m, flip, position, nothing(),
            )),
        ])
        feature = report.value

    if feature is None:
        raise SolidWorksError(
            "SHEET_METAL_FAILED",
            "InsertSheetMetal3dBend returned null. Check the bend line lies on a sheet face and "
            "spans it, and that the fixed face is on the sheet.",
            {"sketch": sketch_name, "fixed_face_index": fixed_face_index},
        )

    # InsertSheetMetal3dBend IGNORES its Flip argument: both values produce the
    # same fold and the created feature reads back ReverseDirection=false either
    # way. Enforce the requested direction through the feature definition.
    flip_enforced = False
    definition = pm(feature, "GetDefinition", default=None)
    if definition is not None and bool(pm(definition, "ReverseDirection", default=False)) != flip:
        accessed = False
        modified = False
        try:
            accessed = bool(definition.AccessSelections(doc, nothing()))
            definition.ReverseDirection = flip
            modified = bool(feature.ModifyDefinition(definition, doc, nothing()))
            flip_enforced = modified
        except Exception as exc:
            logger.debug("sketched bend flip enforcement failed: %s", exc)
        finally:
            # ModifyDefinition consumes the selection access; release it only if
            # ModifyDefinition never ran.
            if accessed and not modified:
                try:
                    definition.ReleaseSelectionAccess()
                except Exception:
                    pass

    compat.invoke_any(doc, [("EditRebuild3", ())])
    return {
        "feature": compat.feature_name(feature),
        "feature_type": "sketched_bend",
        "bend_line_sketch": sketch_name,
        "angle_degrees": angle,
        "bend_position": bend_position,
        "flip_requested": flip,
        "flip_enforced_via_definition": flip_enforced,
        "body": selection.body_summary(doc),
    }


def _set_flat_state(session, doc, flatten: bool) -> dict:
    """Flatten or fold by (un)suppressing the Flat-Pattern feature.

    ``SetBendState`` only flips a state flag -- the displayed body re-folds on the
    next rebuild, so the centre of gravity comes back unchanged from the bent
    state. Unsuppressing Flat-Pattern is what the UI's Flatten button actually
    does, and it produces a genuinely unfolded body.
    """
    flat = compat.find_feature_by_type(doc, "FlatPattern")
    if flat is None:
        raise ToolError(
            "NOT_SHEET_METAL",
            "This part has no Flat-Pattern feature, so it is not sheet metal.",
            hint="Create a sheet-metal base first: "
                 "sheet_metal_feature(feature_type='base_flange', thickness=...).",
        )

    action = enums.UNSUPPRESS_FEATURE if flatten else enums.SUPPRESS_FEATURE
    report = compat.invoke_bool(flat, [
        ("SetSuppression2", (action, enums.THIS_CONFIGURATION, nothing())),
        ("SetSuppression", (action,)),
    ])
    if not report.ok:
        raise SolidWorksError(
            "FLAT_PATTERN_FAILED",
            f"Could not {'unsuppress' if flatten else 'suppress'} the Flat-Pattern feature.",
            {"attempts": report.diagnostic()},
        )

    compat.invoke_any(doc, [("EditRebuild3", ())])
    return {
        "feature": compat.feature_name(flat),
        "feature_type": "flat_pattern" if flatten else "fold",
        "state": "flattened" if flatten else "folded",
        "bend_state": pm(doc, "GetBendState", default=None),
        "body": selection.body_summary(doc),
    }
