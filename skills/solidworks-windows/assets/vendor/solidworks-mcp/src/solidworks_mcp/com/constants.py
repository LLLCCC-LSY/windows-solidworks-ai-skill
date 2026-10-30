"""
Authoritative SolidWorks enum values, read from the installation's own type library.

``enums.py`` carries hand-maintained integer fallbacks, which is what both reference
implementations rely on -- one by inlining ints, the other by compiling against a
pinned ``SolidWorks.Interop.swconst`` assembly. Both approaches have the same flaw:
the value is fixed at authoring time, and a constant that shifts between releases
(or that was transcribed wrongly) becomes a silent behaviour change rather than an
error.

This module removes the guesswork. ``swconst.tlb`` ships next to ``SLDWORKS.exe`` in
every install, so it can be loaded **by path** -- no GUID, no registry walk, no
generated code cache, no version pinning -- and its enumerations read directly. The
result is the value *this* SolidWorks actually uses.

Resolution is best-effort and cached: if the type library cannot be read, callers
fall back to the ``enums.py`` value and nothing breaks. It matters most for the
constants where a wrong value would be actively harmful rather than merely
ineffective -- the user-preference toggles, which mutate the user's SolidWorks
settings.
"""

from __future__ import annotations

import logging
import os
import threading

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_table: dict[str, int] | None = None
_source: str = "not attempted"


def _load_table() -> dict[str, int]:
    """Build a ``{enum member name: value}`` map from ``swconst.tlb``.

    Returns an empty map on any failure -- a missing or unreadable type library is
    expected on some installs and must not be fatal.
    """
    global _source

    try:
        import pythoncom
    except ImportError:
        _source = "pywin32 unavailable"
        return {}

    from .finder import find_solidworks_exe

    executable = os.environ.get("SOLIDWORKS_EXE") or find_solidworks_exe()
    if not executable:
        _source = "no SolidWorks installation located"
        return {}

    install_root = os.path.dirname(executable)
    candidates = [
        os.path.join(install_root, "swconst.tlb"),
        os.path.join(install_root, "swconst.dll"),
        # Some installs keep the type libraries one level up alongside the API SDK.
        os.path.join(os.path.dirname(install_root), "swconst.tlb"),
    ]

    library = None
    for path in candidates:
        if not os.path.isfile(path):
            continue
        try:
            library = pythoncom.LoadTypeLib(path)
            _source = path
            break
        except Exception as exc:
            logger.debug("Could not load type library %s: %s", path, exc)

    if library is None:
        _source = "swconst type library not found next to SLDWORKS.exe"
        return {}

    table: dict[str, int] = {}
    try:
        for index in range(library.GetTypeInfoCount()):
            try:
                if library.GetTypeInfoType(index) != pythoncom.TKIND_ENUM:
                    continue
                type_info = library.GetTypeInfo(index)
                attributes = type_info.GetTypeAttr()
            except Exception:
                continue

            # The enum member count is TYPEATTR.cVars -- index 7, not 8. Index 8 is
            # cImplTypes, which is always 0 for an enum, so reading it there silently
            # yielded zero constants for every enum in the library. Prefer the named
            # attribute and fall back to the verified index.
            try:
                variable_count = attributes.cVars
            except AttributeError:
                try:
                    variable_count = attributes[7]
                except Exception:
                    continue

            for variable_index in range(variable_count):
                try:
                    description = type_info.GetVarDesc(variable_index)
                    name = type_info.GetNames(description[0])[0]
                    value = description[1]
                except Exception:
                    continue
                if isinstance(value, int):
                    table[name] = value
    except Exception as exc:
        logger.debug("Enumerating swconst failed: %s", exc)

    logger.info("Loaded %d SolidWorks constants from %s", len(table), _source)
    return table


def table() -> dict[str, int]:
    """The cached constant map, loading it on first use."""
    global _table
    if _table is None:
        with _lock:
            if _table is None:
                _table = _load_table()
    return _table


