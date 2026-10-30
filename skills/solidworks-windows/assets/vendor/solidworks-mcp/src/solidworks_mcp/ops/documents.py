"""
Document lifecycle: create, open, save, close, activate, export, rebuild.

All functions here run **on the STA thread** -- they receive the live session and
touch COM directly. The server layer marshals them.
"""

from __future__ import annotations

import logging
import os
import time

import pythoncom

from ..com import compat, enums, selection
from ..com import constants as swconst
from ..com.compat import byref_int, nothing, pm
from ..com.connection import SolidWorksError, SolidWorksSession
from ..util.result import ToolError

logger = logging.getLogger(__name__)

#: Export formats and the extension SolidWorks infers the translator from.
EXPORT_FORMATS = {
    "STEP": ".step",
    "IGES": ".igs",     # the IGES translator rejects '.iges' -> SaveAs error 256
    "STL": ".stl",
    "PARASOLID": ".x_t",
    "3MF": ".3mf",
    "OBJ": ".obj",
    "PDF": ".pdf",
    "DWG": ".dwg",
    "DXF": ".dxf",
    "EDRW": ".edrw",
    "JPG": ".jpg",
    "PNG": ".png",
}

#: Formats that only make sense from a drawing document.
DRAWING_ONLY_FORMATS = {"DWG", "DXF"}

NATIVE_EXTENSIONS = {".sldprt", ".sldasm", ".slddrw"}


def doc_state(session: SolidWorksSession, doc=None) -> dict:
    """A compact snapshot of what is currently active.

    Returned alongside every tool result so an agent always knows the context its
    next call will land in, without a separate query.
    """
    if doc is None:
        doc = pm(session.app, "ActiveDoc", default=None)
    if doc is None:
        return {"document": None, "type": None, "active_sketch": None}

    sketch_name = None
    manager = pm(doc, "SketchManager", default=None)
    if manager is not None:
        sketch = pm(manager, "ActiveSketch", default=None)
        if sketch is not None:
            sketch_name = compat.feature_name(sketch, default="Sketch")

    return {
        "document": pm(doc, "GetTitle", default=None),
        "type": session.doc_type(doc),
        "path": pm(doc, "GetPathName", default="") or None,
        "active_sketch": sketch_name,
    }


# ---------------------------------------------------------------------------
# Create / open
# ---------------------------------------------------------------------------

def new_document(
    session: SolidWorksSession,
    doc_type: str,
    template_path: str | None = None,
    paper_width: float = 0.420,
    paper_height: float = 0.297,
) -> dict:
    """Create a blank part, assembly or drawing.

    Consolidates what the reference projects split across ``open_new_part``,
    ``open_new_assembly``, ``create_new_part``, ``create_new_assembly`` and
    ``create_drawing``.

    Args:
        doc_type: ``"part"``, ``"assembly"`` or ``"drawing"``.
        template_path: Explicit template; auto-resolved when omitted.
        paper_width: Drawing sheet width in metres (default A3 landscape).
        paper_height: Drawing sheet height in metres.
    """
    template = session.template_for(doc_type, template_path)

    if doc_type == "drawing":
        # swDwgPapersUserDefined = 12; explicit dimensions beat a paper-size enum
        # because sheet-size enums differ between metric and imperial installs.
        doc = session.app.NewDocument(template, 12, paper_width, paper_height)
    else:
        doc = session.app.NewDocument(template, 0, 0, 0)

    if doc is None:
        raise SolidWorksError(
            "DOCUMENT_CREATION_FAILED",
            f"NewDocument returned null for template '{template}'. The template may be "
            f"corrupt or built by an incompatible SolidWorks version.",
            {"template": template},
        )

    title = pm(doc, "GetTitle", default=None)
    if not title:
        # Observed transient hazard right after SolidWorks starts or restarts: the
        # very first NewDocument call can return a document whose title/state isn't
        # ready yet, one message-loop tick behind. Give the STA thread's queue a
        # moment to catch up and re-read from the SAME reference -- do not create
        # a second document, which would just leave the first one orphaned.
        pythoncom.PumpWaitingMessages()
        time.sleep(0.5)
        title = pm(doc, "GetTitle", default=None)

    callback = getattr(session, "on_new_document", None)
    if callback is not None:
        callback(doc)

    return {
        "template": template,
        "document": title,
        "type": session.doc_type(doc),
    }


