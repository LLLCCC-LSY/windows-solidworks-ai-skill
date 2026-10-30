"""
SolidWorks API enumeration constants.

Inlined as plain ints so nothing here depends on the version-locked
``SolidWorks.Interop.swconst`` assembly. Every value below is stable across
SolidWorks 2018-2026; the few that are version-sensitive are noted.
"""

# --- swDocumentTypes_e -------------------------------------------------------
DOC_NONE = 0
DOC_PART = 1
DOC_ASSEMBLY = 2
DOC_DRAWING = 3

DOC_TYPE_BY_EXT = {
    ".sldprt": DOC_PART,
    ".prtdot": DOC_PART,
    ".sldasm": DOC_ASSEMBLY,
    ".asmdot": DOC_ASSEMBLY,
    ".slddrw": DOC_DRAWING,
    ".drwdot": DOC_DRAWING,
}

# --- swOpenDocOptions_e ------------------------------------------------------
OPEN_SILENT = 1
OPEN_READONLY = 2
OPEN_VIEWONLY = 8
OPEN_RAPIDDRAFT = 16
OPEN_LOADMODEL = 32
OPEN_AUTOMISSING = 32768

# --- swSaveAsVersion_e / swSaveAsOptions_e ----------------------------------
SAVE_AS_CURRENT_VERSION = 0
SAVE_SILENT = 1
SAVE_COPY = 2
SAVE_UPDATE_INACTIVE_VIEWS = 4
SAVE_AVOID_REBUILD = 8

# --- swCloseSaveOptions ------------------------------------------------------
# swDontRebuildActiveDoc for ActivateDoc3
ACTIVATE_DONT_REBUILD = 1

# --- swEndConditions_e -------------------------------------------------------
END_BLIND = 0
END_THROUGH_ALL = 1
END_THROUGH_ALL_BOTH = 2
END_UP_TO_VERTEX = 3
END_UP_TO_SURFACE = 4
END_OFFSET_FROM_SURFACE = 5
END_MID_PLANE = 6
END_UP_TO_BODY = 7
END_UP_TO_NEXT = 8

# --- swStartConditions_e -----------------------------------------------------
START_SKETCH_PLANE = 0
START_SURFACE = 1
START_VERTEX = 2
START_OFFSET = 3

# --- swBodyType_e ------------------------------------------------------------
BODY_SOLID = 0
BODY_SHEET = 1
BODY_WIRE = 2
BODY_GENERAL = 3

# --- swSelectType_e (subset used for typed selection strings) ----------------
SEL_STRING_FACE = "FACE"
SEL_STRING_EDGE = "EDGE"
SEL_STRING_VERTEX = "VERTEX"
SEL_STRING_PLANE = "PLANE"
SEL_STRING_SKETCH = "SKETCH"
SEL_STRING_SKETCHSEGMENT = "SKETCHSEGMENT"
SEL_STRING_SKETCHPOINT = "SKETCHPOINT"
SEL_STRING_BODYFEATURE = "BODYFEATURE"
SEL_STRING_AXIS = "AXIS"
SEL_STRING_REFERENCE_CURVE = "REFERENCECURVES"
SEL_STRING_COMPONENT = "COMPONENT"
SEL_STRING_SOLIDBODY = "SOLIDBODY"

# --- swFeatureFilletOptions_e / swFeatureFilletType_e -----------------------
FILLET_PROPAGATE = 1
FILLET_UNIFORM_RADIUS = 2
FILLET_ATTACHED_TO_ALL = 4
FILLET_TYPE_SIMPLE = 0
FILLET_TYPE_VARIABLE = 1
FILLET_TYPE_FACE = 2
FILLET_TYPE_FULLROUND = 3

# --- swChamferType_e / swFeatureChamferOption_e -----------------------------
CHAMFER_ANGLE_DISTANCE = 1
CHAMFER_DISTANCE_DISTANCE = 2
CHAMFER_VERTEX = 3
CHAMFER_EQUAL_DISTANCE = 4
CHAMFER_FLIP_DIRECTION = 1