def resolve(name: str, fallback: int) -> int:
    """The live value of a SolidWorks enum member, or ``fallback``.

    Args:
        name: Exact enum member name, e.g. ``"swInputDimValOnCreate"``.
        fallback: The value from ``enums.py``, used when the type library is
            unavailable.

    Returns:
        The value this SolidWorks install reports, or ``fallback``.
    """
    value = table().get(name)
    if value is None:
        return fallback
    if value != fallback:
        # Worth logging loudly: it means the hand-maintained fallback in enums.py
        # disagrees with this release and should be revisited.
        logger.warning(
            "Constant %s is %d on this SolidWorks build, not the built-in fallback %d "
            "-- using the build's value.",
            name, value, fallback,
        )
    return value


def verified(name: str, fallback: int) -> tuple[int, bool]:
    """Like :func:`resolve`, but also reports whether the value was confirmed.

    Use this for operations that **mutate user settings**: acting on an unverified
    guess risks changing an unrelated preference, so a caller can choose to skip
    the operation instead.

    Returns:
        ``(value, confirmed)`` where ``confirmed`` is true only when the value came
        from the type library rather than the fallback.
    """
    value = table().get(name)
    if value is None:
        return fallback, False
    return value, True


# ---------------------------------------------------------------------------
# Binding the enums module to the type library
# ---------------------------------------------------------------------------

