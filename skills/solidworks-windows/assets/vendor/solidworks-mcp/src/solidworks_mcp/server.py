"""
The MCP server: tool definitions and dispatch.

Every tool follows the same three steps -- validate arguments, marshal the
operation onto the STA COM thread, wrap the outcome in the standard envelope. The
``@tool`` decorator does the marshalling and error handling so each tool body stays
about the SolidWorks work.

Tool inventory (41 tools, deliberately deduplicated -- five ``draw_*`` tools collapse
into one batch ``add_sketch_entities``, six drawing tools into two, and separate
extrude/cut/revolve/sweep/loft tools into one ``extrude_feature``):

======================  ==========================================================
Connection              ``connect_solidworks``, ``get_solidworks_info``, ``set_units``
Documents               ``new_document``, ``open_document``, ``save_document``,
                        ``close_document``, ``activate_document``, ``list_documents``,
                        ``rebuild_document``, ``export_document``,
                        ``set_configuration``
Sketching               ``create_sketch``, ``edit_sketch``, ``exit_sketch``,
                        ``add_sketch_entities``, ``add_sketch_constraint``,
                        ``add_sketch_dimension``
Features                ``extrude_feature``, ``add_edge_feature``, ``create_rib``,
                        ``create_shell``, ``create_hole``, ``add_reference_geometry``,
                        ``create_pattern``, ``set_material``, ``modify_dimension``,
                        ``edit_feature``
Sheet metal             ``sheet_metal_feature``
Drawings                ``create_drawing_view``, ``annotate_drawing``
Assemblies              ``insert_component``, ``add_mate``, ``analyze_assembly``,
                        ``save_body_as_part``
Analysis                ``analyze_model``, ``analyze_drawing``, ``get_selection``,
                        ``compare_documents``
Scripting               ``execute_python``, ``run_macro``
======================  ==========================================================
"""

from __future__ import annotations

import functools
import logging
import logging.handlers
from collections.abc import Callable
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from .com import constants as swconst
from .com.connection import SolidWorksSession
from .config import load_config
from .ops import (
    analysis,
    assemblies,
    documents,
    drawings,
    features,
    scripting,
    sheetmetal,
    sketches,
)
from .util import result as R
from .util import units

logger = logging.getLogger("solidworks_mcp")

CONFIG = load_config()
SESSION = SolidWorksSession(CONFIG)
CONVERTER = units.UnitConverter(CONFIG.default_unit)

mcp = FastMCP(
    "solidworks",
    instructions=(
        "Drives SolidWorks on this machine through its COM API. Works with SolidWorks "
        "2018 through 2026.\n\n"
        "Typical part workflow: new_document -> create_sketch -> add_sketch_entities -> "
        "extrude_feature -> add_edge_feature -> save_document.\n\n"
        "Selecting geometry: prefer INDICES over coordinates. Run "
        "analyze_model(analysis_type='faces') or ('edges') to get stable indices, then pass "
        "them to create_sketch(face_index=...), add_edge_feature(edge_indices=[...]) and so on. "
        "Coordinate picks are ambiguous wherever faces or edges meet. If the user has clicked "
        "geometry in the SolidWorks window, get_selection maps their click to the same indices.\n\n"
        "Units: lengths default to millimetres and every tool takes an optional `unit`. Angles "
        "are always in degrees. Analysis results report SI values alongside display units.\n\n"
        "Every tool returns JSON with an `ok` flag; failures carry `error.code` and an "
        "`error.hint` naming the next action."
    ),
)


def _configure_logging() -> None:
    """Log to a file only -- stdout is the MCP transport and must stay clean."""
    root = logging.getLogger("solidworks_mcp")
    root.setLevel(CONFIG.numeric_log_level())
    if root.handlers:
        return
    if not CONFIG.log_file:
        root.addHandler(logging.NullHandler())
        return
    path = Path(CONFIG.log_file)
    if not path.is_absolute():
        path = Path(__file__).resolve().parent / path
    try:
        handler = logging.handlers.RotatingFileHandler(
            path, maxBytes=4_000_000, backupCount=3, encoding="utf-8"
        )
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
        )
        root.addHandler(handler)
    except OSError:
        root.addHandler(logging.NullHandler())


_configure_logging()


def tool(operation: str, *, needs_connection: bool = True) -> Callable:
    """Wrap an operation as an MCP tool.

    Handles the three concerns every tool shares: ensuring a live connection,
    marshalling the call onto the STA COM thread, and converting the outcome (or any
    exception) into the standard JSON envelope. Tool bodies receive the session and
    return ``(message, data)``.
    """

    def decorator(func: Callable[..., tuple[str, dict]]) -> Callable[..., str]:
        @functools.wraps(func)
        def wrapper(*args, **kwargs) -> str:
            try:
                if needs_connection:
                    SESSION.ensure()

                def run() -> tuple[str, dict]:
                    scope = getattr(SESSION, "operation_scope", None)
                    if scope is not None:
                        with scope(operation):
                            return func(*args, **kwargs)
                    return func(*args, **kwargs)

                message, data = SESSION.com.call(run)
                state = (
                    SESSION.com.call(documents.doc_state, SESSION, timeout=30.0)
                    if needs_connection else None
                )
                return R.ok(operation, message, data, state)
            except BaseException as exc:  # noqa: BLE001 - every failure is reported
                return R.from_exception(operation, exc)

        return wrapper

    return decorator


# ===========================================================================
# Connection and system
# ===========================================================================

@mcp.tool()
def connect_solidworks(launch: bool = False) -> str:
    """Connect to SolidWorks, starting it if it is not already running.

    Every other tool connects automatically, so this is only needed to warm up the
    connection or to check the version before doing anything else.

    Args:
        launch: Start SolidWorks when no running instance is found. With false, the
            call fails instead of launching.
    """
    try:
        info = SESSION.ensure(launch=launch)
        capabilities = SESSION.capabilities()
        probed_apis = {
            name: value for name, value in capabilities.items()
            if not name.startswith("_") and isinstance(value, bool)
        }
        supported = sum(probed_apis.values())
        version_support = info.get("version_support", {})
        message = (
            f"Connected to SolidWorks {info['version_year'] or info['revision']}"
            + (" (launched it)" if info["launched"] else " (already running)")
        )
        if version_support.get("level") not in ("verified", "compatible"):
            message += f" -- WARNING: {version_support.get('message')}"
        return R.ok(
            "connect_solidworks",
            message,
            {**info, "capabilities": capabilities,
             "capability_summary": f"{supported}/{len(probed_apis)} probed APIs available"},
        )
    except BaseException as exc:  # noqa: BLE001
        return R.from_exception("connect_solidworks", exc)


