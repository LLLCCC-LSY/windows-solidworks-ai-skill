"""
Version-compatibility layer for late-bound SolidWorks COM.

This module exists because the SolidWorks COM surface is *not* stable in two
distinct ways, and both reference implementations tripped over them:

1. **Property-vs-method ambiguity.** Under ``win32com`` dynamic dispatch,
   zero-argument accessors such as ``FirstFeature``, ``GetNextFeature``,
   ``GetTypeName2``, ``GetTitle`` and ``RevisionNumber`` resolve as *properties*
   on some SolidWorks builds and as *methods* on others. Call the wrong form and
   you get either a bound method object where you expected a string, or a
   ``TypeError``. :func:`pm` resolves both forms once, centrally.

2. **Signature churn across releases.** SolidWorks revs feature APIs by
   suffixing a number (``FeatureExtrusion2`` -> ``3``, ``FeatureCut3`` -> ``4``,
   ``AddMate3`` -> ``5``) *and* by appending parameters to existing methods
   between releases. A single hard-coded argument vector therefore only works on
   the exact release it was written against -- which is why the C# reference
   project (compiled against one interop assembly) has narrow version support.
   :func:`invoke_first` takes a list of candidate ``(method, args)`` attempts and
   returns the first one that actually produces a result, so one code path spans
   SolidWorks 2018-2026.

Everything here is pure ``pywin32``: no ``SolidWorks.Interop.*`` assemblies, no
generated type-library cache, no version-pinned references.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Sequence
from typing import Any

import pythoncom
import win32com.client

logger = logging.getLogger(__name__)

_MISSING = object()

#: Iteration ceiling for feature-tree walks, so a malformed/cyclic tree can
#: never hang the server.
WALK_LIMIT = 20000

#: Plain Python types a COM member can resolve to. A member that comes back as one
#: of these has already been fully resolved -- either it is a value property, or
#: win32com invoked a zero-argument method during attribute access (see
#: :func:`should_invoke`).
_PLAIN_TYPES = (str, bytes, bytearray, bool, int, float, complex, tuple, list, dict, set)

#: Type names that identify a wrapped or raw COM interface pointer.
_COM_TYPE_NAMES = frozenset(
    {"CDispatch", "Dispatch", "DispatchBaseClass", "PyIDispatch", "PyIUnknown"}
)


def is_com_object(value) -> bool:
    """Whether ``value`` is a COM interface pointer rather than a plain value.

    Needed because ``win32com``'s ``CDispatch`` implements ``__call__``, so a
    property that returns a COM object is indistinguishable from a method by
    ``callable()`` alone -- see :func:`should_invoke`.
    """
    if value is None or isinstance(value, _PLAIN_TYPES):
        return False
    try:
        if isinstance(value, win32com.client.CDispatch):
            return True
    except Exception:
        pass
    return type(value).__name__ in _COM_TYPE_NAMES


def should_invoke(value) -> bool:
    """Whether an attribute lookup produced something that still needs calling.

    This is the crux of late-bound SolidWorks access, and ``callable()`` on its own
    gets it wrong in *both* directions. Verified against SolidWorks 2023:

    ==============================  =========================  ==========  ==========
    Member shape                    ``getattr`` returns        ``callable``  Action
    ==============================  =========================  ==========  ==========
    method **with** arguments       ``<bound method ...>``     ``True``    **invoke**
    method with **no** arguments    the already-invoked value  ``False``   use as-is
    property returning an object    ``CDispatch``              ``True``    use as-is
    property returning nothing      ``None``                   ``False``   use as-is
    ==============================  =========================  ==========  ==========

    So a bare ``callable()`` test both *calls object-valued properties*
    (``ActiveDoc``, ``SketchManager``, ``FeatureManager``, ``Extension``,
    ``SelectionManager`` -- every one raises "Member not found") and *misreads
    already-invoked zero-argument methods* as unresolved properties.

    The reliable rule is: invoke only a genuine callable that is neither plain data
    nor a COM pointer.
    """
    if value is None or isinstance(value, _PLAIN_TYPES):
        return False
    if is_com_object(value):
        return False
    return callable(value)


# ---------------------------------------------------------------------------
# VARIANT helpers
# ---------------------------------------------------------------------------

def nothing():
    """An explicit ``VT_DISPATCH`` null.

    Many SolidWorks methods take optional ``IDispatch`` arguments (the
    ``Callout`` of ``SelectByID2``, the ``Configurations`` array of
    ``SetSuppression2``). Passing Python ``None`` works on some builds and
    raises ``com_error`` on others; a typed null VARIANT works everywhere.
    """
    return win32com.client.VARIANT(pythoncom.VT_DISPATCH, None)


def dispatch_array(items: Sequence[Any]):
    """A ``VT_ARRAY | VT_DISPATCH`` SAFEARRAY of COM objects.

    Needed by APIs that accept object arrays, e.g.
    ``InsertSheetMetalEdgeFlange2(Edges, Sketches, ...)``.
    """
    return win32com.client.VARIANT(
        pythoncom.VT_ARRAY | pythoncom.VT_DISPATCH, list(items)
    )


def double_array(values: Iterable[float]):
    """A ``VT_ARRAY | VT_R8`` SAFEARRAY of doubles (spline points, transforms)."""
    return win32com.client.VARIANT(
        pythoncom.VT_ARRAY | pythoncom.VT_R8, [float(v) for v in values]
    )


def byref_int(initial: int = 0):
    """A by-reference ``long`` out-parameter.

    Used for ``OpenDoc6(..., ref errors, ref warnings)``,
    ``AddMate5(..., out errorStatus)`` and friends. Read the result off
    ``.value`` after the call.
    """
    return win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, initial)


def byref_str(initial: str = ""):
    """A by-reference ``BSTR`` out-parameter (e.g. ``GetMaterialPropertyName2``)."""
    return win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_BSTR, initial)


def as_list(com_array) -> list:
    """Normalise whatever a SolidWorks accessor returned into a Python list.

    SolidWorks array returns arrive as a tuple, a list, ``None`` (empty), or --
    when a single element is returned from a SAFEARRAY-typed property -- a bare
    object. All four shapes appear in practice across releases.
    """
    if com_array is None:
        return []
    if isinstance(com_array, (list, tuple)):
        return [item for item in com_array if item is not None]
    return [com_array]


def as_floats(com_array) -> list[float]:
    """Coerce a numeric COM array to a list of floats, dropping non-numerics."""
    out = []
    for item in as_list(com_array):
        try:
            out.append(float(item))
        except (TypeError, ValueError):
            continue
    return out


# ---------------------------------------------------------------------------
# Property-vs-method resolution
# ---------------------------------------------------------------------------

def pm(obj, name: str, *args, default=_MISSING):
    """Read ``obj.name``, tolerating property-or-method binding.

    ``pm(doc, "GetTitle")`` works whether this SolidWorks build exposes
    ``GetTitle`` as a property or as a zero-argument method. If ``args`` are
    supplied the accessor is only sensible as a method, but the property form is
    still attempted first for accessors that SolidWorks models as parameterised
    properties.

    Args:
        obj: Any COM object.
        name: Accessor name.
        *args: Arguments to pass if the accessor turns out to be a method.
        default: Returned instead of raising when the accessor is absent or
            fails. Omit it to let errors propagate.

    Returns:
        The accessor's value, or ``default``.
    """
    try:
        attr = getattr(obj, name)
    except Exception:
        if default is not _MISSING:
            return default
        raise

    if should_invoke(attr):
        try:
            return attr(*args)
        except Exception:
            if default is not _MISSING:
                return default
            raise

    # Already fully resolved: an object-valued property (``CDispatch``), a value
    # property, or a zero-argument method win32com invoked during the attribute
    # lookup. Return it rather than calling it -- calling a ``CDispatch`` raises
    # "Member not found", which is what previously made every ``ActiveDoc`` /
    # ``SketchManager`` / ``FeatureManager`` read come back as ``None``.
    # Arguments supplied but the member resolved to a bare value means this is not
    # the member the caller meant, so prefer their default over a misleading value.
    if args and not is_com_object(attr) and default is not _MISSING:
        return default
    return attr


def sketch_constraint_status(status):
    """Decode swConstrainedStatus_e, verified against the installed swconst.tlb."""
    try:
        return {
            1: "unknown", 2: "under_defined", 3: "fully_defined",
            4: "over_defined", 5: "no_solution", 6: "invalid_solution",
            7: "autosolve_off",
        }.get(int(status))
    except (TypeError, ValueError):
        return None


def has_member(obj, name: str) -> bool:
    """Whether ``obj`` exposes ``name`` at all -- the capability-probe primitive.

    For COM objects, resolve the member's DISPID without invoking it. Plain
    Python objects retain normal attribute lookup, as used by offline tests.
    """
    # Looking up an attribute on late-bound COM can invoke a zero-argument
    # method (including CloseAllDocuments). Resolve its DISPID without Invoke.
    try:
        dispatch = object.__getattribute__(obj, "_oleobj_")
    except (AttributeError, TypeError):
        dispatch = None
    if dispatch is not None:
        try:
            dispatch.GetIDsOfNames(name)
            return True
        except Exception:
            return False
    try:
        getattr(obj, name)
        return True
    except Exception:
        return False


def first_member(obj, names: Iterable[str]) -> str | None:
    """The first name in ``names`` that ``obj`` actually exposes."""
    for name in names:
        if has_member(obj, name):
            return name
    return None


# ---------------------------------------------------------------------------
# Arity- and revision-tolerant invocation
# ---------------------------------------------------------------------------

class InvocationReport:
    """Outcome of an :func:`invoke_first` attempt sequence.

    Attributes:
        value: The successful call's return value (``None`` if all failed).
        signature: ``"<method>/<argcount>"`` of the winning attempt, or ``None``.
        attempts: ``[(signature, outcome), ...]`` for every attempt made --
            surfaced in tool errors so a failure on an unusual SolidWorks build
            says exactly which signatures were tried and how each failed.
    """

    __slots__ = ("value", "signature", "attempts", "succeeded")

    def __init__(self):
        self.value = None
        self.signature: str | None = None
        self.attempts: list[tuple[str, str]] = []
        # Tracked explicitly rather than derived from ``value``: void-returning
        # APIs (``InsertRib``, ``EditRebuild3``) succeed *with* a ``None`` value,
        # so `value is not None` would report every such call as a failure.
        self.succeeded = False

    @property
    def ok(self) -> bool:
        return self.succeeded

    def diagnostic(self) -> str:
        return "; ".join(f"{sig} -> {outcome}" for sig, outcome in self.attempts)


def invoke_first(
    obj,
    candidates: Sequence[tuple[str, Sequence[Any]]],
    *,
    accept: Callable[[Any], bool] | None = None,
) -> InvocationReport:
    """Try each ``(method_name, args)`` candidate until one succeeds.

    This is the mechanism that buys wide SolidWorks version support. Order
    candidates newest-API-first; older revisions simply do not expose the newer
    method and fall through, and a release that appended a parameter is covered
    by listing both argument vectors for the same method name.

    A candidate "succeeds" when the method exists, does not raise, and returns a
    value ``accept`` approves of. The default ``accept`` rejects only ``None``,
    matching the SolidWorks convention of returning a null ``IFeature`` on
    failure rather than raising.

    Args:
        obj: The COM object to invoke on (e.g. ``doc.FeatureManager``).
        candidates: Ordered attempts, e.g.
            ``[("FeatureExtrusion3", args_v3), ("FeatureExtrusion2", args_v2)]``.
        accept: Optional predicate deciding whether a return value counts as
            success. Pass ``lambda r: r is not None`` semantics explicitly when
            a falsy-but-valid value (``0``, ``False``, ``""``) is a real result.

    Returns:
        An :class:`InvocationReport`. Check ``.ok``; on failure ``.diagnostic()``
        explains every signature that was tried.
    """
    if accept is None:
        def accept(result):  # noqa: E306 - tiny local default
            return result is not None

    report = InvocationReport()

    for method_name, args in candidates:
        signature = f"{method_name}/{len(args)}"
        try:
            method = getattr(obj, method_name)
        except Exception:
            report.attempts.append((signature, "not exposed by this SolidWorks build"))
            continue

        if should_invoke(method):
            try:
                result = method(*args)
            except Exception as exc:  # com_error, TypeError on arity mismatch, ...
                report.attempts.append((signature, f"raised {type(exc).__name__}: {_short(exc)}"))
                continue
        elif is_com_object(method):
            # The member is an object-valued property, not a method. Calling it
            # would raise "Member not found"; the object itself is the result.
            result = method
        elif not args:
            # win32com invokes zero-argument members during attribute access, so the
            # call has ALREADY happened and this value is its return. Treating it as
            # "resolved as a property" would report a successful EditRebuild3 /
            # EditDelete / GraphicsRedraw2 as a failure.
            result = method
        else:
            report.attempts.append((
                signature,
                f"resolved to a bare {type(method).__name__} instead of a callable, so the "
                f"{len(args)} supplied argument(s) could not be passed",
            ))
            continue

        if accept(result):
            report.value = result
            report.signature = signature
            report.succeeded = True
            report.attempts.append((signature, "ok"))
            logger.debug("invoke_first: %s succeeded", signature)
            return report

        report.attempts.append((signature, "returned null/rejected"))

    logger.debug("invoke_first: all candidates failed (%s)", report.diagnostic())
    return report


def invoke_bool(obj, candidates: Sequence[tuple[str, Sequence[Any]]]) -> InvocationReport:
    """:func:`invoke_first` for APIs whose success value is ``True``.

    ``False`` is a genuine failure signal for these (``InsertAxis2``,
    ``SetSuppression2``, ``AutoInsertCenterMarks2``), so it must not be accepted
    the way the default predicate would accept any non-``None`` value.
    """
    return invoke_first(obj, candidates, accept=lambda r: bool(r))


def invoke_any(obj, candidates: Sequence[tuple[str, Sequence[Any]]]) -> InvocationReport:
    """:func:`invoke_first` for APIs that legitimately return ``None``/void.

    Success here means "the call was dispatched without raising". Callers must
    verify the effect themselves -- e.g. ``InsertRib`` returns void, so
    features.py snapshots the last tree feature name before and after.
    """
    return invoke_first(obj, candidates, accept=lambda r: True)


def _short(exc: Exception, limit: int = 160) -> str:
    text = str(exc).replace("\n", " ").strip()
    return text[:limit] + ("..." if len(text) > limit else "")


# ---------------------------------------------------------------------------
# Feature-tree traversal
# ---------------------------------------------------------------------------

def iter_features(doc):
    """Yield every top-level feature of ``doc`` in feature-tree order.

    Uses :func:`pm` for both the head (``FirstFeature``) and the link
    (``GetNextFeature``) because either can bind as a property. Bounded by
    :data:`WALK_LIMIT`.
    """
    feature = pm(doc, "FirstFeature", default=None)
    seen = 0
    while feature is not None and seen < WALK_LIMIT:
        yield feature
        seen += 1
        feature = pm(feature, "GetNextFeature", default=None)


def iter_sub_features(feature):
    """Yield a feature's sub-features (mates in a MateGroup, bends in a flange)."""
    sub = pm(feature, "GetFirstSubFeature", default=None)
    seen = 0
    while sub is not None and seen < WALK_LIMIT:
        yield sub
        seen += 1
        sub = pm(sub, "GetNextSubFeature", default=None)