# --- swRefPlaneReferenceConstraints_e --------------------------------------
REFPLANE_DISTANCE = 8
REFPLANE_OPTION_FLIP = 256   # verified against a 2023 type library (not 65536)
REFPLANE_ANGLE = 4
REFPLANE_PARALLEL = 16
REFPLANE_PERPENDICULAR = 32
REFPLANE_COINCIDENT = 64
REFPLANE_MIDPLANE = 4096

# --- swRefPointType_e --------------------------------------------------------
REFPOINT_ARCCENTER = 1
REFPOINT_CENTROID = 2
REFPOINT_INTERSECTION = 4
REFPOINT_PROJECTION = 8
REFPOINT_ONSURFACE = 16

# --- swFeatureSuppressionAction_e / swInConfigurationOpts_e -----------------
SUPPRESS_FEATURE = 0
UNSUPPRESS_FEATURE = 1
UNSUPPRESS_DEPENDENT = 2
THIS_CONFIGURATION = 1
ALL_CONFIGURATION = 2
SPECIFY_CONFIGURATION = 3

# --- swMoveRollbackBarTo_e ---------------------------------------------------
# Verified against a SolidWorks 2023 type library: this enum is 1-based, not
# 0-based. Reading it as 0-based rolled the bar to the wrong position.
ROLLBACK_TO_END = 1
ROLLBACK_TO_PREVIOUS = 2
ROLLBACK_TO_BEFORE_FEATURE = 3
ROLLBACK_TO_AFTER_FEATURE = 4

# --- swFeatureNameID_e (CreateDefinition ids) --------------------------------
FEATURE_ID_BASE_FLANGE = 34   # swFmBaseFlange
FEATURE_ID_EDGE_FLANGE = 37   # swFmEdgeFlange
FEATURE_ID_SHELL = 8          # swFmShell

# --- swFlangePositionTypes_e -------------------------------------------------
FLANGE_POS_MATERIAL_INSIDE = 1
FLANGE_POS_MATERIAL_OUTSIDE = 2
FLANGE_POS_BEND_OUTSIDE = 3
FLANGE_POS_BEND_CENTERLINE = 4
FLANGE_POS_BEND_SHARP = 5
FLANGE_POS_BEND_TANGENT = 6

FLANGE_POS_BY_NAME = {
    "material_inside": FLANGE_POS_MATERIAL_INSIDE,
    "material_outside": FLANGE_POS_MATERIAL_OUTSIDE,
    "bend_outside": FLANGE_POS_BEND_OUTSIDE,
    "centerline": FLANGE_POS_BEND_CENTERLINE,
    "bend_sharp": FLANGE_POS_BEND_SHARP,
    "bend_tangent": FLANGE_POS_BEND_TANGENT,
}

# InsertSheetMetal3dBend uses a DIFFERENT position encoding than
# ISketchedBendFeatureData.PositionType (which uses swFlangePositionTypes_e).
BEND_3D_POS_BY_NAME = {
    "centerline": 0,
    "material_inside": 1,
    "material_outside": 2,
    "bend_outside": 3,
}

# --- swSheetMetalReliefTypes_e / swInsertEdgeFlangeOptions_e ----------------
RELIEF_RECTANGULAR = 1
RELIEF_TEAR = 2
RELIEF_OBROUND = 3
EDGE_FLANGE_USE_DEFAULT_RADIUS = 1
EDGE_FLANGE_USE_RELIEF_RATIO = 2
EDGE_FLANGE_USE_DEFAULT_RELIEF = 4

# --- swMateType_e ------------------------------------------------------------
MATE_COINCIDENT = 0
MATE_CONCENTRIC = 1
MATE_PERPENDICULAR = 2
MATE_PARALLEL = 3
MATE_TANGENT = 4
MATE_DISTANCE = 5
MATE_ANGLE = 6
MATE_SYMMETRIC = 9
MATE_CAMFOLLOWER = 11
MATE_GEAR = 12
MATE_WIDTH = 14
MATE_LOCK = 16   # verified: 15 is swMateMAXMATES on a 2023 type library, not LOCK

