"""
Assembly operations: component insertion, mates, and assembly analysis.

Mate entity selection is **index-first**, the same discipline the part tools use:
each side is (component name, face-or-edge, index from
``analyze_assembly(analysis_type="faces"|"edges")``). Coordinate picking is not
offered here at all, because in an assembly it is close to unusable -- components
overlap in space, and a point that looks unambiguous on screen routinely resolves
to the wrong body's face.

Component coordinates from ``analyze_assembly`` are **component-local**; the
payload carries each component's transform so a caller can map them into assembly
space when needed.
"""

from __future__ import annotations

import logging
import os

from ..com import compat, enums, selection
from ..com.compat import byref_int, double_array, nothing, pm
from ..com.connection import SolidWorksError, SolidWorksSession
from ..util import units
from ..util.result import ToolError, one_of, require

logger = logging.getLogger(__name__)

ANALYSIS_TYPES = {"components", "components_flat", "mates", "faces", "edges"}


def _assembly(session: SolidWorksSession):
    return session.require_doc("ASSEMBLY")


def _transform_values(component, precision: int = 9) -> list[float] | None:
    """A component's placement as 13 numbers: 3x3 rotation row-major, translation, scale.

    This is the assembly verification ground truth -- comparing transforms is how
    you tell whether a rebuilt assembly actually positions its parts the same way.
    """
    transform = pm(component, "Transform2", default=None)
    if transform is None:
        return None
    values = compat.as_floats(pm(transform, "ArrayData", default=None))
    if len(values) < 13:
        return None
    return [round(v, precision) for v in values[:13]]


def _iter_components(assembly, top_level_only: bool = True) -> list:
    """Components in **feature-tree order**.

    ``GetComponents`` returns an arbitrary order, so the tree walk is used instead:
    stable ordering is what makes an index or an ordinal meaningful across reads.
    """
    components = compat.as_list(pm(assembly, "GetComponents", top_level_only, default=None))
    return components


def _find_component(assembly, name: str):
    """Locate a component by its instance-numbered name (``"Shaft-1"``)."""
    for component in _iter_components(assembly, False):
        if pm(component, "Name2", default=None) == name:
            return component
    return None


def _component_names(assembly) -> list[str]:
    return [
        str(pm(c, "Name2", default="")) for c in _iter_components(assembly, True)
    ]


# ---------------------------------------------------------------------------
# insert_component
# ---------------------------------------------------------------------------