def feature_name(feature, default: str = "<unnamed>") -> str:
    """A feature's tree name, never raising."""
    name = pm(feature, "Name", default=None)
    return str(name) if name else default


#: Tree-node types that are UI containers rather than modelling features. Every
#: SolidWorks part tree begins with a dozen of these (Comments, Favorites, History,
#: Selection Sets, Sensors, Design Binder, Annotations, Surface Bodies, Solid Bodies,
#: Lights/Cameras/Scene, Markups, Equations, Material), so reporting them as features
#: buries the real tree in noise. All bar one end in "Folder".
_TREE_CONTAINER_TYPES = frozenset({"DetailCabinet", "OriginProfileFeature"})


def is_tree_container(type_name: str) -> bool:
    """Whether a tree-node type is a UI container rather than a real feature."""
    return type_name.endswith("Folder") or type_name in _TREE_CONTAINER_TYPES


def feature_type(feature, default: str = "<unknown>") -> str:
    """A feature's type token (``ProfileFeature``, ``Extrusion``, ``FlatPattern``).

    Prefers ``GetTypeName2`` and falls back to the pre-2012 ``GetTypeName``.
    """
    for accessor in ("GetTypeName2", "GetTypeName"):
        value = pm(feature, accessor, default=None)
        if value:
            return str(value)
    return default