#: Maps every version-sensitive ``enums`` attribute to its ``swconst`` member name.
#:
#: Hand-transcribed constants are the single biggest correctness risk in a
#: late-bound CAD integration: a wrong value does not raise, it quietly does the
#: wrong thing. Checking this table against a real SolidWorks 2023 type library found
#: **nine** wrong values, including ``swFmBaseFlange`` (1287 vs 34, which would have
#: broken every sheet-metal feature) and ``swInsertDimensionsMarkedForDrawing``
#: (1 vs 32768, which would have made drawing auto-dimensioning silently place
#: nothing). So rather than trusting the table, :func:`sync_enums` overwrites it with
#: the values this install actually reports.
_ENUM_BINDINGS: dict[str, str] = {
    # Document types and open/save options
    "DOC_PART": "swDocPART",
    "DOC_ASSEMBLY": "swDocASSEMBLY",
    "DOC_DRAWING": "swDocDRAWING",
    "OPEN_SILENT": "swOpenDocOptions_Silent",
    "OPEN_READONLY": "swOpenDocOptions_ReadOnly",
    "SAVE_AS_CURRENT_VERSION": "swSaveAsCurrentVersion",
    "SAVE_SILENT": "swSaveAsOptions_Silent",
    "SAVE_COPY": "swSaveAsOptions_Copy",
    "SAVE_AVOID_REBUILD": "swSaveAsOptions_AvoidRebuildOnSave",
    "ACTIVATE_DONT_REBUILD": "swDontRebuildActiveDoc",
    # End and start conditions
    "END_BLIND": "swEndCondBlind",
    "END_THROUGH_ALL": "swEndCondThroughAll",
    "END_THROUGH_ALL_BOTH": "swEndCondThroughAllBoth",
    "END_UP_TO_VERTEX": "swEndCondUpToVertex",
    "END_UP_TO_SURFACE": "swEndCondUpToSurface",
    "END_OFFSET_FROM_SURFACE": "swEndCondOffsetFromSurface",
    "END_MID_PLANE": "swEndCondMidPlane",
    "END_UP_TO_BODY": "swEndCondUpToBody",
    "END_UP_TO_NEXT": "swEndCondUpToNext",
    "START_SKETCH_PLANE": "swStartSketchPlane",
    "START_SURFACE": "swStartSurface",
    "START_VERTEX": "swStartVertex",
    "START_OFFSET": "swStartOffset",
    # Bodies
    "BODY_SOLID": "swSolidBody",
    "BODY_SHEET": "swSheetBody",
    "BODY_WIRE": "swWireBody",
    # Fillet and chamfer
    "FILLET_PROPAGATE": "swFeatureFilletPropagate",
    "FILLET_UNIFORM_RADIUS": "swFeatureFilletUniformRadius",
    "FILLET_TYPE_SIMPLE": "swFeatureFilletType_Simple",
    "CHAMFER_ANGLE_DISTANCE": "swChamferAngleDistance",
    "CHAMFER_DISTANCE_DISTANCE": "swChamferDistanceDistance",
    "CHAMFER_EQUAL_DISTANCE": "swChamferEqualDistance",
    "CHAMFER_FLIP_DIRECTION": "swFeatureChamferFlipDirection",
    # Reference geometry
    "REFPLANE_DISTANCE": "swRefPlaneReferenceConstraint_Distance",
    "REFPLANE_OPTION_FLIP": "swRefPlaneReferenceConstraint_OptionFlip",
    "REFPOINT_INTERSECTION": "swRefPointIntersection",
    # Suppression, configurations and the rollback bar
    "SUPPRESS_FEATURE": "swSuppressFeature",
    "UNSUPPRESS_FEATURE": "swUnSuppressFeature",
    "THIS_CONFIGURATION": "swThisConfiguration",
    "ALL_CONFIGURATION": "swAllConfiguration",
    "ROLLBACK_TO_END": "swMoveRollbackBarToEnd",
    "ROLLBACK_TO_PREVIOUS": "swMoveRollbackBarToPreviousPosition",
    "ROLLBACK_TO_BEFORE_FEATURE": "swMoveRollbackBarToBeforeFeature",
    "ROLLBACK_TO_AFTER_FEATURE": "swMoveRollbackBarToAfterFeature",
    # Feature-definition ids used by CreateDefinition
    "FEATURE_ID_BASE_FLANGE": "swFmBaseFlange",
    "FEATURE_ID_EDGE_FLANGE": "swFmEdgeFlange",
    "FEATURE_ID_SHELL": "swFmShell",
    # Sheet metal
    "FLANGE_POS_MATERIAL_INSIDE": "swFlangePositionTypeMaterialInside",
    "FLANGE_POS_MATERIAL_OUTSIDE": "swFlangePositionTypeMaterialOutside",
    "FLANGE_POS_BEND_OUTSIDE": "swFlangePositionTypeBendOutside",
    "FLANGE_POS_BEND_CENTERLINE": "swFlangePositionTypeBendCenterLine",
    "FLANGE_POS_BEND_SHARP": "swFlangePositionTypeBendSharp",
    "FLANGE_POS_BEND_TANGENT": "swFlangePositionTypeBendTangent",
    "RELIEF_RECTANGULAR": "swSheetMetalReliefRectangular",
    "RELIEF_TEAR": "swSheetMetalReliefTear",
    "RELIEF_OBROUND": "swSheetMetalReliefObround",
    "EDGE_FLANGE_USE_DEFAULT_RADIUS": "swInsertEdgeFlangeUseDefaultRadius",
    "EDGE_FLANGE_USE_RELIEF_RATIO": "swInsertEdgeFlangeUseReliefRatio",
    "EDGE_FLANGE_USE_DEFAULT_RELIEF": "swInsertEdgeFlangeUseDefaultRelief",
    # Mates
    "MATE_COINCIDENT": "swMateCOINCIDENT",
    "MATE_CONCENTRIC": "swMateCONCENTRIC",
    "MATE_PERPENDICULAR": "swMatePERPENDICULAR",
    "MATE_PARALLEL": "swMatePARALLEL",
    "MATE_TANGENT": "swMateTANGENT",
    "MATE_DISTANCE": "swMateDISTANCE",
    "MATE_ANGLE": "swMateANGLE",
    "MATE_SYMMETRIC": "swMateSYMMETRIC",
    "MATE_WIDTH": "swMateWIDTH",
    "MATE_LOCK": "swMateLOCK",
    "ALIGN_ALIGNED": "swMateAlignALIGNED",
    "ALIGN_ANTI_ALIGNED": "swMateAlignANTI_ALIGNED",
    "ALIGN_CLOSEST": "swMateAlignCLOSEST",
    "ADD_COMPONENT_CURRENT_SELECTED_CONFIG":
        "swAddComponentConfigOptions_CurrentSelectedConfig",
    # Drawings
    "INSERT_DIMS_MARKED_FOR_DRAWING": "swInsertDimensionsMarkedForDrawing",
    "INSERT_DIMS_NOT_MARKED_FOR_DRAWING": "swInsertDimensionsNotMarkedForDrawing",
    "SECTION_VIEW_NOT_ALIGNED": "swCreateSectionView_NotAligned",
    "SECTION_VIEW_OFFSET_SECTION": "swCreateSectionView_OffsetSection",
    "SECTION_VIEW_CHANGE_DIRECTION": "swCreateSectionView_ChangeDirection",
    "SECTION_VIEW_SCALE_WITH_MODEL": "swCreateSectionView_ScaleWithModel",
    "SECTION_VIEW_PARTIAL": "swCreateSectionView_Partial",
    "SECTION_VIEW_DISPLAY_SURFACE_CUT": "swCreateSectionView_DisplaySurfaceCut",
    "SECTION_VIEW_EXCLUDE_FASTENERS": "swCreateSectionView_ExcludeFasteners",
    "SECTION_VIEW_CUT_SURFACE_BODIES": "swCreateSectionView_CutSurfaceBodies",
    "CENTER_MARK_TYPE_HOLE": "swAutoInsertCenterMarkType_Hole",
    "CENTER_MARK_TYPE_FILLETS": "swAutoInsertCenterMarkType_Fillets",
    "CENTER_MARK_TYPE_SLOTS": "swAutoInsertCenterMarkType_Slots",
    # Preferences
    "PREF_INPUT_DIM_VAL_ON_CREATE": "swInputDimValOnCreate",
    "PREF_MULTICAD_ENABLE_3D_INTERCONNECT": "swMultiCAD_Enable3DInterconnect",
    "PREF_MULTICAD_APPLY_ONLY_TO_PARTS": "swMultiCAD_ApplyOnlyToParts",
    "PREF_DEFAULT_TEMPLATE_PART": "swDefaultTemplatePart",
    "PREF_DEFAULT_TEMPLATE_ASSEMBLY": "swDefaultTemplateAssembly",
    "PREF_DEFAULT_TEMPLATE_DRAWING": "swDefaultTemplateDrawing",
    # Export
    "EXPORT_PDF_DATA": "swExportPdfData",
}