def insert_component(
    session: SolidWorksSession,
    file_path: str,
    *,
    position: list | None = None,
    configuration: str | None = None,
    fixed: bool | None = None,
    transform: list | None = None,
    unit: str | None = None,
    converter: units.UnitConverter | None = None,
) -> dict:
    """Insert a component instance into the active assembly.

    The component document must be loaded before ``AddComponent5`` will accept it,
    so it is preloaded silently and the assembly is then re-activated -- explicitly,
    because ``CloseDoc`` leaves the active document unpredictable and a later call
    would otherwise operate on the wrong document.

    Args:
        file_path: The part or sub-assembly file to insert.
        position: ``[x, y, z]`` insertion point in ``unit``. Defaults to the origin.
        configuration: Referenced configuration name.
        fixed: Fix or float the instance explicitly. SolidWorks auto-fixes the first
            component in some flows, so passing this makes the state deterministic
            either way.
        transform: Full placement as 13 numbers -- 3x3 rotation row-major, then
            translation xyz in **metres**, then scale. Needed for arbitrary
            rotations that ``position`` alone cannot express; overrides ``position``.
        unit: Unit for ``position``.
    """
    assembly = _assembly(session)
    converter = converter or units.UnitConverter(session.config.default_unit)

    if not os.path.isfile(file_path):
        raise ToolError(
            "FILE_NOT_FOUND",
            f"No file at '{file_path}'.",
            hint="The component must exist on disk before it can be inserted.",
        )

    coords = position or [0.0, 0.0, 0.0]
    if len(coords) < 3:
        raise ToolError("INVALID_PARAMETER", "position needs x, y and z.")
    x, y, z = converter.points_to_meters(coords[:3], unit)

    assembly_title = pm(assembly, "GetTitle", default=None)

    # AddComponent5 requires the component document to be loaded already.
    errors, warnings = byref_int(), byref_int()
    extension = os.path.splitext(file_path)[1].lower()
    doc_type = enums.DOC_TYPE_BY_EXT.get(extension, enums.DOC_PART)
    preloaded = session.app.OpenDoc6(
        file_path, doc_type, enums.OPEN_SILENT, configuration or "", errors, warnings
    )
    if preloaded is None:
        raise SolidWorksError(
            "OPEN_FAILED",
            f"Could not preload '{os.path.basename(file_path)}' for insertion "
            f"(errors={errors.value}).",
            {"errors": errors.value},
        )
    preloaded_title = pm(preloaded, "GetTitle", default=None)

    # OpenDoc6 activates what it opened; put the assembly back in front.
    activate_errors = byref_int()
    pm(session.app, "ActivateDoc3", assembly_title, False,
       enums.ACTIVATE_DONT_REBUILD, activate_errors, default=None)

    report = compat.invoke_first(assembly, [
        ("AddComponent5", (
            file_path, enums.ADD_COMPONENT_CURRENT_SELECTED_CONFIG, "", False,
            configuration or "", x, y, z,
        )),
        ("AddComponent4", (file_path, configuration or "", x, y, z)),
        ("AddComponent", (file_path, x, y, z)),
    ])

    if not report.ok:
        raise SolidWorksError(
            "INSERT_FAILED",
            f"Inserting '{os.path.basename(file_path)}' returned null "
            f"(configuration='{configuration or 'default'}'). The file must be a loadable part "
            f"or assembly document.",
            {"attempts": report.diagnostic()},
        )

    component = report.value
    applied_transform = None
    if transform:
        if len(transform) < 13:
            raise ToolError(
                "INVALID_PARAMETER",
                f"transform needs 13 numbers (9 rotation row-major + 3 translation + scale), "
                f"got {len(transform)}.",
            )
        # CreateTransform wants a 16-element array; the last three are unused.
        padded = [float(v) for v in transform[:13]] + [0.0, 0.0, 0.0]
        math_utility = pm(session.app, "IGetMathUtility", default=None) or pm(
            session.app, "GetMathUtility", default=None)
        if math_utility is None:
            raise SolidWorksError(
                "TRANSFORM_FAILED", "The math utility is not available on this build."
            )
        matrix = pm(math_utility, "CreateTransform", double_array(padded), default=None)
        if matrix is None:
            raise SolidWorksError(
                "TRANSFORM_FAILED", "CreateTransform rejected the supplied 13-number array."
            )
        try:
            component.Transform2 = matrix
            applied_transform = transform[:13]
        except Exception as exc:
            raise SolidWorksError(
                "TRANSFORM_FAILED", f"Could not apply the transform: {exc}"
            ) from None

    fixed_state = bool(pm(component, "IsFixed", default=False))
    if fixed is not None and fixed_state != fixed:
        selection.clear(assembly)
        if pm(component, "Select4", False, nothing(), False, default=False):
            compat.invoke_any(assembly, [
                ("FixComponent", ()) if fixed else ("UnfixComponent", ()),
            ])
        selection.clear(assembly)
        fixed_state = bool(pm(component, "IsFixed", default=fixed))

    # Close the standalone preload; the assembly keeps its own reference.
    if preloaded_title and preloaded_title != assembly_title:
        try:
            session.app.CloseDoc(preloaded_title)
        except Exception:
            pass
    # CloseDoc leaves the active document unpredictable -- re-activate explicitly.
    pm(session.app, "ActivateDoc3", assembly_title, False,
       enums.ACTIVATE_DONT_REBUILD, byref_int(), default=None)

    compat.invoke_any(assembly, [("EditRebuild3", ())])

    return {
        "component": pm(component, "Name2", default=None),
        "file": file_path,
        "configuration": pm(component, "ReferencedConfiguration", default=configuration),
        "fixed": fixed_state,
        "position": {"x": coords[0], "y": coords[1], "z": coords[2],
                     "unit": units.parse_unit(unit, converter.default).value},
        "transform": _transform_values(component),
        "transform_applied": applied_transform is not None,
        "api": report.signature,
        "component_count": len(_component_names(assembly)),
    }


