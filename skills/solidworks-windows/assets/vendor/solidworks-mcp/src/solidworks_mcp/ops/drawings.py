"""
Drawing views and annotations.

The reference C# project spread this across six tools (``add_drawing_view``,
``add_flat_pattern_view``, ``add_section_view``, ``add_drawing_dimension``,
``auto_dimension_drawing``, ``auto_center_marks``, ``add_hole_callout``). Here
there are two: :func:`add_view` for geometry and :func:`annotate` for annotation,
each with an action discriminator. The reference Python project had none of it.

A note on which annotation approach to prefer: coordinate-picked drawing
dimensions are fragile, because a sheet point has to land on a projected edge that
may have moved when the view scaled. ``annotate(action="model_dimensions")`` pulls
the model's own parametric dimensions into the views instead -- correctly placed,
no picking -- and is the right default.
"""

from __future__ import annotations

import logging

from ..com import compat, enums, selection
from ..com.compat import nothing, pm
from ..com.connection import SolidWorksError, SolidWorksSession
from ..util import units
from ..util.result import ToolError, one_of, require

logger = logging.getLogger(__name__)

VIEW_TYPES = set(enums.VIEW_NAME_BY_TYPE) | {"flat_pattern", "section"}
ANNOTATE_ACTIONS = {"model_dimensions", "center_marks", "hole_callout", "dimension", "note"}


def _drawing(session: SolidWorksSession):
    """The active document, asserted to be a drawing."""
    return session.require_doc("DRAWING")


def _resolve_model_path(session, doc, model_path: str | None) -> str:
    """The referenced model's path, falling back to the first open part/assembly."""
    if model_path:
        return model_path

    for candidate in compat.as_list(pm(session.app, "GetDocuments", default=None)):
        kind = session.doc_type(candidate)
        if kind in {"PART", "ASSEMBLY"}:
            path = pm(candidate, "GetPathName", default="")
            if path:
                return path

    raise ToolError(
        "MODEL_NOT_FOUND",
        "No model_path was given and no saved part or assembly is open.",
        hint="Save the model to disk first -- a drawing view can only reference a saved file -- "
             "then pass its path, or leave it open.",
    )


def iter_views(doc):
    """Yield the drawing's views. The first entry is the sheet itself, not a view."""
    view = pm(doc, "GetFirstView", default=None)
    seen = 0
    while view is not None and seen < compat.WALK_LIMIT:
        yield view
        seen += 1
        view = pm(view, "GetNextView", default=None)


def model_views(doc) -> list:
    """Just the model views, skipping the sheet."""
    return list(iter_views(doc))[1:]


# ---------------------------------------------------------------------------
# add_view
# ---------------------------------------------------------------------------