MATE_TYPE_BY_NAME = {
    "coincident": MATE_COINCIDENT,
    "concentric": MATE_CONCENTRIC,
    "perpendicular": MATE_PERPENDICULAR,
    "parallel": MATE_PARALLEL,
    "tangent": MATE_TANGENT,
    "distance": MATE_DISTANCE,
    "angle": MATE_ANGLE,
    "symmetric": MATE_SYMMETRIC,
    "lock": MATE_LOCK,
    "width": MATE_WIDTH,
}
MATE_NAME_BY_TYPE = {v: k for k, v in MATE_TYPE_BY_NAME.items()}

# --- swMateAlign_e -----------------------------------------------------------
ALIGN_ALIGNED = 0
ALIGN_ANTI_ALIGNED = 1
ALIGN_CLOSEST = 2

ALIGN_BY_NAME = {
    "aligned": ALIGN_ALIGNED,
    "anti_aligned": ALIGN_ANTI_ALIGNED,
    "closest": ALIGN_CLOSEST,
}

# --- swAddComponentConfigOptions_e ------------------------------------------
ADD_COMPONENT_CURRENT_SELECTED_CONFIG = 0
ADD_COMPONENT_NEW_CONFIG = 1
ADD_COMPONENT_EXISTING_CONFIG = 2

# --- swDisplayMode_e ---------------------------------------------------------
DISPLAY_MODE_BY_NAME = {
    "wireframe": 0,
    "hlv": 1,           # hidden lines visible (greyed)
    "hlr": 2,           # hidden lines removed
    "shaded": 3,
    "shaded_edges": 7,
}

# --- Standard drawing view names (SolidWorks reserved, locale-independent) ---
VIEW_NAME_BY_TYPE = {
    "front": "*Front",
    "back": "*Back",
    "left": "*Left",
    "right": "*Right",
    "top": "*Top",
    "bottom": "*Bottom",
    "isometric": "*Isometric",
    "trimetric": "*Trimetric",
    "dimetric": "*Dimetric",
    "current": "*Current",
}

# --- swInsertAnnotation_e (InsertModelAnnotations3 Types bitmask) ------------
# Verified against a SolidWorks 2023 type library. These are high bits in a wide
# bitmask, not 1 and 2 -- passing 1 selects an unrelated annotation class, which is
# why a model-dimension insert would report zero dimensions placed.
INSERT_DIMS_MARKED_FOR_DRAWING = 32768
INSERT_DIMS_NOT_MARKED_FOR_DRAWING = 65536

# --- swCreateSectionViewAtOptions_e -----------------------------------------
# Verified against a SolidWorks 2023 type library. There is no "auto hatch" member:
# hatching is the default, and bit 1 actually means NotAligned -- so passing 1 as a
# harmless default silently un-aligned every section view. Pass 0 for defaults.
SECTION_VIEW_DEFAULT = 0
SECTION_VIEW_NOT_ALIGNED = 1
SECTION_VIEW_OFFSET_SECTION = 2
SECTION_VIEW_CHANGE_DIRECTION = 4
SECTION_VIEW_SCALE_WITH_MODEL = 8
SECTION_VIEW_PARTIAL = 16
SECTION_VIEW_DISPLAY_SURFACE_CUT = 32
SECTION_VIEW_EXCLUDE_FASTENERS = 64
SECTION_VIEW_CUT_SURFACE_BODIES = 128

# --- swAutoInsertCenterMarkType_e (AutoInsertCenterMarks2 InsertType bitmask) --
# Verified against a SolidWorks 2023 type library. 0 is not a member of this enum
# and silently matches nothing -- AutoInsertCenterMarks2 returns False for every
# view regardless of whether it has holes, which reads as "no holes in the model"
# rather than "wrong flag value".
CENTER_MARK_TYPE_HOLE = 1
CENTER_MARK_TYPE_FILLETS = 2
CENTER_MARK_TYPE_SLOTS = 4