@mcp.tool()
def get_solidworks_info() -> str:
    """Report the SolidWorks installation, version, and which APIs are available.

    Capabilities are probed from the live COM object rather than inferred from the
    version number, because availability also depends on installed add-ins -- sheet
    metal and the 3D Interconnect translators especially. Useful when a feature
    tool fails and you need to know whether the API exists at all on this install.

    Also reports whether SolidWorks' enum constants were read from the install's own
    type library (``constants.verified``) or are resting on the built-in fallbacks.
    A couple of operations that would mutate user preferences are skipped when
    unverified, so this is worth checking if an import route seems to be missing.

    ``data.version_support`` names the concrete version this install reports and
    whether this server trusts it: ``"verified"`` (directly tested), ``"compatible"``
    (untested but its own type library confirmed every constant this server uses),
    ``"degraded"`` (recognized but constants could not be verified -- real risk of
    wrong values), or ``"unsupported"`` (an unrecognized SolidWorks release entirely).
    """
    try:
        info = SESSION.info()
        message = (
            f"SolidWorks {info.get('version_year') or 'not connected'}"
            f" ({len(info.get('installed_versions', []))} install(s) registered)"
        )
        version_support = info.get("version_support")
        if version_support and version_support.get("level") not in ("verified", "compatible"):
            message += f" -- WARNING: {version_support.get('message')}"
        return R.ok(
            "get_solidworks_info",
            message,
            {**info, "constants": swconst.diagnostics(), "config": CONFIG.to_dict()},
        )
    except BaseException as exc:  # noqa: BLE001
        return R.from_exception("get_solidworks_info", exc)


@mcp.tool()
def set_units(unit: str) -> str:
    """Set the default length unit for every later tool call.

    The COM layer always works in metres; this only changes the tool boundary. Any
    tool can still override it per-call with its own ``unit`` argument.

    Args:
        unit: ``mm`` (default), ``cm``, ``m``, ``um``, ``in`` or ``ft``. Common
            aliases such as ``inch`` and ``millimeters`` are accepted.
    """
    try:
        resolved = units.parse_unit(unit)
        CONVERTER.default = resolved
        CONFIG.default_unit = resolved.value
        return R.ok(
            "set_units",
            f"Default length unit is now {resolved.value}.",
            {"unit": resolved.value,
             "note": "Angles are always in degrees, regardless of this setting."},
        )
    except BaseException as exc:  # noqa: BLE001
        return R.from_exception("set_units", exc)


# ===========================================================================
# Documents
# ===========================================================================

@mcp.tool()
def new_document(
    doc_type: str = "part",
    template_path: str | None = None,
    sheet_width: float | None = None,
    sheet_height: float | None = None,
) -> str:
    """Create a blank part, assembly or drawing.

    Args:
        doc_type: ``part`` (default), ``assembly`` or ``drawing``.
        template_path: Explicit template file. Resolved automatically when omitted --
            the configured default template first, then a filesystem search, so this
            works even on installs where no default template was ever set.
        sheet_width: Drawing only -- sheet width in the current default unit.
            Defaults to A3 landscape (420 x 297 mm).
        sheet_height: Drawing only -- sheet height in the current default unit.
    """

    @tool("new_document")
    def run() -> tuple[str, dict]:
        kind = R.one_of(doc_type, "doc_type", {"part", "assembly", "drawing"})
        width = CONVERTER.to_meters(sheet_width) if sheet_width else 0.420
        height = CONVERTER.to_meters(sheet_height) if sheet_height else 0.297
        data = documents.new_document(SESSION, kind, template_path, width, height)
        return f"Created {kind} '{data['document']}'.", data

    return run()


@mcp.tool()
def open_document(
    file_path: str,
    read_only: bool = False,
    as_assembly: bool = False,
    configuration: str | None = None,
) -> str:
    """Open an existing file and make it active.

    Native ``.sldprt``/``.sldasm``/``.slddrw`` open directly. Foreign formats -- STEP,
    IGES, Parasolid, and native NX/CATIA/Creo files -- escalate through three import
    routes automatically, because a plain open returns a misleading "file requires
    repair" error whenever 3D Interconnect is switched off.

    Args:
        file_path: Absolute path including the extension.
        read_only: Open without taking write access.
        as_assembly: Foreign files only -- import a structured assembly STEP as a
            real assembly with components rather than as a single multibody part.
        configuration: Configuration to activate on open.
    """

    @tool("open_document")
    def run() -> tuple[str, dict]:
        data = documents.open_document(SESSION, file_path, read_only, as_assembly, configuration)
        return (
            f"Opened {data['type'].lower()} '{data['document']}' via {data['opened_via']}.",
            data,
        )

    return run()


@mcp.tool()
def save_document(file_path: str | None = None) -> str:
    """Save the active document.

    A part must be saved to disk before a drawing view can reference it.

    Args:
        file_path: Target path for a Save As, including the extension. Parent
            directories are created. Omit for an in-place save, which requires the
            document to already have a path.
    """

    @tool("save_document")
    def run() -> tuple[str, dict]:
        data = documents.save_document(SESSION, file_path)
        return f"Saved to {data['path']}.", data

    return run()


@mcp.tool()
def close_document(save: bool = False, close_all: bool = False) -> str:
    """Close the active document, or every open document.

    Args:
        save: Save before closing. With false, unsaved changes are discarded.
        close_all: Close everything, including invisibly-loaded component documents.
            Worth doing between unrelated tasks: a lingering document with the same
            title blocks opening another folder's part and keeps files locked.
    """

    @tool("close_document")
    def run() -> tuple[str, dict]:
        data = documents.close_document(SESSION, save, close_all)
        return (
            "Closed all documents."
            if close_all else f"Closed '{data['closed']}'.",
            data,
        )

    return run()


@mcp.tool()
def activate_document(title: str) -> str:
    """Switch to an already-open document by title.

    Args:
        title: Document title, with or without the extension.
    """

    @tool("activate_document")
    def run() -> tuple[str, dict]:
        data = documents.activate_document(SESSION, title)
        return f"'{data['document']}' is now active.", data

    return run()


@mcp.tool()
def list_documents() -> str:
    """List every open document, flagging the active one and unsaved changes."""

    @tool("list_documents")
    def run() -> tuple[str, dict]:
        data = documents.list_documents(SESSION)
        return f"{data['count']} document(s) open; active: {data['active'] or 'none'}.", data

    return run()


@mcp.tool()
def rebuild_document(force: bool = False) -> str:
    """Rebuild the active document and report any feature errors.

    The cheapest way to confirm a model is still valid after a batch of parametric
    edits.

    Args:
        force: Force-rebuild every feature rather than only what is out of date.
            Slower, but surfaces latent failures.
    """

    @tool("rebuild_document")
    def run() -> tuple[str, dict]:
        data = documents.rebuild_document(SESSION, force)
        errors = data.get("feature_errors")
        return (
            f"Rebuilt{' (forced)' if force else ''}."
            + (f" {errors} feature error(s)." if errors else " No feature errors."),
            data,
        )

    return run()


@mcp.tool()
def export_document(formats: Any, output_path: str) -> str:
    """Export the active document to one or more formats.

    Args:
        formats: One format, or a list of them: ``STEP``, ``IGES``, ``STL``,
            ``PARASOLID``, ``3MF``, ``OBJ``, ``PDF``, ``DWG``, ``DXF``, ``JPG``,
            ``PNG``. ``DWG`` and ``DXF`` require an active drawing.
        output_path: For a single format, the full output path. For several, the
            path without an extension -- one file per format is written.
    """

    @tool("export_document")
    def run() -> tuple[str, dict]:
        requested = formats if isinstance(formats, str) else R.parse_json_arg(
            formats, "formats", list)
        data = documents.export_document(SESSION, requested, output_path)
        return f"Exported {data['exported']}/{data['requested']} format(s).", data

    return run()


