"""
The uniform tool-result envelope.

Every tool returns the same JSON shape so an agent can branch on ``ok`` without
parsing prose:

.. code-block:: json

    {
      "ok": true,
      "op": "extrude_feature",
      "message": "Created Boss-Extrude1 (25 mm blind).",
      "data": {"feature": "Boss-Extrude1", "...": "..."},
      "state": {"document": "Part1", "type": "PART", "active_sketch": null}
    }

Failures carry a stable ``error.code`` plus a ``hint`` naming the next action.
Both reference projects returned free-text messages that an agent could only
regex; the codes here are the contract.
"""

from __future__ import annotations

import json
import logging
import traceback
from typing import Any

logger = logging.getLogger(__name__)


class ToolError(Exception):
    """A tool-level failure with a machine-readable code and a recovery hint."""

    def __init__(self, code: str, message: str, hint: str = "", details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint
        self.details = details or {}


def ok(
    op: str,
    message: str,
    data: dict | None = None,
    state: dict | None = None,
) -> str:
    """Serialise a successful result."""
    payload: dict[str, Any] = {"ok": True, "op": op, "message": message}
    if data:
        payload["data"] = data
    if state:
        payload["state"] = state
    return _dump(payload)


def fail(
    op: str,
    code: str,
    message: str,
    hint: str = "",
    details: dict | None = None,
    state: dict | None = None,
) -> str:
    """Serialise a failure result.

    Args:
        code: Stable identifier, e.g. ``NO_ACTIVE_DOCUMENT``, ``FEATURE_FAILED``.
        message: What went wrong, in plain language.
        hint: The concrete next action that would fix it. Worth filling in --
            it is what lets an agent recover without a round-trip to the user.
        details: Structured diagnostics (attempted COM signatures, valid index
            ranges, available feature names).
    """
    error: dict[str, Any] = {"code": code, "message": message}
    if hint:
        error["hint"] = hint
    if details:
        error["details"] = details
    payload: dict[str, Any] = {"ok": False, "op": op, "message": message, "error": error}
    if state:
        payload["state"] = state
    return _dump(payload)


def from_exception(op: str, exc: BaseException) -> str:
    """Convert any exception into a failure envelope.

    :class:`ToolError` and ``SolidWorksError`` keep their codes and hints;
    anything else is reported as ``UNEXPECTED_ERROR`` with a traceback in the
    details, since an unclassified crash is a bug in this server and the
    traceback is what makes it fixable.
    """
    if isinstance(exc, ToolError):
        logger.info("%s failed: %s (%s)", op, exc.message, exc.code)
        return fail(op, exc.code, exc.message, exc.hint, exc.details)

    code = getattr(exc, "code", None)
    if code:
        logger.info("%s failed: %s (%s)", op, exc, code)
        return fail(op, str(code), str(exc), details=getattr(exc, "details", None))

    if isinstance(exc, TimeoutError):
        return fail(
            op,
            "TIMEOUT",
            str(exc),
            hint="Check the SolidWorks window for an open dialog, dismiss it, and retry.",
        )

    logger.error("%s crashed: %s\n%s", op, exc, traceback.format_exc())
    return fail(
        op,
        "UNEXPECTED_ERROR",
        f"{type(exc).__name__}: {exc}",
        hint="This is an internal error in the MCP server, not a SolidWorks refusal.",
        details={"traceback": traceback.format_exc(limit=12)},
    )


def _dump(payload: dict) -> str:
    # default=str so a stray COM object in a diagnostics dict degrades to its
    # repr instead of making the whole result unserialisable.
    return json.dumps(payload, indent=2, default=str, ensure_ascii=False)


def parse_json_arg(value, name: str, expect: type = list):
    """Accept a JSON-typed argument as either a real object or a JSON string.

    MCP clients vary in whether they send a nested array/object through as a
    parsed value or as a string, and the reference projects standardised on
    strings ("the JSON-string idiom") which forces every caller to double-encode.
    Accepting both removes a whole class of confusing failures.

    Raises:
        ToolError: If the value is missing, unparseable, or the wrong type.
    """
    if value is None:
        raise ToolError(
            "MISSING_PARAMETER",
            f"{name} is required.",
            hint=f"Pass {name} as a JSON {expect.__name__}.",
        )
    if isinstance(value, expect):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            raise ToolError("MISSING_PARAMETER", f"{name} is empty.")
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ToolError(
                "INVALID_PARAMETER",
                f"{name} is not valid JSON: {exc}",
                hint=f'Example: {name}=\'[{{"entity_type": "circle", "cx": 0, "cy": 0, "radius": 10}}]\'',
            ) from None
        if not isinstance(parsed, expect):
            raise ToolError(
                "INVALID_PARAMETER",
                f"{name} must be a JSON {expect.__name__}, got {type(parsed).__name__}.",
            )
        return parsed
    raise ToolError(
        "INVALID_PARAMETER",
        f"{name} must be a JSON {expect.__name__} (or a string containing one), "
        f"got {type(value).__name__}.",
    )


def require(value, name: str, hint: str = ""):
    """Assert a parameter was supplied, raising a consistent error if not."""
    if value is None:
        raise ToolError("MISSING_PARAMETER", f"{name} is required.", hint=hint)
    return value


def require_positive(value, name: str, hint: str = "") -> float:
    """Assert a numeric parameter is present and greater than zero."""
    require(value, name, hint)
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ToolError("INVALID_PARAMETER", f"{name} must be a number, got {value!r}.") from None
    if number <= 0:
        raise ToolError(
            "INVALID_PARAMETER",
            f"{name} must be greater than 0, got {number}.",
            hint=hint,
        )
    return number


def one_of(value, name: str, allowed) -> str:
    """Normalise and validate an enumerated string parameter."""
    if value is None:
        raise ToolError(
            "MISSING_PARAMETER",
            f"{name} is required.",
            hint=f"Choose one of: {', '.join(sorted(allowed))}.",
        )
    normalised = str(value).strip().lower()
    if normalised not in allowed:
        raise ToolError(
            "INVALID_PARAMETER",
            f"{name}={value!r} is not supported.",
            hint=f"Choose one of: {', '.join(sorted(allowed))}.",
        )
    return normalised