def find_feature(doc, name: str, collect: list[str] | None = None):
    """Locate a feature by exact tree name.

    When ``collect`` is supplied the whole tree is walked and every name is
    appended to it, so a not-found error can list the available features instead
    of just failing. Without it, the walk stops at the first match.
    """
    found = None
    for feature in iter_features(doc):
        current = feature_name(feature)
        if collect is not None:
            collect.append(current)
        if found is None and current == name:
            found = feature
            if collect is None:
                break
    return found


def find_feature_by_type(doc, type_name: str):
    """The first feature whose type token equals ``type_name`` (e.g. ``FlatPattern``)."""
    for feature in iter_features(doc):
        if feature_type(feature) == type_name:
            return feature
    return None


def last_feature_name(doc) -> str | None:
    """The name of the last top-level feature -- the void-API verification anchor.

    ``InsertRib`` and ``InsertAxis2`` return void/bool with no feature handle, so
    creation is confirmed by comparing this before and after the call.
    """
    last = None
    for feature in iter_features(doc):
        last = feature
    return feature_name(last) if last is not None else None


def feature_names(doc) -> list[str]:
    """Every top-level feature name, in tree order."""
    return [feature_name(feature) for feature in iter_features(doc)]


def error_count(doc) -> int | None:
    """How many features are currently in error, or ``None`` if unavailable.

    ``IModelDoc2.GetErrorCount`` is **not exposed** on SolidWorks 2023, so reading it
    there silently reported ``None`` and every "did that edit break anything?" check
    came back empty. ``IModelDocExtension.GetWhatsWrongCount`` is the accessor that
    works; the older names stay as fallbacks.
    """
    extension = pm(doc, "Extension", default=None)
    for owner, name in (
        (extension, "GetWhatsWrongCount"),
        (doc, "GetErrorCount"),
        (pm(doc, "FeatureManager", default=None), "GetErrorCount"),
    ):
        if owner is None:
            continue
        value = pm(owner, name, default=None)
        if value is not None and not is_com_object(value):
            try:
                return int(value)
            except (TypeError, ValueError):
                continue
    return None