def open_document(
    session: SolidWorksSession,
    file_path: str,
    read_only: bool = False,
    as_assembly: bool = False,
    configuration: str | None = None,
) -> dict:
    """Open an existing file, native or foreign, with a staged import fallback.

    Native ``.sldprt``/``.sldasm``/``.slddrw`` open by type straight from the
    extension. Foreign formats (STEP, IGES, Parasolid, and native NX/CATIA/Creo
    files) need a three-stage escalation, because ``OpenDoc6`` alone returns null
    with the misleading error 2097152 (``swFileRequiresRepairError``) whenever 3D
    Interconnect is off:

    1. ``OpenDoc6`` -- succeeds when Interconnect is already enabled.
    2. ``LoadFile4`` -- the *classic* translator. Handles STEP/IGES/Parasolid
       without Interconnect. Note it imports an assembly STEP as a **multibody
       part**, so ``as_assembly`` skips this stage.
    3. Enable Interconnect (toggle 691) and retry ``OpenDoc6``. Required for
       native NX/CATIA/Creo, which have no classic translator.

    Args:
        file_path: Absolute path to the file.
        read_only: Open without acquiring write access.
        as_assembly: For foreign files only -- import a structured STEP as a real
            assembly with components rather than a multibody part. Forces the
            Interconnect path.
        configuration: Configuration to activate on open (native files).
    """
    if not os.path.isfile(file_path):
        raise ToolError(
            "FILE_NOT_FOUND",
            f"No file at '{file_path}'.",
            hint="Pass an absolute path including the extension.",
        )

    extension = os.path.splitext(file_path)[1].lower()
    is_foreign = extension not in NATIVE_EXTENSIONS
    kind = enums.DOC_TYPE_BY_EXT.get(extension, enums.DOC_PART)
    if is_foreign and as_assembly:
        kind = enums.DOC_ASSEMBLY

    options = enums.OPEN_SILENT
    if read_only:
        options |= enums.OPEN_READONLY

    errors, warnings = byref_int(), byref_int()
    doc = None
    route = None

    # Stage 3 is the only route for a structured foreign assembly, so try it first
    # in that case rather than letting LoadFile4 flatten it to a multibody part.
    if is_foreign and as_assembly:
        doc, route = _open_with_interconnect(session, file_path, kind, options)

    if doc is None:
        doc = session.app.OpenDoc6(
            file_path, kind, options, configuration or "", errors, warnings
        )
        route = "OpenDoc6"

    if doc is None and is_foreign:
        load_errors = byref_int()
        doc = pm(session.app, "LoadFile4", file_path, "r", nothing(), load_errors, default=None)
        route = "LoadFile4 (classic translator)"

    if doc is None and is_foreign:
        doc, route = _open_with_interconnect(session, file_path, kind, options)

    if doc is None:
        raise SolidWorksError(
            "OPEN_FAILED",
            f"Could not open '{os.path.basename(file_path)}'. "
            f"OpenDoc6 reported errors={errors.value}, warnings={warnings.value}"
            + (", and the classic translator and 3D Interconnect fallbacks also failed."
               if is_foreign else "."),
            {
                "open_errors": errors.value,
                "open_warnings": warnings.value,
                "foreign": is_foreign,
                "hint": "errors=2097152 means swFileRequiresRepairError; for foreign files "
                        "that usually means no translator is available. Convert to STEP.",
            },
        )

    # OpenDoc6 on an ALREADY-OPEN file returns its handle but does NOT activate
    # it -- the previously active document silently stays active and every
    # follow-up read hits the wrong document. Activate explicitly.
    title = pm(doc, "GetTitle", default=None)
    if title:
        activate_errors = byref_int()
        pm(session.app, "ActivateDoc3", title, False, enums.ACTIVATE_DONT_REBUILD,
           activate_errors, default=None)

    callback = getattr(session, "on_open_document", None)
    if callback is not None:
        callback(doc)

    return {
        "document": title,
        "type": session.doc_type(doc),
        "path": pm(doc, "GetPathName", default="") or file_path,
        "opened_via": route,
        "warnings": warnings.value,
    }


