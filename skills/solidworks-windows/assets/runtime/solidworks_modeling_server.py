"""Basic SolidWorks modeling, restricted to documents created by this session."""

from __future__ import annotations

import functools
import json
import os
import threading
from pathlib import Path
from typing import Any

import pythoncom
from mcp.server.fastmcp import FastMCP

from workspace_config import WORKSPACE, is_reserved_path
import solidworks_query_server as query
from solidworks_mcp.com import compat
from solidworks_mcp.util import result as R

upstream = query.upstream
CAD_ROOT = (WORKSPACE / "CAD").resolve()
LOCK = threading.RLock()
MANAGED = []  # (IUnknown identity, writable); only accessed on the COM STA thread.

mcp = FastMCP(
    "solidworks",
    instructions=(
        "Basic part modeling. Start SolidWorks manually. Mutation tools only work "
        "on parts created by this server session. All lengths default to mm. "
        "save_document requires a NEW .SLDPRT path under the configured workspace CAD directory and "
        "refuses overwrites. open_document opens a workspace part read-only. "
        "close_document refuses unsaved changes. No scripts, macros, deletion, "
        "assemblies or exports are exposed. Check every JSON ok flag and stop on "
        "failure. Geometrically correct coordinates do not imply a fully defined "
        "sketch or an engineering strength check. The plate recipe uses native "
        "dimensions by default. set_plate_parameters synchronizes repeated D/E "
        "dimensions. Global-equation mode remains disabled pending VBA repair."
    ),
)


def _identity(doc):
    return doc._oleobj_.QueryInterface(pythoncom.IID_IUnknown)


def _remember(writable):
    doc = upstream.SESSION.active_doc()
    MANAGED.append((_identity(doc), writable))


def _require_managed(writable=True):
    doc = upstream.SESSION.active_doc()
    identity = _identity(doc)
    for known, can_write in MANAGED:
        if known == identity and (can_write or not writable):
            return doc
    raise R.ToolError(
        "DOCUMENT_NOT_OWNED",
        "The active document was not created by this modeling server session.",
        hint="Create a new part with new_document. Existing user documents are query-only.",
    )


def _checked_path(file_path, *, must_exist):
    supplied = Path(file_path)
    if not supplied.is_absolute() or is_reserved_path(supplied):
        raise ValueError("Use an absolute, ordinary Windows file path.")
    resolved = supplied.resolve()
    if not resolved.is_relative_to(CAD_ROOT) or resolved.suffix.lower() != ".sldprt":
        raise ValueError("Only .SLDPRT files inside the configured workspace CAD directory are allowed.")
    if any(part.endswith((" ", ".")) or ":" in part for part in supplied.parts[1:]):
        raise ValueError("Ambiguous Windows path components are not allowed.")
    if must_exist and not resolved.is_file():
        raise FileNotFoundError(str(resolved))
    if not must_exist and resolved.exists():
        raise FileExistsError("Refusing to overwrite " + str(resolved))
    return str(resolved)


for function in (query.connect_solidworks, query.get_solidworks_info, query.list_documents,
                 upstream.analyze_model):
    mcp.tool()(function)


def _register_mutation(name):
    operation = getattr(upstream, name)

    @functools.wraps(operation)
    def guarded(*args, **kwargs):
        with LOCK:
            try:
                upstream.SESSION.ensure(launch=False)
                upstream.SESSION.com.call(_require_managed)
                return operation(*args, **kwargs)
            except Exception as exc:
                return R.from_exception(name, exc)

    mcp.tool()(guarded)


for name in ("create_sketch", "add_sketch_entities", "exit_sketch",
             "add_sketch_constraint", "extrude_feature", "rebuild_document",
             "edit_sketch", "modify_dimension"):
    _register_mutation(name)


@mcp.tool()
def new_document(template_path: str | None = None) -> str:
    """Create a new part and allow this session to edit that part only."""
    with LOCK:
        try:
            if template_path and Path(template_path).suffix.lower() != ".prtdot":
                raise ValueError("A part template must have the .prtdot extension.")
            response = upstream.new_document(doc_type="part", template_path=template_path)
            if json.loads(response).get("ok"):
                upstream.SESSION.com.call(_remember, True)
            return response
        except Exception as exc:
            return R.from_exception("new_document", exc)


@mcp.tool()
def save_document(file_path: str) -> str:
    """Save a session-created part to a new .SLDPRT file under the configured workspace CAD directory."""
    with LOCK:
        try:
            destination = _checked_path(file_path, must_exist=False)
            upstream.SESSION.ensure(launch=False)
            upstream.SESSION.com.call(_require_managed)
            return upstream.save_document(file_path=destination)
        except FileExistsError as exc:
            return R.from_exception("save_document", R.ToolError(
                "FILE_EXISTS", str(exc), hint="Choose a new versioned file name."
            ))
        except Exception as exc:
            return R.from_exception("save_document", exc)


@mcp.tool()
def open_document(file_path: str) -> str:
    """Open a saved workspace .SLDPRT read-only for verification."""
    with LOCK:
        try:
            source = _checked_path(file_path, must_exist=True)
            response = upstream.open_document(file_path=source, read_only=True)
            if json.loads(response).get("ok"):
                upstream.SESSION.com.call(_remember, False)
            return response
        except Exception as exc:
            return R.from_exception("open_document", exc)


@mcp.tool()
def close_document() -> str:
    """Close only the active managed document, refusing to discard unsaved changes."""
    with LOCK:
        try:
            upstream.SESSION.ensure(launch=False)

            def check_saved():
                doc = _require_managed(writable=False)
                dirty = compat.pm(doc, "GetSaveFlag", default=None)
                if dirty is not False and dirty != 0:
                    raise ValueError("Save changes to a new file before closing this document.")
                return _identity(doc)

            identity = upstream.SESSION.com.call(check_saved)
            response = upstream.close_document(save=False, close_all=False)
            if json.loads(response).get("ok"):
                def forget():
                    MANAGED[:] = [(known, writable) for known, writable in MANAGED if known != identity]
                upstream.SESSION.com.call(forget)
            return response
        except Exception as exc:
            return R.from_exception("close_document", exc)


import parametric_plate_tools


@mcp.tool()
def add_sketch_dimension(point: Any, value: float, label_offset: Any = None,
                         is_angle: bool = False, unit: str | None = None) -> str:
    """Add a native driving dimension, temporarily suppressing and restoring its input dialog."""
    with LOCK:
        token = None
        try:
            upstream.SESSION.ensure(launch=False)
            upstream.SESSION.com.call(_require_managed)
            token = upstream.SESSION.com.call(parametric_plate_tools.suspend_dimension_input, upstream.SESSION)
            return upstream.add_sketch_dimension(point=point, value=value, label_offset=label_offset,
                                                 is_angle=is_angle, unit=unit)
        except Exception as exc:
            return R.from_exception("add_sketch_dimension", exc)
        finally:
            if token is not None:
                upstream.SESSION.com.call(parametric_plate_tools.restore_dimension_input, upstream.SESSION, token)


parametric_plate_tools.register(mcp, upstream, _require_managed, _remember, LOCK)


if __name__ == "__main__":
    try:
        mcp.run(transport="stdio")
    finally:
        upstream.SESSION.shutdown()