# --- swUserPreferenceToggle_e (subset) --------------------------------------
PREF_INPUT_DIM_VAL_ON_CREATE = 10
PREF_MULTICAD_ENABLE_3D_INTERCONNECT = 691
PREF_MULTICAD_APPLY_ONLY_TO_PARTS = 758

# --- swUserPreferenceStringValue_e ------------------------------------------
PREF_DEFAULT_TEMPLATE_PART = 8
PREF_DEFAULT_TEMPLATE_ASSEMBLY = 9
PREF_DEFAULT_TEMPLATE_DRAWING = 10

# --- swExportDataFileType_e -------------------------------------------------
EXPORT_PDF_DATA = 1
EXPORT_DXF_DATA = 2

# --- swSketchConstraint ids (SketchAddConstraints tokens) -------------------
SKETCH_CONSTRAINT_BY_NAME = {
    "horizontal": ("sgHORIZONTAL", 1),
    "vertical": ("sgVERTICAL", 1),
    "coincident": ("sgCOINCIDENT", 2),
    "parallel": ("sgPARALLEL", 2),
    "perpendicular": ("sgPERPENDICULAR", 2),
    "tangent": ("sgTANGENT", 2),
    "equal": ("sgSAMELENGTH", 2),
    "concentric": ("sgCONCENTRIC", 2),
    "midpoint": ("sgATMIDDLE", 2),
    "collinear": ("sgCOLINEAR", 2),
    "symmetric": ("sgSYMMETRIC", 3),
    "fix": ("sgFIXED", 1),
}

# --- Default reference plane names, in tree ordinal order -------------------
# SolidWorks localises these; the ordinal fallback in selection.py resolves
# them language-independently.
DEFAULT_PLANE_ORDINALS = {"front": 0, "top": 1, "right": 2}

PLANE_ALIASES = {
    "front": "Front Plane",
    "front plane": "Front Plane",
    "xy": "Front Plane",
    "top": "Top Plane",
    "top plane": "Top Plane",
    "xz": "Top Plane",
    "right": "Right Plane",
    "right plane": "Right Plane",
    "yz": "Right Plane",
}

# --- SolidWorks major revision -> marketing year ----------------------------
# IModelDoc2/ISldWorks.RevisionNumber major component.
REVISION_TO_YEAR = {
    22: 2014, 23: 2015, 24: 2016, 25: 2017, 26: 2018, 27: 2019,
    28: 2020, 29: 2021, 30: 2022, 31: 2023, 32: 2024, 33: 2025,
    34: 2026, 35: 2027,
}


def year_for_revision(revision: str):
    """Map a RevisionNumber string like '33.2.0' to the marketing year (2025)."""
    try:
        major = int(str(revision).split(".")[0])
    except (ValueError, AttributeError, IndexError):
        return None
    return REVISION_TO_YEAR.get(major)


# --- Directly tested versions -------------------------------------------------
# Years this server's tools have actually been exercised against a live
# SolidWorks instance, feature by feature, not merely "the revision number maps
# to a known year". Every other year in REVISION_TO_YEAR is *unverified but
# structurally supported*: the COM interfaces this server calls have been stable
# across SolidWorks releases for a decade, and constants.sync_enums() re-reads
# every bound enum from that install's own swconst.tlb at connect time, so a
# version with different underlying integer values still gets the *correct*
# ones for itself rather than this server's hardcoded fallbacks. See
# SolidWorksSession.version_support() for how this is combined into one verdict.
VERIFIED_VERSIONS = {2023}


