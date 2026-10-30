"""
The scripting escape hatch.

The SolidWorks API has thousands of members; no tool surface covers all of it. The
reference Python project's ``execute_python`` was its most valuable tool for
exactly that reason -- it is what let users reach features the server had not
wrapped.

Kept here, with three improvements: the code runs on the **STA thread** like every
other COM call (so ``CreateDefinition``-style APIs work in ad-hoc scripts too), the
helper namespace includes this server's own compat and selection utilities rather
than only the raw COM handle, and it can be disabled by configuration.

Security note: this executes arbitrary Python in the server process, with whatever
privileges the server holds. It is appropriate for a local single-user workstation
and inappropriate for a shared or network-exposed deployment. Set
``allow_execute_python: false`` there.
"""

from __future__ import annotations

import io
import json
import logging
import math
import os
import traceback
from contextlib import redirect_stderr, redirect_stdout

from ..com import compat, enums, selection
from ..com.connection import SolidWorksSession
from ..util import units
from ..util.result import ToolError

logger = logging.getLogger(__name__)


def execute_python(
    session: SolidWorksSession,
    code: str,
    converter: units.UnitConverter | None = None,
) -> dict:
    """Run Python against the live SolidWorks COM objects.

    The escape hatch for anything the tools do not cover. Runs on the same STA
    thread as every other COM call, so the whole API surface behaves as it does
    inside the tools -- including the feature-data APIs that fail from an MTA
    caller.

    Available names:

    * ``sw`` -- the ``ISldWorks`` application object
    * ``doc`` -- the active ``IModelDoc2`` (``None`` if nothing is open)
    * ``session`` -- this server's session, for ``session.doc_type(doc)`` etc.
    * ``compat`` -- ``pm``, ``invoke_first``, ``iter_features``, ``as_list``, ...
      Use ``compat.pm(obj, "GetTitle")`` rather than raw attribute access to stay
      version-portable.
    * ``sel`` -- the selection helpers, including ``sel.flatten_faces(doc)`` and
      ``sel.select_entity(...)``
    * ``enums`` -- SolidWorks constants as plain ints
    * ``units`` -- the unit converter module; ``uc`` is a ready-made converter
    * ``math``, ``json``, ``os``

    Set a ``result`` variable, or just ``print``: both stdout and ``result`` come
    back in the response.

    Args:
        code: Python source to execute.

    Returns:
        Captured ``stdout``, ``stderr``, and the ``result`` variable if set.
    """
    if not session.config.allow_execute_python:
        raise ToolError(
            "EXECUTION_DISABLED",
            "execute_python is disabled by configuration.",
            hint="Set allow_execute_python to true in the server config to enable it. "
                 "It grants arbitrary code execution, so leave it off for shared deployments.",
        )

    if not code or not code.strip():
        raise ToolError("MISSING_PARAMETER", "code is required.")

    doc = compat.pm(session.app, "ActiveDoc", default=None)
    namespace = {
        "sw": session.app,
        "doc": doc,
        "session": session,
        "compat": compat,
        "sel": selection,
        "enums": enums,
        "units": units,
        "uc": converter or units.UnitConverter(session.config.default_unit),
        "math": math,
        "json": json,
        "os": os,
        "result": None,
        "__name__": "__solidworks_mcp_script__",
    }

    stdout, stderr = io.StringIO(), io.StringIO()
    try:
        with redirect_stdout(stdout), redirect_stderr(stderr):
            exec(compile(code, "<execute_python>", "exec"), namespace)  # noqa: S102
    except SyntaxError as exc:
        raise ToolError(
            "SYNTAX_ERROR",
            f"{exc.msg} (line {exc.lineno})",
            details={"line": exc.lineno, "offset": exc.offset, "text": exc.text},
        ) from None
    except Exception as exc:
        # A traceback is the point of this tool -- surface it rather than
        # collapsing the failure into a code.
        raise ToolError(
            "EXECUTION_ERROR",
            f"{type(exc).__name__}: {exc}",
            hint="The script raised. Check the traceback in details.",
            details={
                "traceback": traceback.format_exc(limit=12),
                "stdout": stdout.getvalue(),
                "stderr": stderr.getvalue(),
            },
        ) from None

    result_value = namespace.get("result")
    return {
        "stdout": stdout.getvalue() or None,
        "stderr": stderr.getvalue() or None,
        "result": _describe(result_value),
        "document": compat.pm(doc, "GetTitle", default=None) if doc is not None else None,
    }


def _describe(value):
    """Render a script result as something JSON-serialisable."""
    if value is None:
        return None
    if isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (list, tuple)):
        return [_describe(item) for item in value][:500]
    if isinstance(value, dict):
        return {str(k): _describe(v) for k, v in list(value.items())[:500]}
    return repr(value)[:4000]


def run_macro(
    session: SolidWorksSession,
    file_path: str,
    module: str,
    procedure: str = "main",
) -> dict:
    """Run an existing SolidWorks macro (``.swp``, ``.swb`` or ``.dll``).

    Complements ``execute_python``: teams accumulate VBA macros over years, and
    being able to invoke them keeps that work usable from an agent instead of
    requiring a rewrite.

    Args:
        file_path: Absolute path to the macro file.
        module: Module name inside the macro.
        procedure: Procedure to call. Defaults to ``main``.
    """
    if not os.path.isfile(file_path):
        raise ToolError("FILE_NOT_FOUND", f"No macro at '{file_path}'.")

    error = compat.byref_int()
    report = compat.invoke_bool(session.app, [
        ("RunMacro2", (file_path, module, procedure, 0, error)),
        ("RunMacro", (file_path, module, procedure)),
    ])

    if not report.ok:
        raise ToolError(
            "MACRO_FAILED",
            f"Running '{os.path.basename(file_path)}' failed "
            f"(module='{module}', procedure='{procedure}', error={error.value}).",
            hint="Check the module and procedure names match the macro's contents exactly.",
            details={"error_code": error.value, "attempts": report.diagnostic()},
        )

    return {
        "macro": file_path,
        "module": module,
        "procedure": procedure,
        "error_code": error.value,
        "api": report.signature,
    }