def _open_with_interconnect(session, file_path: str, kind: int, options: int):
    """Enable 3D Interconnect for one open, then restore the prior toggles.

    Leaving Interconnect on globally changes how *every* later foreign open behaves,
    so the toggles are restored in a ``finally`` block.

    Both toggle indices are only used when **verified** against the install's own
    type library, because writing to a wrongly-guessed index would change an
    unrelated user preference. Unverified, this route is skipped and the caller
    reports the open failure honestly instead.
    """
    app = session.app

    interconnect, interconnect_ok = swconst.verified(
        "swMultiCAD_Enable3DInterconnect", enums.PREF_MULTICAD_ENABLE_3D_INTERCONNECT
    )
    parts_only, parts_only_ok = swconst.verified(
        "swMultiCAD_ApplyOnlyToParts", enums.PREF_MULTICAD_APPLY_ONLY_TO_PARTS
    )
    if not interconnect_ok:
        logger.warning(
            "Skipping the 3D Interconnect import route: the toggle index could not be "
            "verified against this install, and writing an unverified preference index "
            "risks changing an unrelated setting."
        )
        return None, None

    needs_parts_only = kind == enums.DOC_ASSEMBLY and parts_only_ok

    try:
        prior_enabled = bool(app.GetUserPreferenceToggle(interconnect))
        prior_parts_only = (
            bool(app.GetUserPreferenceToggle(parts_only)) if needs_parts_only else False
        )
    except Exception:
        return None, None

    try:
        if not prior_enabled:
            app.SetUserPreferenceToggle(interconnect, True)
        if needs_parts_only and prior_parts_only:
            app.SetUserPreferenceToggle(parts_only, False)
        errors, warnings = byref_int(), byref_int()
        doc = app.OpenDoc6(file_path, kind, options, "", errors, warnings)
        return doc, "OpenDoc6 + 3D Interconnect"
    except Exception as exc:
        logger.debug("Interconnect open failed: %s", exc)
        return None, None
    finally:
        try:
            if not prior_enabled:
                app.SetUserPreferenceToggle(interconnect, False)
            if needs_parts_only and prior_parts_only:
                app.SetUserPreferenceToggle(parts_only, True)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Save / close / activate
# ---------------------------------------------------------------------------