# ---------------------------------------------------------------------------
# add_mate
# ---------------------------------------------------------------------------

def add_mate(
    session: SolidWorksSession,
    mate_type: str,
    entity_a: dict,
    entity_b: dict,
    *,
    alignment: str = "closest",
    flip: bool = False,
    value: float | None = None,
    unit: str | None = None,
    converter: units.UnitConverter | None = None,
) -> dict:
    """Mate two component entities in the active assembly.

    Args:
        mate_type: ``coincident``, ``concentric``, ``perpendicular``, ``parallel``,
            ``tangent``, ``distance``, ``angle``, ``lock``, ``symmetric``, ``width``.
        entity_a: Side A as ``{"component": "Shaft-1", "kind": "face", "index": 3}``.
            ``kind`` is ``"face"`` (default) or ``"edge"``; ``index`` comes from
            ``analyze_assembly(analysis_type="faces", component="Shaft-1")``.
        entity_b: Side B, same shape.
        alignment: ``aligned``, ``anti_aligned``, or ``closest`` (default).
        flip: Flip the mate.
        value: Distance in ``unit`` for a distance mate, or degrees for an angle mate.
        unit: Unit for a distance ``value``.

    Returns:
        The mate name plus **both** components' resulting transforms -- the per-mate
        check that tells you the mate positioned things the way you intended,
        rather than solving to a different valid configuration.
    """
    assembly = _assembly(session)
    converter = converter or units.UnitConverter(session.config.default_unit)

    kind = one_of(mate_type, "mate_type", set(enums.MATE_TYPE_BY_NAME))
    mate_code = enums.MATE_TYPE_BY_NAME[kind]
    align_code = enums.ALIGN_BY_NAME[one_of(alignment, "alignment", set(enums.ALIGN_BY_NAME))]

    entity_objects = []
    described = []
    for label, spec in (("entity_a", entity_a), ("entity_b", entity_b)):
        if not isinstance(spec, dict):
            raise ToolError(
                "INVALID_PARAMETER",
                f"{label} must be an object like "
                f'{{"component": "Shaft-1", "kind": "face", "index": 3}}.',
            )
        component_name = require(spec.get("component"), f"{label}.component")
        entity_kind = one_of(spec.get("kind", "face"), f"{label}.kind", {"face", "edge"})
        index = spec.get("index")
        if index is None:
            raise ToolError(
                "MISSING_PARAMETER",
                f"{label}.index is required.",
                hint=f"Get it from analyze_assembly(analysis_type='{entity_kind}s', "
                     f"component='{component_name}').",
            )

        entity, error = _component_entity(assembly, component_name, entity_kind, int(index))
        if entity is None:
            raise ToolError(
                "ENTITY_NOT_FOUND", f"{label}: {error}",
                details={"components": _component_names(assembly)},
            )
        entity_objects.append(entity)
        described.append(
            {"component": component_name, "kind": entity_kind, "index": int(index)}
        )

    selection.clear(assembly)
    # Mate entities carry selection mark 1.
    for entity in entity_objects:
        if not selection.select_entity(entity, append=True, mark=1, doc=assembly):
            selection.clear(assembly)
            raise SolidWorksError(
                "SELECTION_FAILED",
                "One of the mate entities could not be selected even though it was resolved.",
            )

    distance = upper_distance = lower_distance = 0.0
    angle = upper_angle = lower_angle = 0.0
    if kind == "distance":
        if value is None:
            raise ToolError(
                "MISSING_PARAMETER", "A distance mate needs value.", hint="e.g. value=10"
            )
        distance = upper_distance = lower_distance = converter.to_meters(value, unit)
    elif kind == "angle":
        if value is None:
            raise ToolError(
                "MISSING_PARAMETER", "An angle mate needs value in degrees.", hint="e.g. value=45"
            )
        angle = upper_angle = lower_angle = units.deg_to_rad(value)

    error_status = byref_int()
    report = compat.invoke_first(assembly, [
        ("AddMate5", (
            mate_code, align_code, flip,
            distance, upper_distance, lower_distance, 0.0, 0.0,
            angle, upper_angle, lower_angle,
            False, False, 0, error_status,
        )),
        ("AddMate4", (
            mate_code, align_code, flip,
            distance, upper_distance, lower_distance, 0.0, 0.0,
            angle, upper_angle, lower_angle, False, error_status,
        )),
        ("AddMate3", (
            mate_code, align_code, flip,
            distance, upper_distance, lower_distance, 0.0, 0.0,
            angle, upper_angle, lower_angle, error_status,
        )),
    ])
    selection.clear(assembly)

    if not report.ok:
        raise SolidWorksError(
            "MATE_FAILED",
            f"The {kind} mate returned null (SolidWorks error status {error_status.value}). "
            f"The entity kinds must suit the mate type -- a concentric mate wants cylindrical "
            f"faces, for instance -- and the alignment must be achievable.",
            {
                "error_status": error_status.value,
                "entities": described,
                "attempts": report.diagnostic(),
            },
        )

    compat.invoke_any(assembly, [("EditRebuild3", ())])

    return {
        "mate": _last_mate_name(assembly),
        "mate_type": kind,
        "alignment": alignment,
        "entities": described,
        "value": (
            converter.describe(distance, unit) if kind == "distance"
            else {"value": value, "unit": "deg"} if kind == "angle" else None
        ),
        "error_status": error_status.value,
        "transforms": {
            described[0]["component"]: _transform_values(
                _find_component(assembly, described[0]["component"])),
            described[1]["component"]: _transform_values(
                _find_component(assembly, described[1]["component"])),
        },
        "api": report.signature,
    }