@mcp.tool()
def set_configuration(name: str) -> str:
    """Activate a named configuration on the active document.

    Configurations are how one SolidWorks file carries size variants, so this is the
    read half of parametric family work -- ``modify_dimension`` being the write half.

    Args:
        name: Configuration name. ``analyze_model(analysis_type='configurations')``
            lists them.
    """

    @tool("set_configuration")
    def run() -> tuple[str, dict]:
        data = documents.set_configuration(SESSION, name)
        return f"Configuration '{name}' is now active.", data

    return run()


# ===========================================================================
# Sketching
# ===========================================================================

@mcp.tool()
def create_sketch(
    plane: str | None = None,
    face_index: int | None = None,
    face_point: Any = None,
    unit: str | None = None,
) -> str:
    """Open a new sketch on a plane or an existing model face.

    Any already-open sketch is closed first, so sketch mode never needs tracking.

    Args:
        plane: ``"Front Plane"``, ``"Top Plane"``, ``"Right Plane"``, a short form
            like ``"front"`` or ``"XY"``, or a created plane like ``"Plane1"``.
            Localised default-plane names resolve correctly on non-English installs.
        face_index: Sketch on a model face, from
            ``analyze_model(analysis_type='faces')``. **Preferred over
            ``face_point``** -- an index is unambiguous where a coordinate is not.
        face_point: ``[x, y, z]`` of a point on the target planar face.
        unit: Unit for ``face_point``.
    """

    @tool("create_sketch")
    def run() -> tuple[str, dict]:
        point = R.parse_json_arg(face_point, "face_point", list) if face_point else None
        data = sketches.create_sketch(SESSION, plane, face_index, point, unit, CONVERTER)
        return f"Sketch '{data['sketch']}' opened on {data['on']}.", data

    return run()


@mcp.tool()
def edit_sketch(sketch_name: str) -> str:
    """Reopen an existing sketch for editing.

    Args:
        sketch_name: Exact sketch feature name, e.g. ``"Sketch1"``.
    """

    @tool("edit_sketch")
    def run() -> tuple[str, dict]:
        data = sketches.edit_sketch(SESSION, sketch_name)
        return f"Editing '{data['sketch']}' ({data['segments']} segment(s)).", data

    return run()


@mcp.tool()
def exit_sketch() -> str:
    """Close the active sketch without consuming it.

    Feature tools exit sketch mode themselves, so this is only needed to abandon a
    sketch. Reports success even when no sketch was open.
    """

    @tool("exit_sketch")
    def run() -> tuple[str, dict]:
        data = sketches.exit_sketch(SESSION)
        return (
            f"Closed sketch '{data['sketch']}'." if data["was_open"]
            else "No sketch was open.",
            data,
        )

    return run()


@mcp.tool()
def add_sketch_entities(entities: Any, unit: str | None = None) -> str:
    """Draw one or many entities in the active sketch, in a single call.

    All entities are created under one batch bracket with SolidWorks' input
    inference disabled, so coordinates and radii land exactly as given rather than
    snapping to nearby geometry.

    Entity records by ``entity_type`` (lengths in ``unit``, angles in degrees):

    * ``line`` / ``centerline`` -- ``x1, y1, x2, y2``
    * ``rectangle`` -- ``x1, y1, x2, y2`` (opposite corners)
    * ``center_rectangle`` -- ``cx, cy, x1, y1``
    * ``circle`` -- ``cx, cy, radius``
    * ``arc`` -- ``x1, y1`` start, ``x2, y2`` end, ``xm, ym`` a point on the arc
    * ``arc_center`` -- ``cx, cy, x1, y1, x2, y2``, ``direction`` +1 CCW / -1 CW.
      Radius-exact; prefer it for shallow arcs.
    * ``ellipse`` -- ``cx, cy``, ``x1, y1`` major point, ``x2, y2`` minor point
    * ``spline`` -- ``points: [x1, y1, x2, y2, ...]``
    * ``polygon`` -- ``cx, cy, radius, sides``, optional ``inscribed``
    * ``slot`` -- ``x1, y1, x2, y2`` centre-line ends, ``radius``
    * ``point`` -- ``x, y``
    * ``fillet`` -- ``vx, vy`` the vertex to round, ``radius``
    * ``chamfer`` -- ``vx, vy`` the vertex to cut, ``distance``

    Any record may add ``"construction": true`` to make it reference geometry -- how
    you create a revolve axis.

    Args:
        entities: List of entity records, e.g.
            ``[{"entity_type": "circle", "cx": 0, "cy": 0, "radius": 25}]``.
        unit: Unit for every coordinate in this call.

    Example:
        A 100x60 plate outline with a 10 mm hole::

            entities=[
              {"entity_type": "rectangle", "x1": 0, "y1": 0, "x2": 100, "y2": 60},
              {"entity_type": "circle", "cx": 50, "cy": 30, "radius": 5}
            ]
    """

    @tool("add_sketch_entities")
    def run() -> tuple[str, dict]:
        records = R.parse_json_arg(entities, "entities", list)
        data = sketches.add_entities(SESSION, records, unit, CONVERTER)
        summary = ", ".join(f"{count} {name}" for name, count in data["counts"].items())
        return f"Created {summary} in '{data['sketch']}'.", data

    return run()


@mcp.tool()
def add_sketch_constraint(
    constraint_type: str,
    points: Any,
    entity_types: Any = None,
    unit: str | None = None,
) -> str:
    """Apply a geometric relation between sketch entities.

    Entities are picked by a point lying on each one -- a line's midpoint, or a point
    on a circle's perimeter.

    Args:
        constraint_type: One entity -- ``horizontal``, ``vertical``, ``fix``.
            Two entities -- ``coincident``, ``parallel``, ``perpendicular``,
            ``tangent``, ``equal``, ``concentric``, ``collinear``, ``midpoint``.
            Three -- ``symmetric`` (two entities plus the symmetry line).
        points: ``[[x1, y1], [x2, y2], ...]``, one per entity the relation needs.
        entity_types: Per-entity selection type -- ``"SKETCHSEGMENT"`` (default) or
            ``"SKETCHPOINT"``. Endpoint relations such as ``midpoint`` need
            ``SKETCHPOINT``.
        unit: Unit for the coordinates.
    """

    @tool("add_sketch_constraint")
    def run() -> tuple[str, dict]:
        picks = R.parse_json_arg(points, "points", list)
        types = R.parse_json_arg(entity_types, "entity_types", list) if entity_types else None
        data = sketches.add_constraint(SESSION, constraint_type, picks, types, unit, CONVERTER)
        return (
            f"Applied {data['constraint']} to {data['entities']} entity(ies); "
            f"sketch is {data['fully_defined'] or 'unknown'}.",
            data,
        )

    return run()