def save_document(session: SolidWorksSession, file_path: str | None = None) -> dict:
    """Save the active document, in place or to a new path.

    A part must be saved to disk before a drawing view can reference it, which is
    why this returns the resolved path.

    Args:
        file_path: Target path (Save As, creating parent directories). Omit for
            an in-place save, which requires the document to already have a path.
    """
    doc = session.active_doc()

    if file_path:
        directory = os.path.dirname(os.path.abspath(file_path))
        if directory:
            os.makedirs(directory, exist_ok=True)
        errors, warnings = byref_int(), byref_int()
        extension = pm(doc, "Extension", default=None)
        saved = False
        if extension is not None:
            report = compat.invoke_bool(extension, [
                ("SaveAs3", (file_path, enums.SAVE_AS_CURRENT_VERSION, enums.SAVE_SILENT,
                             nothing(), errors, warnings)),
                ("SaveAs2", (file_path, enums.SAVE_AS_CURRENT_VERSION, enums.SAVE_SILENT,
                             nothing(), nothing(), errors, warnings)),
            ])
            saved = report.ok
        if not saved:
            # IModelDoc2.SaveAs4 predates the Extension-based API.
            report = compat.invoke_bool(doc, [
                ("SaveAs4", (file_path, enums.SAVE_AS_CURRENT_VERSION,
                             enums.SAVE_SILENT, errors, warnings)),
                ("SaveAs2", (file_path, enums.SAVE_AS_CURRENT_VERSION, "", True,
                             errors, warnings)),
            ])
            saved = report.ok
        if not saved:
            raise SolidWorksError(
                "SAVE_FAILED",
                f"Save As '{file_path}' failed (errors={errors.value}, warnings={warnings.value}).",
                {"errors": errors.value, "warnings": warnings.value},
            )
        resolved = file_path
    else:
        existing = pm(doc, "GetPathName", default="")
        if not existing:
            raise ToolError(
                "NO_DOCUMENT_PATH",
                "This document has never been saved, so there is no path to save to.",
                hint="Pass file_path to do a Save As.",
            )
        errors, warnings = byref_int(), byref_int()
        report = compat.invoke_bool(doc, [
            ("Save3", (enums.SAVE_SILENT, errors, warnings)),
            ("Save2", (True,)),
        ])
        if not report.ok:
            raise SolidWorksError(
                "SAVE_FAILED",
                f"Save failed (errors={errors.value}, warnings={warnings.value}).",
                {"attempts": report.diagnostic()},
            )
        resolved = existing

    return {"path": resolved, "document": pm(doc, "GetTitle", default=None)}


def close_document(
    session: SolidWorksSession,
    save: bool = False,
    close_all: bool = False,
) -> dict:
    """Close the active document, or every open document.

    Args:
        save: Save before closing. When false, unsaved changes are discarded.
        close_all: Close everything including *invisibly loaded* component
            documents. A per-active-document close loop can never reach those,
            and a lingering same-titled document blocks opening another folder's
            part (error 65536) and keeps files locked -- so this is the right
            choice between unrelated tasks.
    """
    if close_all:
        report = compat.invoke_any(session.app, [
            ("CloseAllDocuments", (True,)),
        ])
        if not report.ok:
            raise SolidWorksError(
                "CLOSE_FAILED",
                "CloseAllDocuments is not available on this SolidWorks build.",
                {"attempts": report.diagnostic()},
            )
        return {"closed": "all"}

    doc = session.active_doc()
    title = pm(doc, "GetTitle", default=None)

    if save:
        path = pm(doc, "GetPathName", default="")
        if path:
            errors, warnings = byref_int(), byref_int()
            compat.invoke_bool(doc, [("Save3", (enums.SAVE_SILENT, errors, warnings))])
        else:
            raise ToolError(
                "NO_DOCUMENT_PATH",
                f"'{title}' has never been saved, so save-before-close cannot run.",
                hint="Call save_document with a file_path first, or close with save=false.",
            )

    session.app.CloseDoc(title)
    return {"closed": title, "saved": bool(save)}


def activate_document(session: SolidWorksSession, title: str) -> dict:
    """Make an already-open document active by title.

    Lets a caller read or compare another open document -- activate the original,
    analyse it, activate the rebuild -- without reopening from disk.

    Args:
        title: Document title, with or without the extension (``"bracket"`` or
            ``"bracket.SLDPRT"``).
    """
    errors = byref_int()
    doc = pm(session.app, "ActivateDoc3", title, False,
             enums.ACTIVATE_DONT_REBUILD, errors, default=None)
    if doc is None:
        doc = pm(session.app, "ActivateDoc2", title, False, errors, default=None)

    if doc is None:
        raise SolidWorksError(
            "ACTIVATE_FAILED",
            f"Could not activate '{title}' (error={errors.value}).",
            {"open_documents": list_documents(session)["documents"]},
        )

    return {
        "document": pm(doc, "GetTitle", default=title),
        "type": session.doc_type(doc),
    }