def add_view(
    session: SolidWorksSession,
    view_type: str,
    *,
    position: list | None = None,
    scale: float | None = None,
    model_path: str | None = None,
    display_mode: str | None = None,
    configuration: str | None = None,
    hide_bend_lines: bool = False,
    flip: bool = False,
    section_line: list | None = None,
    section_edge_point: list | None = None,
    label: str = "A",
    unit: str | None = None,
    converter: units.UnitConverter | None = None,
) -> dict:
    """Place a view on the active drawing sheet.

    The referenced model must be saved to disk -- SolidWorks resolves views by
    file path, not by open document.

    Args:
        view_type:
            * A standard projection: ``front``, ``back``, ``left``, ``right``,
              ``top``, ``bottom``, ``isometric``, ``trimetric``, ``dimetric``,
              ``current``.
            * ``flat_pattern`` -- the unfolded sheet-metal blank with bend lines.
              This is the *correct* way to detail sheet metal, which is dimensioned
              on its flat pattern rather than on the folded views.
            * ``section`` -- a section view exposing internal or blind features as
              dimensionable edges. Define the cut with ``section_line`` (through a
              feature's interior, where no edge exists) or ``section_edge_point``
              (along an existing projected edge).
        position: ``[x, y]`` on the sheet, in ``unit``. Defaults to 100 mm, 100 mm.
        scale: View scale. Inherits the sheet's scale when omitted.
        model_path: Path to the part or assembly. Auto-detects the first saved open
            model when omitted.
        display_mode: ``wireframe``, ``hlv``, ``hlr``, ``shaded``, ``shaded_edges``.
            Omitted means the drafting default -- orthographic views get hidden
            lines visible, isometric keeps the document default.
        configuration: Model configuration to show.
        hide_bend_lines: Flat pattern only.
        flip: Flat pattern or section -- mirror the view / reverse the cut direction.
        section_line: Section only -- ``[x1, y1, x2, y2]`` cut line in sheet
            coordinates.
        section_edge_point: Section only -- ``[x, y]`` on an existing straight
            projected edge to cut along.
        label: Section only -- the section letter, e.g. ``"A"`` gives "SECTION A-A".
        unit: Unit for all sheet coordinates.
    """
    doc = _drawing(session)
    converter = converter or units.UnitConverter(session.config.default_unit)
    kind = one_of(view_type, "view_type", VIEW_TYPES)

    coords = position or [100.0, 100.0]
    if len(coords) < 2:
        raise ToolError("INVALID_PARAMETER", "position needs an x and a y.")
    pos_x, pos_y = converter.points_to_meters(coords[:2], unit)

    if kind == "section":
        return _add_section_view(
            session, doc, converter, unit, pos_x, pos_y, scale, label, flip,
            section_line, section_edge_point,
        )

    resolved_model = _resolve_model_path(session, doc, model_path)

    if kind == "flat_pattern":
        config = configuration or _active_configuration_of(session, resolved_model) or "Default"
        report = compat.invoke_first(doc, [
            ("CreateFlatPatternViewFromModelView3", (
                resolved_model, config, pos_x, pos_y, 0.0, hide_bend_lines, flip,
            )),
            ("CreateFlatPatternViewFromModelView2", (
                resolved_model, config, pos_x, pos_y, 0.0, hide_bend_lines,
            )),
        ])
        if not report.ok:
            raise SolidWorksError(
                "VIEW_CREATION_FAILED",
                f"The flat-pattern view returned null for '{resolved_model}' "
                f"(configuration '{config}'). The part must be sheet metal -- it needs a "
                f"Flat-Pattern feature -- and must be saved to disk.",
                {"attempts": report.diagnostic()},
            )
        view = report.value
        detail = {"configuration": config, "hide_bend_lines": hide_bend_lines}
    else:
        standard_name = enums.VIEW_NAME_BY_TYPE[kind]
        report = compat.invoke_first(doc, [
            ("CreateDrawViewFromModelView3", (resolved_model, standard_name, pos_x, pos_y, 0.0)),
            ("CreateDrawViewFromModelView2", (resolved_model, standard_name, pos_x, pos_y, 0.0)),
        ])
        if not report.ok:
            raise SolidWorksError(
                "VIEW_CREATION_FAILED",
                f"The {kind} view returned null for '{resolved_model}'. The model must be saved "
                f"to disk before a drawing can reference it.",
                {"attempts": report.diagnostic()},
            )
        view = report.value
        detail = {"standard_view": standard_name}

        if configuration:
            try:
                view.ReferencedConfiguration = configuration
                detail["configuration"] = configuration
            except Exception:
                pass

    if scale:
        try:
            view.ScaleDecimal = float(scale)
        except Exception:
            logger.debug("Could not set view scale")

    applied_mode = _apply_display_mode(view, display_mode, kind)

    return {
        "view": pm(view, "Name", default=None),
        "view_type": kind,
        "model": resolved_model,
        "position": {"x": coords[0], "y": coords[1],
                     "unit": units.parse_unit(unit, converter.default).value},
        "scale": pm(view, "ScaleDecimal", default=scale),
        "display_mode": applied_mode,
        **detail,
    }


def _active_configuration_of(session, model_path: str) -> str | None:
    """The active configuration of an open document matching ``model_path``."""
    import os

    target = os.path.normcase(os.path.abspath(model_path))
    for candidate in compat.as_list(pm(session.app, "GetDocuments", default=None)):
        path = pm(candidate, "GetPathName", default="")
        if path and os.path.normcase(os.path.abspath(path)) == target:
            manager = pm(candidate, "ConfigurationManager", default=None)
            config = pm(manager, "ActiveConfiguration", default=None) if manager else None
            return pm(config, "Name", default=None) if config else None
    return None