def _component_entity(assembly, component_name: str, kind: str, index: int):
    """Resolve (component, face-or-edge, index) to an entity object.

    The enumeration matches :func:`analyze_assembly` exactly, so an index handed
    out by analysis always resolves to the same entity here.
    """
    component = _find_component(assembly, component_name)
    if component is None:
        return None, (
            f"no component named '{component_name}' in this assembly "
            f"(names are instance-numbered, e.g. 'Shaft-1')"
        )

    bodies = compat.as_list(pm(component, "GetBodies2", enums.BODY_SOLID, default=None))
    if not bodies:
        bodies = compat.as_list(pm(component, "GetBody", default=None))

    entities = []
    accessor = "GetFaces" if kind == "face" else "GetEdges"
    for body in bodies:
        entities.extend(compat.as_list(pm(body, accessor, default=None)))

    if not entities:
        return None, f"component '{component_name}' exposes no {kind}s (is it suppressed?)"
    if index < 0 or index >= len(entities):
        return None, (
            f"{kind} index {index} is out of range: '{component_name}' has {len(entities)} "
            f"{kind}s (0-{len(entities) - 1})"
        )
    return entities[index], ""


def _last_mate_name(assembly) -> str | None:
    """The most recently added mate's name.

    ``AddMate5`` returns a mate object with no name accessor, so the name is read
    from the MateGroup's last sub-feature.
    """
    last = None
    for feature in compat.iter_features(assembly):
        if compat.feature_type(feature) != "MateGroup":
            continue
        for sub in compat.iter_sub_features(feature):
            last = compat.feature_name(sub)
    return last


# ---------------------------------------------------------------------------
# analyze_assembly
# ---------------------------------------------------------------------------