def list_documents(session: SolidWorksSession) -> dict:
    """Every open document, with the active one flagged."""
    docs = compat.as_list(pm(session.app, "GetDocuments", default=None))
    active = pm(session.app, "ActiveDoc", default=None)
    active_title = pm(active, "GetTitle", default=None) if active is not None else None

    entries = []
    for doc in docs:
        title = pm(doc, "GetTitle", default=None)
        entries.append({
            "title": title,
            "type": session.doc_type(doc),
            "path": pm(doc, "GetPathName", default="") or None,
            "active": title == active_title,
            "modified": bool(pm(doc, "GetSaveFlag", default=False)),
        })
    return {"count": len(entries), "documents": entries, "active": active_title}


def rebuild_document(session: SolidWorksSession, force: bool = False) -> dict:
    """Rebuild the active document and report whether anything errored.

    Neither reference project exposed this, yet it is the cheapest way to
    confirm a model is still valid after a batch of parametric edits.

    Args:
        force: Force-rebuild every feature (``Ctrl+Q``) rather than only what is
            out of date (``Ctrl+B``). Slower, but catches latent failures.
    """
    doc = session.active_doc()

    if force:
        report = compat.invoke_any(doc, [
            ("ForceRebuild3", (False,)),
            ("EditRebuild3", ()),
        ])
    else:
        report = compat.invoke_any(doc, [("EditRebuild3", ()), ("EditRebuild", ())])

    if not report.ok:
        raise SolidWorksError(
            "REBUILD_FAILED", "No rebuild API responded.", {"attempts": report.diagnostic()}
        )

    return {
        "forced": bool(force),
        "feature_errors": compat.error_count(doc),
        "needs_further_rebuild": compat.needs_rebuild(doc),
        "body": selection.body_summary(doc) if session.doc_type(doc) == "PART" else None,
    }


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

def export_document(
    session: SolidWorksSession,
    formats,
    output_path: str,
) -> dict:
    """Export the active document to one or more formats in a single call.

    Merges the reference projects' separate ``export_document`` and
    ``batch_export``: pass one format or several, and ``output_path`` is treated
    as a full filename for a single format or as a basename for several.

    Args:
        formats: One format string, or a list -- ``STEP``, ``IGES``, ``STL``,
            ``PARASOLID``, ``3MF``, ``OBJ``, ``PDF``, ``DWG``, ``DXF``, ``JPG``,
            ``PNG``. ``DWG``/``DXF`` require an active drawing.
        output_path: For a single format, the full output path (its extension is
            corrected if it disagrees with the format). For several formats, the
            path without an extension.
    """
    doc = session.active_doc()
    kind = session.doc_type(doc)

    if isinstance(formats, str):
        requested = [formats]
    else:
        requested = list(formats)

    normalised = []
    for entry in requested:
        token = str(entry).strip().upper()
        if token == "STP":
            token = "STEP"
        if token == "X_T":
            token = "PARASOLID"
        if token not in EXPORT_FORMATS:
            raise ToolError(
                "INVALID_PARAMETER",
                f"Unsupported export format '{entry}'.",
                hint="Supported: " + ", ".join(sorted(EXPORT_FORMATS)),
            )
        normalised.append(token)

    if not normalised:
        raise ToolError("MISSING_PARAMETER", "At least one export format is required.")

    single = len(normalised) == 1
    base = os.path.splitext(output_path)[0] if not single else output_path
    directory = os.path.dirname(os.path.abspath(base or output_path))
    if directory:
        os.makedirs(directory, exist_ok=True)

    results = []
    for fmt in normalised:
        extension = EXPORT_FORMATS[fmt]

        if fmt in DRAWING_ONLY_FORMATS and kind != "DRAWING":
            results.append({
                "format": fmt,
                "ok": False,
                "reason": f"{fmt} export requires an active drawing; the active document "
                          f"is a {kind.lower()}.",
            })
            continue

        target = (
            os.path.splitext(output_path)[0] + extension if single
            else base + extension
        )

        errors, warnings = byref_int(), byref_int()
        export_data = nothing()
        if fmt == "PDF":
            # PDF needs an ExportPdfData options object; without it SaveAs3
            # fails on drawings.
            data = pm(session.app, "GetExportFileData", enums.EXPORT_PDF_DATA, default=None)
            if data is not None:
                export_data = data

        extension_obj = pm(doc, "Extension", default=None)
        report = compat.invoke_bool(extension_obj, [
            ("SaveAs3", (target, enums.SAVE_AS_CURRENT_VERSION, enums.SAVE_SILENT,
                         export_data, nothing(), errors, warnings)),
            ("SaveAs", (target, enums.SAVE_AS_CURRENT_VERSION, enums.SAVE_SILENT,
                        export_data, errors, warnings)),
        ]) if extension_obj is not None else compat.InvocationReport()

        if not report.ok:
            report = compat.invoke_bool(doc, [
                ("SaveAs4", (target, enums.SAVE_AS_CURRENT_VERSION, enums.SAVE_SILENT,
                             errors, warnings)),
            ])

        results.append({
            "format": fmt,
            "ok": bool(report.ok),
            "path": target if report.ok else None,
            "reason": None if report.ok
                      else f"SaveAs reported errors={errors.value}, warnings={warnings.value}",
            "errors": errors.value,
        })

    succeeded = [r for r in results if r["ok"]]
    if not succeeded:
        raise SolidWorksError(
            "EXPORT_FAILED",
            "Every requested export failed. "
            + "; ".join(f"{r['format']}: {r['reason']}" for r in results),
            {"results": results},
        )

    return {
        "exported": len(succeeded),
        "requested": len(results),
        "results": results,
        "document_type": kind,
    }