def _apply_display_mode(view, display_mode: str | None, kind: str) -> str | None:
    """Set the view's display mode, defaulting orthographic views to hidden-lines-visible.

    That default follows drafting convention: show hidden lines for reference, but
    never dimension them. Isometric views keep the document default.
    """
    if display_mode:
        mode = one_of(display_mode, "display_mode", set(enums.DISPLAY_MODE_BY_NAME))
        code = enums.DISPLAY_MODE_BY_NAME[mode]
    elif kind in {"isometric", "trimetric", "dimetric", "current"}:
        return None
    else:
        mode, code = "hlv", enums.DISPLAY_MODE_BY_NAME["hlv"]

    report = compat.invoke_bool(view, [
        ("SetDisplayMode3", (False, code, False, True)),
        ("SetDisplayMode2", (False, code, False, True, False)),
    ])
    return mode if report.ok else None


def _add_section_view(session, doc, converter, unit, pos_x, pos_y, scale, label, flip,
                      section_line, section_edge_point):
    """Create a section view from either a drawn cut line or an existing edge."""
    if section_edge_point:
        if len(section_edge_point) < 2:
            raise ToolError("INVALID_PARAMETER", "section_edge_point needs an x and a y.")
        ex, ey = converter.points_to_meters(section_edge_point[:2], unit)
        selection.clear(doc)
        if not selection.select_by_id(doc, "", enums.SEL_STRING_EDGE, ex, ey, 0.0):
            raise ToolError(
                "EDGE_NOT_FOUND",
                f"No projected edge found at {list(section_edge_point)} to cut along.",
                hint="Use section_line=[x1, y1, x2, y2] to cut through a feature's interior "
                     "where no edge exists.",
            )
        cut_description = f"along the edge at {list(section_edge_point)}"

    elif section_line:
        if len(section_line) < 4:
            raise ToolError(
                "INVALID_PARAMETER", "section_line needs [x1, y1, x2, y2]."
            )
        x1, y1, x2, y2 = converter.points_to_meters(section_line[:4], unit)

        # The cut line must be drawn INSIDE a view's sketch, not on the sheet: a
        # sheet-level line has no owning view, and section creation then returns
        # null. Activate the parent view so SketchManager works in its space.
        views = model_views(doc)
        if not views:
            raise ToolError(
                "NO_VIEW",
                "The drawing has no model view to section.",
                hint="Add a model view first with add_view(view_type='front', ...).",
            )
        parent = views[0]
        parent_name = pm(parent, "Name", default=None)
        try:
            doc.ActivateView(parent_name)
        except Exception:
            pass

        sketch_manager = pm(doc, "SketchManager", default=None)
        if sketch_manager is None:
            raise SolidWorksError("NO_SKETCH_MANAGER", "The drawing exposes no SketchManager.")

        # View-sketch space is model-scale and view-centred, so sheet coordinates
        # must be shifted by the view's sheet position and divided by its scale.
        origin = compat.as_floats(pm(parent, "Position", default=None))
        view_scale = float(pm(parent, "ScaleDecimal", default=1.0) or 1.0)
        ox, oy = (origin[0], origin[1]) if len(origin) >= 2 else (0.0, 0.0)

        def to_view(sheet_x, sheet_y):
            return (sheet_x - ox) / view_scale, (sheet_y - oy) / view_scale

        vx1, vy1 = to_view(x1, y1)
        vx2, vy2 = to_view(x2, y2)

        sketch_manager.CreateLine(vx1, vy1, 0.0, vx2, vy2, 0.0)
        if pm(sketch_manager, "ActiveSketch", default=None) is not None:
            compat.invoke_any(sketch_manager, [("InsertSketch", (True,))])
        cut_description = f"along the line {list(section_line[:4])}"

    else:
        raise ToolError(
            "MISSING_PARAMETER",
            "A section view needs section_line=[x1, y1, x2, y2] or section_edge_point=[x, y].",
            hint="Use section_line to cut through a pocket's interior; use section_edge_point "
                 "when a real edge already lies on the desired cut plane.",
        )

    # Default behaviour (aligned, hatched) is options=0. There is no "auto hatch"
    # bit -- bit 1 means NotAligned, so passing it as a supposedly-harmless default
    # would silently un-align every section view.
    options = enums.SECTION_VIEW_DEFAULT
    if flip:
        options |= enums.SECTION_VIEW_CHANGE_DIRECTION

    report = compat.invoke_first(doc, [
        ("CreateSectionViewAt5", (pos_x, pos_y, 0.0, label, options, nothing(), 0.0)),
        ("CreateSectionViewAt3", (pos_x, pos_y, 0.0, label, options, nothing())),
        ("CreateSectionViewAt2", (pos_x, pos_y, 0.0, label, options)),
    ])

    if not report.ok:
        raise SolidWorksError(
            "SECTION_FAILED",
            f"Creating the section view {cut_description} returned null. The cut must pass "
            f"through material inside the parent view's bounds. Repeated failed attempts can "
            f"leave the drawing in a state where section creation keeps failing -- start from "
            f"a clean drawing if so.",
            {"attempts": report.diagnostic()},
        )

    view = report.value
    if scale:
        try:
            view.ScaleDecimal = float(scale)
        except Exception:
            pass

    return {
        "view": pm(view, "Name", default=None),
        "view_type": "section",
        "label": label,
        "section_label": f"SECTION {label}-{label}",
        "cut": cut_description,
        "flipped": flip,
        "scale": pm(view, "ScaleDecimal", default=scale),
        "next_step": "Dimension the now-visible internal geometry with "
                     "annotate(action='model_dimensions') or annotate(action='dimension').",
    }