@mcp.tool()
def add_sketch_dimension(
    point: Any,
    value: float,
    label_offset: Any = None,
    is_angle: bool = False,
    unit: str | None = None,
) -> str:
    """Add a driving dimension to the sketch segment nearest a point.

    Works in parts and in drawings. For drawings, prefer
    ``annotate_drawing(action='model_dimensions')`` -- it pulls the model's own
    dimensions in without any coordinate picking.

    Args:
        point: ``[x, y]`` on the target segment. A line's midpoint works well.
        value: Value to drive the dimension to, in ``unit`` (or degrees when
            ``is_angle``).
        label_offset: ``[dx, dy]`` for the dimension text. Defaults to 15 mm below
            the pick point.
        is_angle: Treat ``value`` as an angle in degrees.
        unit: Unit for the coordinates and for ``value``.
    """

    @tool("add_sketch_dimension")
    def run() -> tuple[str, dict]:
        pick = R.parse_json_arg(point, "point", list)
        offset = R.parse_json_arg(label_offset, "label_offset", list) if label_offset else None
        data = sketches.add_dimension(SESSION, pick, value, offset, unit, is_angle, CONVERTER)
        return f"Dimension '{data['dimension']}' set; sketch is {data['fully_defined']}.", data

    return run()


# ===========================================================================
# Features
# ===========================================================================

@mcp.tool()
def extrude_feature(
    feature_type: str = "boss",
    depth: float | None = None,
    angle: float = 360.0,
    axis: Any = None,
    path_sketch: str | None = None,
    profiles: Any = None,
    reverse: bool = False,
    through_all: bool = False,
    mid_plane: bool = False,
    up_to_face_index: int | None = None,
    draft_angle: float = 0.0,
    draft_outward: bool = False,
    thickness: float | None = None,
    merge_result: bool = True,
    unit: str | None = None,
) -> str:
    """Turn the active sketch into a 3D feature. The sketch is consumed.

    Args:
        feature_type: ``boss`` (default solid extrusion), ``cut`` (remove material),
            ``revolve``, ``sweep``, ``loft``, or ``thin`` (thin-walled extrusion).
        depth: Depth in ``unit``. Required for a blind or mid-plane boss/cut/thin.
        angle: Revolve angle in degrees. 360 is a full revolve.
        axis: Revolve only -- ``[x1, y1, x2, y2]``, the two endpoints of the
            centreline. It is converted to construction geometry automatically.
        path_sketch: Sweep only -- the path sketch's name. Create the *profile*
            sketch last so it is the active one.
        profiles: Loft only -- ``["Sketch1", "Sketch2", ...]``, at least two.
        reverse: Flip the direction. Needed for a cut or boss sketched on a model
            face where material is on the other side. A null cut is retried in the
            opposite direction automatically, so this is usually optional.
        through_all: Through-all end condition; ``depth`` is ignored.
        mid_plane: Extrude symmetrically about the sketch plane; ``depth`` is the
            **total** width.
        up_to_face_index: Terminate exactly on this face, from
            ``analyze_model(analysis_type='faces')``; ``depth`` is ignored.
        draft_angle: Draft in degrees.
        draft_outward: Draft outward rather than inward.
        thickness: Wall thickness for ``feature_type='thin'``, in ``unit``.
        merge_result: Merge into the existing body rather than making a new one.
        unit: Unit for all lengths.
    """

    @tool("extrude_feature")
    def run() -> tuple[str, dict]:
        axis_values = R.parse_json_arg(axis, "axis", list) if axis else None
        profile_names = R.parse_json_arg(profiles, "profiles", list) if profiles else None
        data = features.extrude_feature(
            SESSION, feature_type, depth,
            angle=angle, axis=axis_values, path_sketch=path_sketch, profiles=profile_names,
            reverse=reverse, through_all=through_all, mid_plane=mid_plane,
            up_to_face_index=up_to_face_index, draft_angle=draft_angle,
            draft_outward=draft_outward, thickness=thickness, merge_result=merge_result,
            unit=unit, converter=CONVERTER,
        )
        body = data["body"]
        return (
            f"Created {data['feature']} ({data['feature_type']}). "
            f"Body now has {body['faces']} faces, {body['edges']} edges.",
            data,
        )

    return run()


@mcp.tool()
def add_edge_feature(
    feature_type: str,
    size: float,
    edge_indices: Any = None,
    edge_points: Any = None,
    face_indices: Any = None,
    chamfer_type: str = "distance_angle",
    angle: float = 45.0,
    distance2: float | None = None,
    flip: bool = False,
    propagate: bool = True,
    unit: str | None = None,
) -> str:
    """Round or chamfer solid-body edges.

    Selection priority: ``edge_indices`` (robust), then ``face_indices`` (every edge
    of those faces), then ``edge_points``. Get indices from
    ``analyze_model(analysis_type='edges')`` -- coordinate picks cannot disambiguate
    crowded or concave edges.

    Args:
        feature_type: ``fillet`` or ``chamfer``.
        size: Fillet radius, or the chamfer's first-face setback, in ``unit``.
        edge_indices: ``[3, 5, 7]`` from ``analyze_model(edges)``.
        face_indices: Fillet or chamfer every edge of these faces.
        edge_points: ``[[x, y, z], ...]`` points on the target edges.
        chamfer_type: ``distance_angle`` (default), ``distance_distance``, or
            ``equal_distance``.
        angle: Chamfer angle in degrees, for ``distance_angle``.
        distance2: Second-face setback for ``distance_distance``, in ``unit``.
        flip: Swap which face gets which setback.
        propagate: Continue the fillet around tangent edges.
        unit: Unit for ``size`` and ``distance2``.
    """

    @tool("add_edge_feature")
    def run() -> tuple[str, dict]:
        data = features.add_edge_feature(
            SESSION, feature_type, size,
            edge_indices=R.parse_json_arg(edge_indices, "edge_indices", list)
            if edge_indices else None,
            edge_points=R.parse_json_arg(edge_points, "edge_points", list)
            if edge_points else None,
            face_indices=R.parse_json_arg(face_indices, "face_indices", list)
            if face_indices else None,
            chamfer_type=chamfer_type, angle=angle, distance2=distance2, flip=flip,
            propagate=propagate, unit=unit, converter=CONVERTER,
        )
        return f"Created {data['feature']} on {data['selected']}.", data

    return run()


@mcp.tool()
def create_rib(
    thickness: float,
    two_sided: bool = True,
    reverse_thickness: bool = False,
    reverse_material: bool = False,
    normal_to_sketch: bool = False,
    draft_angle: float = 0.0,
    unit: str | None = None,
) -> str:
    """Create a rib from the active sketch, which is consumed.

    The profile is **open** geometry -- typically one line whose extensions reach
    existing walls, like an L-bracket's diagonal. SolidWorks extends and thickens
    it. If the chosen material direction has nothing to fill, the opposite is
    retried automatically.

    Args:
        thickness: Rib thickness in ``unit``.
        two_sided: Thicken symmetrically about the sketch.
        reverse_thickness: Single-sided only -- thicken the other side.
        reverse_material: Flip which way the material grows.
        normal_to_sketch: Extrude normal to the sketch rather than parallel to it.
        draft_angle: Draft in degrees.
        unit: Unit for ``thickness``.
    """

    @tool("create_rib")
    def run() -> tuple[str, dict]:
        data = features.create_rib(
            SESSION, thickness, two_sided=two_sided, reverse_thickness=reverse_thickness,
            reverse_material=reverse_material, normal_to_sketch=normal_to_sketch,
            draft_angle=draft_angle, unit=unit, converter=CONVERTER,
        )
        return f"Created rib {data['feature']}.", data

    return run()