def set_configuration(session: SolidWorksSession, name: str) -> dict:
    """Activate a named configuration on the active document.

    Configurations are how a single SolidWorks file carries size variants, so
    this is the read half of parametric family work (``modify_dimension`` being
    the write half). Neither reference project exposed it.

    ``ShowConfiguration2`` is unreliable on its own boolean return -- verified
    live against a configuration just created in the same session, it reported
    ``False`` on the very first switch even though the configuration genuinely
    became active (confirmed via ``ConfigurationManager.ActiveConfiguration``).
    Same silently-wrong-return contract as ``InsertRib``/``InsertFeatureShell``
    elsewhere in this codebase, so success is verified the same way: by reading
    back the effect rather than trusting the call's own report of it.
    """
    doc = session.active_doc()
    available = [str(n) for n in compat.as_list(pm(doc, "GetConfigurationNames", default=None))]

    if name not in available:
        raise ToolError(
            "CONFIGURATION_NOT_FOUND",
            f"No configuration named '{name}' in this document.",
            hint=f"Available configurations: {', '.join(available) or '(none)'}.",
            details={"available": available},
        )

    pm(doc, "ShowConfiguration2", name, default=False)
    compat.invoke_any(doc, [("EditRebuild3", ())])

    manager = pm(doc, "ConfigurationManager", default=None)
    active = pm(manager, "ActiveConfiguration", default=None) if manager is not None else None
    active_name = pm(active, "Name", default=None) if active is not None else None

    if active_name != name:
        raise SolidWorksError(
            "CONFIGURATION_ACTIVATE_FAILED",
            f"Could not activate configuration '{name}' (active configuration is now "
            f"'{active_name}').",
        )

    return {"configuration": name, "available": available}


def list_configurations(session: SolidWorksSession) -> dict:
    """Every configuration in the active document, with the active one named."""
    doc = session.active_doc()
    names = [str(n) for n in compat.as_list(pm(doc, "GetConfigurationNames", default=None))]
    active = pm(doc, "ConfigurationManager", default=None)
    active_name = None
    if active is not None:
        config = pm(active, "ActiveConfiguration", default=None)
        if config is not None:
            active_name = pm(config, "Name", default=None)
    return {"count": len(names), "configurations": names, "active": active_name}