# ---------------------------------------------------------------------------
# annotate
# ---------------------------------------------------------------------------

def annotate(
    session: SolidWorksSession,
    action: str,
    *,
    all_views: bool = True,
    include_unmarked: bool = False,
    eliminate_duplicates: bool = True,
    include_slots: bool = True,
    extended_lines: bool = True,
    point: list | None = None,
    value: float | None = None,
    label_offset: list | None = None,
    text: str | None = None,
    unit: str | None = None,
    converter: units.UnitConverter | None = None,
) -> dict:
    """Annotate the active drawing.

    Args:
        action:
            * ``model_dimensions`` -- transfer the model's own driving dimensions
              into the views (SolidWorks "Insert Model Items > Dimensions"). The
              robust default: dimensions come from the model's parametric values,
              correctly placed, with no coordinate picking.
            * ``center_marks`` -- auto-place centre marks with extended centrelines
              on every hole and slot in every model view.
            * ``hole_callout`` -- add a callout (``4x M6 THRU``) on the hole edge
              nearest ``point``.
            * ``dimension`` -- a driving dimension on the projected edge nearest
              ``point``, driven to ``value``.
            * ``note`` -- place a free text note at ``point``.
        all_views: ``model_dimensions`` -- insert into every view, not just the
            selected one.
        include_unmarked: ``model_dimensions`` -- also insert dimensions *not*
            marked for drawing, i.e. all driving dimensions. Noisier but complete;
            try this when the marked-only pass inserts zero.
        eliminate_duplicates: ``model_dimensions`` -- avoid repeating the same
            model dimension in more than one view.
        include_slots: ``center_marks`` -- also mark linear and arc slots.
        extended_lines: ``center_marks`` -- draw full centrelines rather than a
            small cross.
        point: ``[x, y]`` sheet coordinates, for the picking actions.
        value: ``dimension`` -- the value to drive to, in ``unit``.
        label_offset: ``dimension`` -- ``[dx, dy]`` for the label position.
        text: ``note`` -- the note text.
        unit: Unit for sheet coordinates and for ``value``.
    """
    doc = _drawing(session)
    converter = converter or units.UnitConverter(session.config.default_unit)
    verb = one_of(action, "action", ANNOTATE_ACTIONS)

    if verb == "model_dimensions":
        types = enums.INSERT_DIMS_MARKED_FOR_DRAWING
        if include_unmarked:
            types |= enums.INSERT_DIMS_NOT_MARKED_FOR_DRAWING

        report = compat.invoke_first(doc, [
            # (Option, Types, AllViews, DuplicateDims, HiddenFeatureDims, InstanceCounts)
            ("InsertModelAnnotations3", (0, types, all_views, eliminate_duplicates, False, False)),
            ("InsertModelAnnotations2", (0, types, all_views, eliminate_duplicates, False)),
        ], accept=lambda r: r is not None)

        inserted = 0
        if report.value is not None:
            inserted = len(compat.as_list(report.value))

        return {
            "action": verb,
            "inserted": inserted,
            "all_views": all_views,
            "include_unmarked": include_unmarked,
            "note": (
                "Zero dimensions inserted. Retry with include_unmarked=true -- the model's "
                "dimensions may not be marked for drawing."
                if inserted == 0 else None
            ),
        }

    if verb == "center_marks":
        views = model_views(doc)
        if not views:
            raise ToolError(
                "NO_VIEW", "The drawing has no model views to annotate.",
                hint="Add a view first with add_view.",
            )

        # Verified via the interface's own type info (IView.AutoInsertCenterMarks2):
        # (InsertType, InsertOption, LinearSlotCenter, ArcSlotCenter,
        #  UseDocumentDefaults, Size, Gap, ExtendedLines, CenterLineFont, Angle)
        # -- 10 arguments, not the 7 originally guessed here. The wrong arity made
        # every call fail silently (invoke_bool reported no signature matched), so
        # views_processed stayed 0 regardless of whether the model had holes.
        #
        # InsertType is itself a bitmask (swAutoInsertCenterMarkType_e), and 0 is
        # not a member of it -- it silently matches nothing, so the call returned
        # False for every view regardless of hole content. Hole marking must be
        # requested explicitly via CENTER_MARK_TYPE_HOLE.
        #
        # GetCenterMarks/GetCenterMarkCount also do not reflect marks placed this
        # way on this build -- they read back 0/null even right after a verified
        # successful insert (confirmed live via GetAnnotationCount before/after).
        # Count placed marks via the annotation-count delta instead.
        insert_type = enums.CENTER_MARK_TYPE_HOLE
        if include_slots:
            insert_type |= enums.CENTER_MARK_TYPE_SLOTS
        total = 0
        processed = 0
        for view in views:
            before = pm(view, "GetAnnotationCount", default=0) or 0
            report = compat.invoke_bool(view, [
                ("AutoInsertCenterMarks2", (
                    insert_type, 0, include_slots, include_slots, True,
                    0.003, 0.001, extended_lines, False, 0.0,
                )),
                ("AutoInsertCenterMarks", (
                    insert_type, 0, include_slots, include_slots, True,
                    0.003, extended_lines, False, 0.0,
                )),
            ])
            if report.ok:
                processed += 1
                after = pm(view, "GetAnnotationCount", default=before) or before
                total += max(0, after - before)

        return {
            "action": verb,
            "center_marks": total,
            "views_processed": processed,
            "views_total": len(views),
            "include_slots": include_slots,
            "note": (
                "No centre marks were added -- the model may have no holes or slots."
                if total == 0 else None
            ),
        }

    # The remaining actions all need a sheet point.
    if not point or len(point) < 2:
        raise ToolError(
            "MISSING_PARAMETER",
            f"action='{verb}' needs point=[x, y] in sheet coordinates.",
        )
    scale = units.TO_METERS[units.parse_unit(unit, converter.default)]
    px, py = float(point[0]) * scale, float(point[1]) * scale

    if verb == "hole_callout":
        # Both AddHoleCallout2 and AddHoleCallout are void APIs -- they return
        # None on success too, so invoke_first's default "None means failure"
        # accept always misreported a real callout as a failed one. Verified via
        # the total annotation count across every view before/after, the same
        # pattern already used for center_marks and create_shell.
        #
        # AddHoleCallout2 takes (X, Y, Z) directly per the interface's own type
        # info, but verified live it does not actually place anything even when
        # the coordinate sits exactly on a hole's edge. What does work: select
        # the edge first (SelectByID2), then call the legacy zero-argument
        # AddHoleCallout(), which operates on the current selection -- it was
        # previously called here with 3 coordinate args, which is not its
        # signature at all ("Invalid number of parameters" on every attempt).
        #
        # A failed AddHoleCallout2 call also has a side effect: verified live,
        # it leaves the document unable to re-select the same edge by
        # coordinate until rebuilt, so the fallback selection below silently
        # finds nothing without the rebuild in between.
        views = model_views(doc)

        def annotation_total() -> int:
            return sum(pm(view, "GetAnnotationCount", default=0) or 0 for view in views)

        selection.clear(doc)
        before = annotation_total()
        doc.AddHoleCallout2(px, py, 0.0)
        compat.invoke_any(doc, [("EditRebuild3", ())])
        placed = annotation_total() > before

        if not placed:
            selection.clear(doc)
            if selection.select_by_id(doc, "", "EDGE", px, py, 0.0):
                doc.AddHoleCallout()
                placed = annotation_total() > before

        if not placed:
            raise SolidWorksError(
                "CALLOUT_FAILED",
                f"No hole callout could be created at {list(point[:2])}. The nearest edge must "
                f"be a hole's projected circular edge.",
            )
        return {
            "action": verb,
            "point": list(point[:2]),
        }

    if verb == "note":
        require(text, "text", hint="The note text to place.")
        manager = pm(doc, "Extension", default=None)
        note = None
        if manager is not None:
            note = pm(manager, "InsertNote", text, default=None)
        if note is None:
            note = pm(doc, "InsertNote", text, default=None)
        if note is None:
            raise SolidWorksError("NOTE_FAILED", "InsertNote returned null.")
        try:
            annotation = pm(note, "GetAnnotation", default=None)
            if annotation is not None:
                annotation.SetPosition(px, py, 0.0)
        except Exception:
            pass
        return {"action": verb, "text": text, "point": list(point[:2])}

    # verb == "dimension" -- delegate to the shared sketch dimension routine,
    # which already handles drawing documents.
    from .sketches import add_dimension

    result = add_dimension(
        session, point=list(point[:2]), value=value if value is not None else 0.0,
        label_offset=label_offset, unit=unit, converter=converter,
    )
    result["action"] = verb
    return result