@mcp.tool()
def create_shell(
    thickness: float,
    face_indices: Any = None,
    outward: bool = False,
    unit: str | None = None,
) -> str:
    """Hollow the solid to a uniform wall, optionally opening it through faces.

    Args:
        thickness: Wall thickness in ``unit``. It must be less than half the part's
            thinnest section.
        face_indices: Faces to remove, from ``analyze_model(analysis_type='faces')``.
            Omit for a closed hollow body.
        outward: Add the wall outward, growing the part, instead of inward.
        unit: Unit for ``thickness``.
    """

    @tool("create_shell")
    def run() -> tuple[str, dict]:
        data = features.create_shell(
            SESSION, thickness,
            face_indices=R.parse_json_arg(face_indices, "face_indices", list)
            if face_indices else None,
            outward=outward, unit=unit, converter=CONVERTER,
        )
        return (
            f"Created {data['feature']} ({data['faces_removed']} face(s) removed).",
            data,
        )

    return run()


@mcp.tool()
def create_hole(
    face_index: int,
    position: Any,
    diameter: float,
    depth: float | None = None,
    through_all: bool = False,
    unit: str | None = None,
) -> str:
    """Cut a round hole on a face in one call.

    Simpler than sketching a circle and cut-extruding it, and the result stays a
    recognisable Hole feature, so drawings can auto-generate hole callouts for it.

    Args:
        face_index: Target face, from ``analyze_model(analysis_type='faces')``.
        position: ``[x, y, z]`` hole centre in model coordinates, on that face.
        diameter: Hole diameter in ``unit``.
        depth: Blind depth in ``unit``. Required unless ``through_all``.
        through_all: Cut through the whole body.
        unit: Unit for ``diameter``, ``depth`` and ``position``.
    """

    @tool("create_hole")
    def run() -> tuple[str, dict]:
        data = features.create_hole(
            SESSION, face_index, R.parse_json_arg(position, "position", list), diameter,
            depth=depth, through_all=through_all, unit=unit, converter=CONVERTER,
        )
        return f"Created {data['feature']}.", data

    return run()


@mcp.tool()
def add_reference_geometry(
    geometry_type: str,
    base_plane: str | None = None,
    offset: float = 0.0,
    entity1: str | None = None,
    entity1_type: str = "PLANE",
    entity2: str | None = None,
    entity2_type: str = "PLANE",
    point: Any = None,
    hide: bool = True,
    unit: str | None = None,
) -> str:
    """Create a reference plane, axis or point.

    A reference axis is required before a circular pattern; offset planes are how
    you sketch away from the default three.

    Args:
        geometry_type: ``plane``, ``axis`` or ``point``.
        base_plane: Plane only -- the plane to offset from.
        offset: Plane only -- **signed** offset distance in ``unit``. A negative
            value offsets the other way (handled correctly, unlike a raw API call,
            where a negative distance is silently ignored and the new plane lands on
            top of its parent).
        entity1: Axis only -- first plane or edge name.
        entity1_type: ``PLANE`` or ``EDGE``.
        entity2: Axis only -- second plane or edge name.
        entity2_type: ``PLANE`` or ``EDGE``.
        point: Point only -- ``[x, y, z]`` of an existing vertex.
        hide: Hide the created geometry so it does not clutter the view. Selection
            by name still works when hidden.
        unit: Unit for ``offset`` and ``point``.
    """

    @tool("add_reference_geometry")
    def run() -> tuple[str, dict]:
        data = features.add_reference_geometry(
            SESSION, geometry_type, base_plane=base_plane, offset=offset,
            entity1=entity1, entity1_type=entity1_type,
            entity2=entity2, entity2_type=entity2_type,
            point=R.parse_json_arg(point, "point", list) if point else None,
            hide=hide, unit=unit, converter=CONVERTER,
        )
        return f"Created {data['geometry_type']} '{data['feature']}'.", data

    return run()


@mcp.tool()
def create_pattern(
    pattern_type: str,
    features_list: Any,
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
) -> str:
    """Repeat features linearly or circularly, or mirror them about a plane.

    Args:
        pattern_type: ``linear``, ``circular`` or ``mirror``.
        features_list: Seed feature names, e.g. ``["Boss-Extrude1"]``. Linear and
            circular use the first; mirror uses all of them.
        count: Instances **including** the seed.
        spacing: Distance between instances in ``unit`` (linear).
        direction: ``X``, ``Y`` or ``Z`` for a linear pattern.
        flip: Pattern toward the negative axis.
        count2: Second-direction count (linear).
        spacing2: Second-direction spacing in ``unit``.
        axis: Circular only -- the reference axis name. Create one first with
            ``add_reference_geometry(geometry_type='axis')``.
        angle: Circular only, in degrees. With ``equal_spacing`` it is the **total**
            spread (360 for a full ring); otherwise the angle **between** instances.
        equal_spacing: See ``angle``.
        plane: Mirror only -- the mirror plane name.
        geometry_pattern: Pattern geometry without re-solving each feature. Faster,
            but fails on some configurations, so it is off by default.
        unit: Unit for ``spacing`` and ``spacing2``.
    """

    @tool("create_pattern")
    def run() -> tuple[str, dict]:
        names = R.parse_json_arg(features_list, "features_list", list)
        data = features.create_pattern(
            SESSION, pattern_type, features=names, count=count, spacing=spacing,
            direction=direction, flip=flip, count2=count2, spacing2=spacing2,
            axis=axis, angle=angle, equal_spacing=equal_spacing, plane=plane,
            geometry_pattern=geometry_pattern, unit=unit, converter=CONVERTER,
        )
        return f"Created {data['pattern_type']} pattern {data['feature']}.", data

    return run()


@mcp.tool()
def set_material(
    material: str,
    library: str = "SolidWorks Materials",
    configuration: str = "",
) -> str:
    """Assign a material to the active part.

    Required before mass or centre-of-gravity figures mean anything.

    Args:
        material: Exact library name. SolidWorks is unforgiving -- ``"1060 Alloy"``
            not ``"Aluminum 1060 Alloy"``; ``"AISI 1020"``; ``"ABS"``.
        library: Material database name.
        configuration: Configuration to apply to. Empty means all of them.
    """

    @tool("set_material")
    def run() -> tuple[str, dict]:
        data = features.set_material(SESSION, material, library, configuration)
        return f"Applied '{data['material']}' from {data['library']}.", data

    return run()


@mcp.tool()
def modify_dimension(
    name: str,
    value: float,
    is_angle: bool = False,
    unit: str | None = None,
) -> str:
    """Drive a named dimension to a new value -- the way to build size variants.

    Pairs with ``analyze_model(analysis_type='features')``, which reports every
    dimension's full name and current value. The effective value is read back after
    rebuilding and flagged if SolidWorks clamped it, since an equation silently
    overriding your value is the failure worth catching immediately.

    Args:
        name: Dimension full name exactly as analysis reports it, e.g.
            ``"D1@Boss-Extrude1@bracket.Part"``.
        value: New value in ``unit``, or degrees when ``is_angle``.
        is_angle: Treat ``value`` as an angle.
        unit: Unit for a length value.
    """

    @tool("modify_dimension")
    def run() -> tuple[str, dict]:
        data = features.modify_dimension(
            SESSION, name, value, is_angle=is_angle, unit=unit, converter=CONVERTER
        )
        note = " (SolidWorks clamped it -- an equation is driving this)" if data["clamped"] else ""
        return f"Set {name} to {value}{note}.", data

    return run()