def analyze_assembly(
    session: SolidWorksSession,
    analysis_type: str = "components",
    *,
    component: str | None = None,
    converter: units.UnitConverter | None = None,
) -> dict:
    """Read the active assembly without modifying it.

    Args:
        analysis_type:
            * ``components`` -- top-level component occurrences with source path,
              configuration, fixed and suppressed state, child count, and the full
              13-number transform.
            * ``components_flat`` -- leaf occurrences across all nesting levels.
            * ``mates`` -- the MateGroup's mates in creation order, with type and
              alignment read from the enums (never the localised display names) and
              the SI value for distance and angle mates.
            * ``faces`` / ``edges`` -- that component's entity list with the stable
              indices ``add_mate`` consumes. Requires ``component``. Coordinates are
              **component-local**; the component transform is included for mapping.
        component: Component instance name, e.g. ``"Shaft-1"``. Required for
            ``faces`` and ``edges``.
    """
    assembly = _assembly(session)
    converter = converter or units.UnitConverter(session.config.default_unit)
    kind = one_of(analysis_type, "analysis_type", ANALYSIS_TYPES)
    precision = session.config.precision

    if kind in {"components", "components_flat"}:
        top_only = kind == "components"
        entries = []
        for component_object in _iter_components(assembly, top_only):
            children = compat.as_list(pm(component_object, "GetChildren", default=None))
            entries.append({
                "name": pm(component_object, "Name2", default=None),
                "path": pm(component_object, "GetPathName", default=None),
                "configuration": pm(component_object, "ReferencedConfiguration", default=None),
                "fixed": bool(pm(component_object, "IsFixed", default=False)),
                "suppressed": bool(pm(component_object, "IsSuppressed", default=False)),
                "children": len(children),
                "transform": _transform_values(component_object, precision + 3),
            })
        return {
            "analysis_type": kind,
            "component_count": len(entries),
            "components": entries,
            "note": "transform = 3x3 rotation row-major, then translation xyz in metres, "
                    "then scale (13 numbers).",
        }

    if kind == "mates":
        mates = []
        for feature in compat.iter_features(assembly):
            if compat.feature_type(feature) != "MateGroup":
                continue
            for sub in compat.iter_sub_features(feature):
                mate = pm(sub, "GetSpecificFeature2", default=None)
                if mate is None:
                    continue
                type_code = pm(mate, "Type", default=None)
                record = {
                    "name": compat.feature_name(sub),
                    "type": enums.MATE_NAME_BY_TYPE.get(
                        int(type_code) if type_code is not None else -1, f"code_{type_code}"),
                    "type_code": type_code,
                    "alignment": pm(mate, "Alignment", default=None),
                    "suppressed": bool(pm(sub, "IsSuppressed", default=False)),
                }
                dimension = pm(mate, "DisplayDimension2", 0, default=None)
                if dimension is not None:
                    inner = pm(dimension, "GetDimension2", 0, default=None)
                    si_value = pm(inner, "SystemValue", default=None) if inner else None
                    if si_value is not None:
                        try:
                            record["value_si"] = round(float(si_value), precision)
                        except (TypeError, ValueError):
                            pass
                entities = []
                for slot in range(2):
                    entity = pm(mate, "MateEntity", slot, default=None)
                    if entity is None:
                        continue
                    owner = pm(entity, "ReferenceComponent", default=None)
                    entities.append({
                        "component": pm(owner, "Name2", default=None) if owner else None,
                        "reference_type": pm(entity, "ReferenceType2", default=None),
                    })
                record["entities"] = entities
                mates.append(record)

        return {"analysis_type": kind, "mate_count": len(mates), "mates": mates}

    # faces / edges
    require(
        component, "component",
        hint="The component instance name, e.g. component='Shaft-1'. "
             "analyze_assembly(analysis_type='components') lists them.",
    )
    component_object = _find_component(assembly, component)
    if component_object is None:
        raise ToolError(
            "COMPONENT_NOT_FOUND",
            f"No component named '{component}'.",
            hint="Names are instance-numbered, e.g. 'Shaft-1'.",
            details={"components": _component_names(assembly)},
        )

    bodies = compat.as_list(pm(component_object, "GetBodies2", enums.BODY_SOLID, default=None))
    if not bodies:
        bodies = compat.as_list(pm(component_object, "GetBody", default=None))

    accessor = "GetFaces" if kind == "faces" else "GetEdges"
    reader = selection.face_geometry if kind == "faces" else selection.edge_geometry

    entries = []
    limit = session.config.max_entities
    index = 0
    truncated = False
    for body in bodies:
        for entity in compat.as_list(pm(body, accessor, default=None)):
            if index >= limit:
                truncated = True
                break
            record = {"i": index}
            record.update(reader(entity, precision))
            entries.append(record)
            index += 1
        if truncated:
            break

    return {
        "analysis_type": kind,
        "component": component,
        f"{kind[:-1]}_count": len(entries),
        kind: entries,
        "transform": _transform_values(component_object, precision + 3),
        "truncated": truncated,
        "note": "Coordinates are COMPONENT-LOCAL. Apply the transform to reach assembly space. "
                "Feed 'i' to add_mate as the entity index.",
    }