# ---------------------------------------------------------------------------
# analyze_drawing
# ---------------------------------------------------------------------------

def analyze_drawing(
    session: SolidWorksSession,
    include_geometry: bool = False,
    converter: units.UnitConverter | None = None,
) -> dict:
    """Read the active drawing's views, dimensions and optionally projected geometry.

    The read-only counterpart to ``analyze_model``. Use it to check whether a
    produced drawing's dimensions actually match the model, or to reverse-engineer
    a part from its drawing.

    Args:
        include_geometry: Also return each view's projected 2D primitives (lines
            and curves) in model-scale metres, centred on the view. The segment
            structure carries profile information a dimension value cannot.
    """
    doc = _drawing(session)
    converter = converter or units.UnitConverter(session.config.default_unit)
    precision = session.config.precision

    views = []
    total_dimensions = 0

    for index, view in enumerate(iter_views(doc)):
        entry: dict = {
            "index": index,
            "name": pm(view, "Name", default=None),
            "is_sheet": index == 0,
            "type": pm(view, "Type", default=None),
            "scale": pm(view, "ScaleDecimal", default=None),
        }

        position = compat.as_floats(pm(view, "Position", default=None))
        if len(position) >= 2:
            entry["position"] = [round(v, precision) for v in position[:2]]

        dimensions = []
        for item in compat.iter_display_dimensions(view):
            dimension = compat.read_dimension(item)
            if dimension is None:
                continue
            si_value = compat.dimension_value(dimension)
            record = {
                "name": pm(dimension, "FullName", default=None) or pm(dimension, "Name",
                                                                     default=None),
            }
            if si_value is not None:
                try:
                    record["value_si"] = round(float(si_value), precision)
                    record["value"] = round(converter.from_meters(float(si_value)), 6)
                    record["unit"] = converter.default.value
                except (TypeError, ValueError):
                    pass
            dimensions.append(record)

        entry["dimensions"] = dimensions
        entry["dimension_count"] = len(dimensions)
        total_dimensions += len(dimensions)

        if include_geometry and index > 0:
            entry["geometry"] = _view_geometry(view, precision)

        views.append(entry)

    return {
        "document": pm(doc, "GetTitle", default=None),
        "view_count": max(0, len(views) - 1),
        "dimension_count": total_dimensions,
        "views": views,
        "note": "views[0] is the drawing sheet, not a model view.",
    }