@mcp.tool()
def edit_feature(
    action: str,
    feature: str | None = None,
    new_name: str | None = None,
) -> str:
    """Suppress, unsuppress, delete or rename a feature, or move the rollback bar.

    Suppress, delete and rollback all change topology, so face and edge indices
    captured earlier will point elsewhere -- re-run ``analyze_model`` afterwards.

    Args:
        action: ``suppress``, ``unsuppress``, ``delete``, ``rename``,
            ``rollback_to`` (roll the bar to just before ``feature``), or
            ``rollback_end``.
        feature: Exact feature-tree name. Not needed for ``rollback_end``.
        new_name: Required for ``rename``.
    """

    @tool("edit_feature")
    def run() -> tuple[str, dict]:
        data = features.edit_feature(SESSION, action, feature=feature, new_name=new_name)
        return f"{data['action']} applied to '{feature or 'tree'}'.", data

    return run()


# ===========================================================================
# Sheet metal
# ===========================================================================

@mcp.tool()
def sheet_metal_feature(
    feature_type: str,
    thickness: float | None = None,
    bend_radius: float | None = None,
    k_factor: float = 0.5,
    reverse_thickness: bool = False,
    symmetric_thickness: bool = False,
    edge_index: int | None = None,
    edge_point: Any = None,
    flange_length: float | None = None,
    angle: float = 90.0,
    bend_position: str = "material_inside",
    use_default_radius: bool = False,
    flip: bool = False,
    fixed_face_index: int | None = None,
    fixed_point: Any = None,
    clear_profile: bool = True,
    unit: str | None = None,
) -> str:
    """Create or modify sheet-metal geometry.

    Args:
        feature_type:
            * ``base_flange`` -- convert the active sketch into the sheet-metal base.
              Needs ``thickness``.
            * ``edge_flange`` -- add a default-profile flange to an edge.
            * ``edge_flange_sketch`` -- step 1 of a custom-profile flange: generate
              the edge-linked profile sketch and leave it open. Returns the sketch's
              measured frame, which you must use when drawing into it.
            * ``edge_flange_finish`` -- step 2: create the flange from the drawn
              profile, using the **same** ``edge_index``.
            * ``sketched_bend`` -- bend about the bend line(s) in the active sketch.
              Needs the fixed face.
            * ``flat_pattern`` -- unfold all bends.
            * ``fold`` -- re-fold.
        thickness: Sheet thickness in ``unit`` (base flange).
        bend_radius: Bend radius in ``unit``. Defaults to the sheet thickness.
        k_factor: Bend-allowance K-factor.
        reverse_thickness: Thicken to the opposite side of the sketch plane. This
            also sets the sheet's intrinsic orientation, which later bends fold
            against -- when reproducing an existing part, match its flag.
        symmetric_thickness: Thicken both ways about the sketch plane.
        edge_index: Attach edge, from ``analyze_model(analysis_type='edges')``.
            **Preferred** over ``edge_point``.
        edge_point: ``[x, y, z]`` on the attach edge.
        flange_length: Edge flange length in ``unit``.
        angle: Bend angle in degrees.
        bend_position: ``material_inside`` (default), ``material_outside``,
            ``bend_outside``, ``centerline`` or ``bend_sharp``.
        use_default_radius: Use the sheet's default bend radius.
        flip: Reverse the bend direction.
        fixed_face_index: Sketched bend -- the face that stays put, from
            ``analyze_model(analysis_type='faces')``.
        fixed_point: ``[x, y, z]`` on the fixed face.
        clear_profile: ``edge_flange_sketch`` only -- wipe the generated default
            rectangle so you can draw a custom outline.
        unit: Unit for all lengths.
    """

    @tool("sheet_metal_feature")
    def run() -> tuple[str, dict]:
        data = sheetmetal.sheet_metal_feature(
            SESSION, feature_type, thickness=thickness, bend_radius=bend_radius,
            k_factor=k_factor, reverse_thickness=reverse_thickness,
            symmetric_thickness=symmetric_thickness, edge_index=edge_index,
            edge_point=R.parse_json_arg(edge_point, "edge_point", list) if edge_point else None,
            flange_length=flange_length, angle=angle, bend_position=bend_position,
            use_default_radius=use_default_radius, flip=flip,
            fixed_face_index=fixed_face_index,
            fixed_point=R.parse_json_arg(fixed_point, "fixed_point", list) if fixed_point else None,
            clear_profile=clear_profile, unit=unit, converter=CONVERTER,
        )
        label = data.get("feature") or data.get("sketch")
        return f"{data['feature_type']}: {label}.", data

    return run()


# ===========================================================================
# Drawings
# ===========================================================================

@mcp.tool()
def create_drawing_view(
    view_type: str,
    position: Any = None,
    scale: float | None = None,
    model_path: str | None = None,
    display_mode: str | None = None,
    configuration: str | None = None,
    hide_bend_lines: bool = False,
    flip: bool = False,
    section_line: Any = None,
    section_edge_point: Any = None,
    label: str = "A",
    unit: str | None = None,
) -> str:
    """Place a view on the active drawing sheet.

    The referenced model must be saved to disk -- views resolve by file path.

    Args:
        view_type: A projection (``front``, ``back``, ``left``, ``right``, ``top``,
            ``bottom``, ``isometric``, ``trimetric``, ``dimetric``, ``current``);
            ``flat_pattern`` for the unfolded sheet-metal blank (the correct way to
            detail sheet metal); or ``section`` to expose internal features as
            dimensionable edges.
        position: ``[x, y]`` on the sheet in ``unit``. Defaults to 100, 100.
        scale: View scale. Inherits the sheet's when omitted.
        model_path: Part or assembly path. Auto-detects the first saved open model.
        display_mode: ``wireframe``, ``hlv``, ``hlr``, ``shaded``, ``shaded_edges``.
            Omitted gives the drafting default: hidden-lines-visible for
            orthographic views, document default for isometric.
        configuration: Model configuration to show.
        hide_bend_lines: Flat pattern only.
        flip: Mirror a flat pattern, or reverse a section's cut direction.
        section_line: Section only -- ``[x1, y1, x2, y2]``. Use this to cut through
            a pocket's interior where no edge exists.
        section_edge_point: Section only -- ``[x, y]`` on an existing projected edge
            to cut along.
        label: Section letter, e.g. ``"A"`` gives "SECTION A-A".
        unit: Unit for all sheet coordinates.
    """

    @tool("create_drawing_view")
    def run() -> tuple[str, dict]:
        data = drawings.add_view(
            SESSION, view_type,
            position=R.parse_json_arg(position, "position", list) if position else None,
            scale=scale, model_path=model_path, display_mode=display_mode,
            configuration=configuration, hide_bend_lines=hide_bend_lines, flip=flip,
            section_line=R.parse_json_arg(section_line, "section_line", list)
            if section_line else None,
            section_edge_point=R.parse_json_arg(
                section_edge_point, "section_edge_point", list)
            if section_edge_point else None,
            label=label, unit=unit, converter=CONVERTER,
        )
        return f"Added {data['view_type']} view '{data['view']}'.", data

    return run()