def needs_rebuild(doc) -> bool | None:
    """Whether the document has pending rebuild work, or ``None`` if unavailable."""
    extension = pm(doc, "Extension", default=None)
    value = pm(extension, "NeedsRebuild2", default=None)
    if value is None or is_com_object(value):
        return None
    try:
        return bool(int(value))
    except (TypeError, ValueError):
        return None


def iter_display_dimensions(owner):
    """Yield the display dimensions of a feature or drawing view.

    Two enumeration APIs exist and availability varies by build. On SolidWorks 2023
    ``IFeature.GetDisplayDimensions`` is **not exposed at all**, so relying on it
    alone made every feature report zero dimensions -- which silently broke the
    discovery half of ``modify_dimension``, since the caller needs the full names it
    reports. The linked-list form (``GetFirstDisplayDimension`` plus ``GetNext*``) is
    the one that works there, so it is tried first.

    Args:
        owner: An ``IFeature`` or ``IView``.

    Yields:
        ``IDisplayDimension`` objects.
    """
    first = pm(owner, "GetFirstDisplayDimension", default=None)
    if first is not None:
        # Feature enumeration may continue into absorbed sketch dimensions.
        # Keep each dimension under its actual owning feature.
        owner_name = pm(owner, "Name", default=None) if pm(owner, "GetTypeName2", default=None) else None
        current = first
        seen = 0
        while current is not None and seen < WALK_LIMIT:
            dimension = read_dimension(current) if owner_name else None
            full_name = str(pm(dimension, "FullName", default="")) if dimension is not None else ""
            parts = full_name.split("@")
            if not owner_name or len(parts) < 2 or parts[1] == str(owner_name):
                yield current
            seen += 1
            # The "next" accessor is versioned; all three return None at the end of
            # the chain, so trying them in order is safe.
            following = pm(owner, "GetNextDisplayDimension", current, default=None)
            if following is None:
                for accessor in ("GetNext3", "GetNext2", "GetNext"):
                    following = pm(current, accessor, default=None)
                    if following is not None:
                        break
            current = following
        return

    # Array form, for builds that expose it.
    yield from as_list(pm(owner, "GetDisplayDimensions", default=None))


def read_dimension(display_dimension):
    """Unwrap an ``IDisplayDimension`` to its underlying ``IDimension``."""
    return (
        pm(display_dimension, "GetDimension2", 0, default=None)
        or pm(display_dimension, "GetDimension", default=None)
    )


def dimension_value(dimension, default=None):
    """A dimension's SI value, preferring the configuration-aware reader."""
    values = as_floats(pm(dimension, "GetSystemValue3", 1, nothing(), default=None))
    if values:
        return values[0]
    value = pm(dimension, "SystemValue", default=None)
    return default if value is None else value