def _view_geometry(view, precision: int) -> dict:
    """Projected 2D primitives of one view, split into lines and curves.

    ``GetPolylines7`` needs a byref ``VT_VARIANT`` out-parameter for the array --
    passing ``nothing()`` (a typed ``VT_DISPATCH`` null) raised ``Type mismatch``
    on every call, which ``pm()`` swallowed into ``default=None``, so this always
    reported ``readable: False`` regardless of what the view contained.
    ``GetPolyLinesAndCurves`` returns the identical flat array as a plain return
    value instead (verified live: byte-for-byte the same data), which sidesteps
    the out-param typing entirely.

    That flat array is a run-length encoding, but the header preceding each
    primitive's points is NOT the fixed 2-value ``(type, count)`` this used to
    assume -- verified live (a rectangle + one circular hole) via exhaustive
    backtracking search over the only grammar that parses the whole buffer with
    no leftover bytes: a **9-value** header for a straight line (ending in the
    point count) and a **21-value** header for a curve/arc (ending in the point
    count, with center/normal/direction data ahead of it that this reader does
    not need). The first header value is 0 for a line and non-zero for a curve
    in every sample seen; other curve subtypes (splines, ellipses) may use a
    different header length, so each record's length is *resolved*, not
    hardcoded -- tried against a plausible range and confirmed by checking that
    the remaining bytes still parse to completion.
    """
    lines: list[dict] = []
    curves: list[dict] = []

    raw = pm(view, "GetPolyLinesAndCurves", 0, default=None)
    values = compat.as_floats(raw)
    if not values:
        return {"lines": lines, "curves": curves, "readable": False}

    records = _parse_polyline_records(values)
    if records is None:
        return {"lines": lines, "curves": curves, "readable": False}

    for is_line, coords in records:
        points = [
            [round(coords[i], precision), round(coords[i + 1], precision)]
            for i in range(0, len(coords), 3)
        ]
        if is_line and len(points) >= 2:
            for start, end in zip(points, points[1:]):
                lines.append({"x1": start[0], "y1": start[1], "x2": end[0], "y2": end[1]})
        elif points:
            curves.append({"points": points})

    return {
        "lines": lines,
        "curves": curves,
        "line_count": len(lines),
        "curve_count": len(curves),
        "readable": True,
        "units": "model-scale metres, centred on the view",
    }