@mcp.tool()
def annotate_drawing(
    action: str,
    all_views: bool = True,
    include_unmarked: bool = False,
    eliminate_duplicates: bool = True,
    include_slots: bool = True,
    extended_lines: bool = True,
    point: Any = None,
    value: float | None = None,
    label_offset: Any = None,
    text: str | None = None,
    unit: str | None = None,
) -> str:
    """Annotate the active drawing.

    Args:
        action:
            * ``model_dimensions`` -- pull the model's own driving dimensions into
              the views. **The robust default**: correctly placed, no coordinate
              picking, values guaranteed to match the model.
            * ``center_marks`` -- auto-place centre marks with centrelines on every
              hole and slot in every view.
            * ``hole_callout`` -- a callout on the hole nearest ``point``.
            * ``dimension`` -- a dimension on the projected edge nearest ``point``.
            * ``note`` -- free text at ``point``.
        all_views: ``model_dimensions`` -- insert into every view.
        include_unmarked: ``model_dimensions`` -- also insert dimensions not marked
            for drawing. Noisier but complete; try it when the default inserts zero.
        eliminate_duplicates: ``model_dimensions`` -- do not repeat a dimension
            across views.
        include_slots: ``center_marks`` -- also mark linear and arc slots.
        extended_lines: ``center_marks`` -- full centrelines rather than a small cross.
        point: ``[x, y]`` sheet coordinates, for the picking actions.
        value: ``dimension`` -- the value to drive to, in ``unit``.
        label_offset: ``dimension`` -- ``[dx, dy]`` for the label.
        text: ``note`` -- the note text.
        unit: Unit for sheet coordinates and ``value``.
    """

    @tool("annotate_drawing")
    def run() -> tuple[str, dict]:
        data = drawings.annotate(
            SESSION, action, all_views=all_views, include_unmarked=include_unmarked,
            eliminate_duplicates=eliminate_duplicates, include_slots=include_slots,
            extended_lines=extended_lines,
            point=R.parse_json_arg(point, "point", list) if point else None,
            value=value,
            label_offset=R.parse_json_arg(label_offset, "label_offset", list)
            if label_offset else None,
            text=text, unit=unit, converter=CONVERTER,
        )
        counted = data.get("inserted", data.get("center_marks"))
        return (
            f"{data['action']}"
            + (f": {counted} placed." if counted is not None else " applied."),
            data,
        )

    return run()


@mcp.tool()
def analyze_drawing(include_geometry: bool = False) -> str:
    """Read the active drawing's views, dimensions and optionally projected geometry.

    Use it to verify a produced drawing's dimensions match the model, or to
    reverse-engineer a part from its drawing.

    Args:
        include_geometry: Also return each view's projected 2D primitives, in
            model-scale metres centred on the view.
    """

    @tool("analyze_drawing")
    def run() -> tuple[str, dict]:
        data = drawings.analyze_drawing(SESSION, include_geometry, CONVERTER)
        return (
            f"{data['view_count']} view(s), {data['dimension_count']} dimension(s).",
            data,
        )

    return run()


# ===========================================================================
# Assemblies
# ===========================================================================

@mcp.tool()
def insert_component(
    file_path: str,
    position: Any = None,
    configuration: str | None = None,
    fixed: bool | None = None,
    transform: Any = None,
    unit: str | None = None,
) -> str:
    """Insert a component into the active assembly.

    Args:
        file_path: Part or sub-assembly file to insert.
        position: ``[x, y, z]`` insertion point in ``unit``. Defaults to the origin.
        configuration: Referenced configuration name.
        fixed: Fix or float the instance explicitly. Worth passing -- SolidWorks
            auto-fixes the first component in some flows, so this makes the state
            deterministic.
        transform: Full placement as 13 numbers -- 3x3 rotation row-major, then
            translation xyz in **metres**, then scale. Needed for arbitrary
            rotations; overrides ``position``.
        unit: Unit for ``position``.
    """

    @tool("insert_component")
    def run() -> tuple[str, dict]:
        data = assemblies.insert_component(
            SESSION, file_path,
            position=R.parse_json_arg(position, "position", list) if position else None,
            configuration=configuration, fixed=fixed,
            transform=R.parse_json_arg(transform, "transform", list) if transform else None,
            unit=unit, converter=CONVERTER,
        )
        return (
            f"Inserted '{data['component']}' "
            f"({'fixed' if data['fixed'] else 'floating'}); "
            f"{data['component_count']} component(s) total.",
            data,
        )

    return run()


@mcp.tool()
def add_mate(
    mate_type: str,
    entity_a: Any,
    entity_b: Any,
    alignment: str = "closest",
    flip: bool = False,
    value: float | None = None,
    unit: str | None = None,
) -> str:
    """Mate two component entities in the active assembly.

    Entity selection is index-based, from
    ``analyze_assembly(analysis_type='faces'|'edges', component=...)``. Coordinate
    picking is not offered: components overlap in space, so a point that looks
    unambiguous routinely resolves to the wrong body.

    Args:
        mate_type: ``coincident``, ``concentric``, ``perpendicular``, ``parallel``,
            ``tangent``, ``distance``, ``angle``, ``lock``, ``symmetric``, ``width``.
        entity_a: ``{"component": "Shaft-1", "kind": "face", "index": 3}``.
            ``kind`` is ``face`` (default) or ``edge``.
        entity_b: Side B, same shape.
        alignment: ``aligned``, ``anti_aligned`` or ``closest`` (default).
        flip: Flip the mate.
        value: Distance in ``unit`` for a distance mate, or degrees for an angle mate.
        unit: Unit for a distance value.

    Returns:
        The mate name plus both components' resulting transforms, so you can confirm
        the mate positioned things as intended rather than solving to a different
        valid configuration.
    """

    @tool("add_mate")
    def run() -> tuple[str, dict]:
        data = assemblies.add_mate(
            SESSION, mate_type,
            R.parse_json_arg(entity_a, "entity_a", dict),
            R.parse_json_arg(entity_b, "entity_b", dict),
            alignment=alignment, flip=flip, value=value, unit=unit, converter=CONVERTER,
        )
        return f"Added {data['mate_type']} mate '{data['mate']}'.", data

    return run()


@mcp.tool()
def analyze_assembly(analysis_type: str = "components", component: str | None = None) -> str:
    """Read the active assembly without modifying it.

    Args:
        analysis_type:
            * ``components`` -- top-level occurrences with path, configuration,
              fixed and suppressed state, and the full 13-number transform.
            * ``components_flat`` -- leaf occurrences across all nesting levels.
            * ``mates`` -- mates in creation order with type, alignment and SI value.
            * ``faces`` / ``edges`` -- that component's entities with the stable
              indices ``add_mate`` consumes. Requires ``component``. Coordinates are
              component-local; the component transform is included for mapping.
        component: Component instance name, e.g. ``"Shaft-1"``. Required for
            ``faces`` and ``edges``.
    """

    @tool("analyze_assembly")
    def run() -> tuple[str, dict]:
        data = assemblies.analyze_assembly(
            SESSION, analysis_type, component=component, converter=CONVERTER
        )
        counts = {k: v for k, v in data.items() if k.endswith("_count")}
        return f"{analysis_type}: " + ", ".join(f"{v} {k[:-6]}" for k, v in counts.items()), data

    return run()