_sync_report: dict | None = None


def sync_enums() -> dict:
    """Overwrite :mod:`enums` scalars with the values this install reports.

    Patching the module attributes in place means every existing ``enums.X``
    reference picks up the verified value with no call-site changes, while the
    hand-maintained literals stay as the fallback for installs whose type library
    cannot be read.

    Idempotent and cached. Returns a report naming every constant that was
    corrected, which ``get_solidworks_info`` surfaces.
    """
    global _sync_report
    if _sync_report is not None:
        return _sync_report

    from . import enums

    loaded = table()
    if not loaded:
        _sync_report = {
            "synced": False,
            "reason": "the swconst type library could not be read; built-in fallbacks are in use",
            "corrected": {},
            "unknown_members": [],
        }
        return _sync_report

    corrected: dict[str, dict] = {}
    unknown: list[str] = []

    for attribute, member in _ENUM_BINDINGS.items():
        actual = loaded.get(member)
        if actual is None:
            unknown.append(member)
            continue
        fallback = getattr(enums, attribute, None)
        if fallback != actual:
            corrected[attribute] = {"was": fallback, "now": actual, "member": member}
        setattr(enums, attribute, actual)

    # The name -> value lookup tables are built from the scalars at import time, so
    # they still hold pre-sync values and must be rebuilt.
    enums.rebuild_derived_tables()

    if corrected:
        logger.warning(
            "Corrected %d built-in constant(s) from the install's type library: %s",
            len(corrected),
            ", ".join(f"{k} {v['was']}->{v['now']}" for k, v in corrected.items()),
        )
    if unknown:
        logger.info("Type library does not expose: %s", ", ".join(unknown))

    _sync_report = {
        "synced": True,
        "bindings": len(_ENUM_BINDINGS),
        "corrected_count": len(corrected),
        "corrected": corrected,
        "unknown_members": unknown,
    }
    return _sync_report


def diagnostics() -> dict:
    """Where the constants came from, how many were read, and what they corrected.

    Surfaced by ``get_solidworks_info`` so a user can tell whether enum values are
    verified against their install or resting on the built-in fallbacks.
    """
    loaded = table()
    return {
        "source": _source,
        "constants_loaded": len(loaded),
        "verified": bool(loaded),
        "enum_sync": sync_enums(),
    }