def _parse_polyline_records(values: list[float]) -> list[tuple[bool, list[float]]] | None:
    """Split ``GetPolyLinesAndCurves``' flat array into ``(is_line, coords)`` records.

    Each record is a header ending in a point count, then that many ``(x, y, z)``
    triples. The header length is 9 for a line and 21 for a curve/arc in every
    live sample seen (a rectangle plus one circular hole); other curve subtypes
    (splines, ellipses) may use a different length, so it is *resolved* per
    record via memoized backtracking -- the candidate must leave a suffix that
    itself parses to completion -- rather than hardcoded. Returns ``None`` if no
    decomposition consumes the whole array.
    """
    total = len(values)
    memo: dict[int, list[tuple[int, int]] | None] = {}

    def solve(cursor: int) -> list[tuple[int, int]] | None:
        if cursor == total:
            return []
        if cursor in memo:
            return memo[cursor]
        memo[cursor] = None  # guards against re-entrant loops on a bad parse
        for header_len in (9, 21, *range(2, 41)):
            if cursor + header_len > total:
                continue
            count = values[cursor + header_len - 1]
            if count != int(count) or count < 2 or count > 5000:
                continue
            count = int(count)
            end = cursor + header_len + count * 3
            if end > total:
                continue
            rest = solve(end)
            if rest is not None:
                memo[cursor] = [(header_len, count)] + rest
                return memo[cursor]
        return None

    spans = solve(0)
    if spans is None:
        return None

    records = []
    cursor = 0
    for header_len, count in spans:
        is_line = values[cursor] == 0
        cursor += header_len
        records.append((is_line, values[cursor:cursor + count * 3]))
        cursor += count * 3
    return records