@mcp.tool()
def save_body_as_part(body_index: int, file_path: str) -> str:
    """Extract one body of a multibody part into its own part file.

    The missing half of flattened-assembly reconstruction: a structured assembly STEP
    with no per-part files imports as one multibody part, and each body must become a
    referenced part file before an assembly can be rebuilt. Geometry keeps the source
    coordinates, so placements survive.

    Args:
        body_index: Body index from ``analyze_model(analysis_type='bodies')``.
        file_path: Target ``.sldprt`` path. Parent directories are created.
    """

    @tool("save_body_as_part")
    def run() -> tuple[str, dict]:
        data = assemblies.save_body_as_part(SESSION, body_index, file_path)
        return f"Saved body {body_index} to {data['path']}.", data

    return run()


# ===========================================================================
# Analysis
# ===========================================================================

@mcp.tool()
def analyze_model(
    analysis_type: str = "summary",
    name: str | None = None,
    near: Any = None,
    limit: int | None = None,
    axis: Any = None,
    unit: str | None = None,
) -> str:
    """Inspect the active part. Never modifies anything.

    This is the source of the **stable indices** every selection-based tool consumes.

    Args:
        analysis_type:
            * ``summary`` (default) -- topology, volume, mass, bounding box and
              material in one cheap call. Start here.
            * ``mass_properties`` -- volume, area, mass, centre of gravity.
            * ``geometry`` -- body, face, edge and vertex counts.
            * ``bodies`` -- per-body counts, volume, area, centroid.
            * ``faces`` -- every face with index ``i``; planar faces carry a normal,
              area and a point. Feed ``i`` to ``create_sketch(face_index=...)`` or
              ``extrude_feature(up_to_face_index=...)``.
            * ``edges`` -- every edge with index ``i``, endpoints, midpoint, length.
              Feed ``i`` to ``add_edge_feature(edge_indices=[...])``.
            * ``features`` -- the feature tree with types, suppression states and
              driving dimensions. The source of the names ``modify_dimension`` needs.
            * ``sketch`` -- one sketch's full geometry. Requires ``name``.
            * ``configurations`` -- configuration names and the active one.
        name: Sketch name, for ``analysis_type='sketch'``.
        near: ``[x, y, z]`` -- faces and edges only: return just the entities nearest
            this point, annotated with distance. Turns a 5000-face import into a few
            relevant candidates.
        limit: With ``near``, keep only the closest ``limit`` entities.
        axis: ``[x, y, z]`` -- faces and edges only: keep only entities roughly
            parallel to this direction.
        unit: Unit for ``near`` and reported display lengths.
    """

    @tool("analyze_model")
    def run() -> tuple[str, dict]:
        data = analysis.analyze_model(
            SESSION, analysis_type, name=name,
            near=R.parse_json_arg(near, "near", list) if near else None,
            limit=limit,
            axis=R.parse_json_arg(axis, "axis", list) if axis else None,
            unit=unit, converter=CONVERTER,
        )
        counts = {k: v for k, v in data.items() if k.endswith("_count")}
        headline = ", ".join(f"{v} {k[:-6]}" for k, v in counts.items()) or analysis_type
        return f"{analysis_type}: {headline}", data

    return run()


@mcp.tool()
def get_selection() -> str:
    """Report what the user currently has selected in the SolidWorks window.

    The bridge between a human pointing at geometry and a tool call: each selected
    face or edge is mapped to the **same stable index** ``analyze_model`` reports, so
    "fillet this edge" becomes ``add_edge_feature(edge_indices=[i])``.

    Read-only, and it does not clear the selection -- but call it *before* any tool
    that does (``create_sketch``, ``add_edge_feature``, any cut).
    """

    @tool("get_selection")
    def run() -> tuple[str, dict]:
        data = analysis.get_selection(SESSION)
        return f"{data['selected_count']} entity(ies) selected.", data

    return run()


@mcp.tool()
def compare_documents(
    document_a: str,
    document_b: str,
    volume_tolerance_percent: float = 1.0,
    area_tolerance_percent: float = 1.0,
) -> str:
    """Objectively compare two parts or assemblies.

    The verification half of any rebuild workflow: prove a rebuilt part matches the
    original instead of eyeballing it. Each argument is either a file path (opened
    from disk) or the title of an already-open document, so unsaved rebuilds compare
    too.

    Args:
        document_a: Reference document -- deltas are relative to it.
        document_b: Candidate document.
        volume_tolerance_percent: Volume delta allowed for a pass.
        area_tolerance_percent: Surface-area delta allowed for a pass.
    """

    @tool("compare_documents")
    def run() -> tuple[str, dict]:
        data = analysis.compare_documents(
            SESSION, document_a, document_b,
            volume_tolerance_percent=volume_tolerance_percent,
            area_tolerance_percent=area_tolerance_percent,
        )
        return (
            f"{'PASS' if data['passed'] else 'FAIL'} -- topology "
            f"{'matches' if data['topology_matches'] else 'differs'}, "
            f"volume delta {data['volume_delta_percent']}%.",
            data,
        )

    return run()


# ===========================================================================
# Scripting
# ===========================================================================

@mcp.tool()
def execute_python(code: str) -> str:
    """Run Python against the live SolidWorks COM objects.

    The escape hatch for anything the tools do not cover. Runs on the same
    apartment-threaded COM thread as every other call, so the full API surface
    behaves as it does inside the tools.

    Available names: ``sw`` (the application), ``doc`` (the active document),
    ``session``, ``compat`` (use ``compat.pm(obj, "GetTitle")`` for
    version-portable attribute access, plus ``invoke_first`` and ``iter_features``),
    ``sel`` (selection and topology helpers such as ``sel.flatten_faces(doc)``),
    ``enums``, ``units``, ``uc`` (a unit converter), ``math``, ``json``, ``os``.

    Set a ``result`` variable or just ``print`` -- both come back.

    Args:
        code: Python source to execute.

    Example::

        faces = sel.flatten_faces(doc)
        result = [{"i": i, "area": compat.pm(f, "GetArea")} for i, f in enumerate(faces)]
    """

    @tool("execute_python")
    def run() -> tuple[str, dict]:
        data = scripting.execute_python(SESSION, code, CONVERTER)
        return "Script completed.", data

    return run()


@mcp.tool()
def run_macro(file_path: str, module: str, procedure: str = "main") -> str:
    """Run an existing SolidWorks macro (``.swp``, ``.swb`` or ``.dll``).

    Lets accumulated VBA macro work stay usable from an agent instead of needing a
    rewrite.

    Args:
        file_path: Absolute path to the macro file.
        module: Module name inside the macro.
        procedure: Procedure to call.
    """

    @tool("run_macro")
    def run() -> tuple[str, dict]:
        data = scripting.run_macro(SESSION, file_path, module, procedure)
        return f"Ran {module}.{procedure}.", data

    return run()


# ===========================================================================

def main() -> None:
    """Run the server over stdio."""
    logger.info("Starting SolidWorks MCP server (config: %s)", CONFIG.to_dict()["config_source"])
    try:
        mcp.run()
    finally:
        SESSION.shutdown()


if __name__ == "__main__":
    main()