def version_support(year: int | None, revision: str | None, constants_verified: bool) -> dict:
    """Collapse version/constant knowledge into one verdict a caller can act on.

    Pure function (no COM access) so it is unit-testable without a live
    SolidWorks connection -- see :meth:`SolidWorksSession.version_support` for
    the live wrapper that supplies the three inputs.

    Args:
        year: Marketing year from :func:`year_for_revision`, or ``None`` if the
            revision was not recognized.
        revision: Raw ``RevisionNumber`` string, used only in the unsupported
            message.
        constants_verified: Whether ``swconst.tlb`` was read successfully for
            this install (``constants.table()`` returned entries).

    Returns:
        A dict with ``level`` (``"verified"``, ``"compatible"``, ``"degraded"``,
        or ``"unsupported"``), the three booleans that produced it, an overall
        ``supported`` flag, and a human-readable ``message``.
    """
    recognized = year is not None
    tested_live = year in VERIFIED_VERSIONS if recognized else False

    if tested_live:
        level = "verified"
        message = f"SolidWorks {year} has been directly tested against this server's tools."
    elif recognized and constants_verified:
        level = "compatible"
        message = (
            f"SolidWorks {year} has not been directly tested, but its enum constants "
            "were read successfully from this install's own type library, so the "
            "values this server uses match what this version actually expects. "
            "Behaviour should match tested versions."
        )
    elif recognized:
        level = "degraded"
        message = (
            f"SolidWorks {year} is a recognized version, but its enum constants could "
            "not be read from swconst.tlb, so this server is falling back to values "
            "verified on a different version. Preference toggles and some feature "
            "calls may use the wrong constant and either fail or silently do the "
            "wrong thing."
        )
    else:
        level = "unsupported"
        message = (
            f"Revision '{revision}' was not recognized (no known marketing year). "
            "This server has not been validated against this SolidWorks release at all. "
            "It may still work if the COM interfaces are unchanged, but tool use might "
            "cause errors."
        )

    return {
        "level": level,
        "recognized_version": recognized,
        "constants_verified": constants_verified,
        "tested_live": tested_live,
        "supported": recognized and constants_verified,
        "message": message,
    }


def rebuild_derived_tables() -> None:
    """Recompute the name-to-value lookup tables from the current scalars.

    The tables above are built at import time, so they capture whatever the
    hand-maintained literals said. Once
    :func:`solidworks_mcp.com.constants.sync_enums` replaces those literals with the
    values the install actually reports, the tables are stale and must be rebuilt --
    otherwise a corrected constant (``MATE_LOCK`` 15 -> 16, say) would be right
    everywhere except in the lookup callers actually go through.
    """
    global MATE_TYPE_BY_NAME, MATE_NAME_BY_TYPE, ALIGN_BY_NAME, FLANGE_POS_BY_NAME

    MATE_TYPE_BY_NAME = {
        "coincident": MATE_COINCIDENT,
        "concentric": MATE_CONCENTRIC,
        "perpendicular": MATE_PERPENDICULAR,
        "parallel": MATE_PARALLEL,
        "tangent": MATE_TANGENT,
        "distance": MATE_DISTANCE,
        "angle": MATE_ANGLE,
        "symmetric": MATE_SYMMETRIC,
        "lock": MATE_LOCK,
        "width": MATE_WIDTH,
    }
    MATE_NAME_BY_TYPE = {v: k for k, v in MATE_TYPE_BY_NAME.items()}

    ALIGN_BY_NAME = {
        "aligned": ALIGN_ALIGNED,
        "anti_aligned": ALIGN_ANTI_ALIGNED,
        "closest": ALIGN_CLOSEST,
    }

    FLANGE_POS_BY_NAME = {
        "material_inside": FLANGE_POS_MATERIAL_INSIDE,
        "material_outside": FLANGE_POS_MATERIAL_OUTSIDE,
        "bend_outside": FLANGE_POS_BEND_OUTSIDE,
        "centerline": FLANGE_POS_BEND_CENTERLINE,
        "bend_sharp": FLANGE_POS_BEND_SHARP,
        "bend_tangent": FLANGE_POS_BEND_TANGENT,
    }