# ---------------------------------------------------------------------------
# save_body_as_part
# ---------------------------------------------------------------------------

def save_body_as_part(
    session: SolidWorksSession,
    body_index: int,
    file_path: str,
) -> dict:
    """Extract one solid body of a multibody part into its own part file.

    The missing half of flattened-assembly reconstruction: a structured assembly
    STEP with no per-part files imports as a single multibody part, and each
    distinct body has to become a referenced part file before an assembly can be
    rebuilt from it. Geometry keeps the source coordinates, so placements survive.

    Args:
        body_index: Body index from ``analyze_model(analysis_type="bodies")``.
        file_path: Target ``.sldprt`` path. Parent directories are created.
    """
    source = session.require_doc("PART")
    source_title = pm(source, "GetTitle", default=None)

    bodies = selection.solid_bodies(source)
    if body_index < 0 or body_index >= len(bodies):
        raise ToolError(
            "BODY_NOT_FOUND",
            f"body_index {body_index} is out of range: the part has {len(bodies)} solid bodies.",
            hint="Call analyze_model(analysis_type='bodies') for valid indices.",
        )
    body = bodies[body_index]
    body_name = pm(body, "Name", default=None)

    copy = pm(body, "Copy", default=None)
    if copy is None:
        raise SolidWorksError("BODY_COPY_FAILED", "IBody2.Copy returned null.")

    template = session.template_for("part")
    new_doc = session.app.NewDocument(template, 0, 0, 0)
    if new_doc is None:
        raise SolidWorksError(
            "DOCUMENT_CREATION_FAILED", f"Could not create a part from template '{template}'."
        )

    report = compat.invoke_first(new_doc, [
        ("CreateFeatureFromBody3", (copy, False, 0)),
        ("CreateFeatureFromBody2", (copy, False, 0)),
        ("CreateFeatureFromBody", (copy, False)),
    ])
    if not report.ok:
        raise SolidWorksError(
            "BODY_FEATURE_FAILED",
            "CreateFeatureFromBody returned null for the copied body.",
            {"attempts": report.diagnostic()},
        )

    directory = os.path.dirname(os.path.abspath(file_path))
    if directory:
        os.makedirs(directory, exist_ok=True)

    errors, warnings = byref_int(), byref_int()
    extension = pm(new_doc, "Extension", default=None)
    saved = compat.invoke_bool(extension, [
        ("SaveAs3", (file_path, enums.SAVE_AS_CURRENT_VERSION, enums.SAVE_SILENT,
                     nothing(), nothing(), errors, warnings)),
    ]).ok if extension is not None else False

    new_title = pm(new_doc, "GetTitle", default=None)
    if new_title:
        try:
            session.app.CloseDoc(new_title)
        except Exception:
            pass
    if source_title:
        pm(session.app, "ActivateDoc3", source_title, False,
           enums.ACTIVATE_DONT_REBUILD, byref_int(), default=None)

    if not saved:
        raise SolidWorksError(
            "SAVE_FAILED",
            f"Saving the extracted body to '{file_path}' failed "
            f"(errors={errors.value}, warnings={warnings.value}).",
            {"errors": errors.value},
        )

    return {
        "path": file_path,
        "body_index": body_index,
        "body_name": body_name,
        "source": source_title,
        "note": "Geometry is in the SOURCE part's coordinates, so original placements are preserved.",
    }
