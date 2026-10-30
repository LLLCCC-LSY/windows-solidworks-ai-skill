"""
SolidWorks connection management on a dedicated STA thread.

**Why the STA thread matters.** SolidWorks is an apartment-threaded (STA) COM
server. When Python calls it from an MTA thread -- which is what
``pythoncom.CoInitialize()`` on an arbitrary worker thread plus
``win32com.client.Dispatch`` gives you -- every call is marshalled through a
proxy. Simple property reads survive that, which is why a basic
extrude-and-fillet server appears to work. The APIs that do *not* survive it are
exactly the advanced ones: ``IFeatureManager.CreateDefinition`` /
``CreateFeature`` (sheet-metal base flange, shell), ``AccessSelections`` /
``ModifyDefinition`` (edge-flange length, sketched-bend flip), and selection
state that must persist across several calls. Those return null from an MTA
caller with no error, which reads as "the API is broken" rather than "the
apartment is wrong".

So this module owns one long-lived thread initialised with
``COINIT_APARTMENTTHREADED``, and every COM touch in the whole server is
marshalled onto it via :meth:`ComThread.call`. That single design choice is what
lets a pure-Python implementation reach the feature set the C# reference project
needed a compiled interop service for.

The thread is also the natural place to enforce a call timeout, so a modal
SolidWorks dialog blocks one tool call instead of wedging the server.
"""

from __future__ import annotations

import concurrent.futures
import logging
import os
import queue
import subprocess
import threading
import time
from collections.abc import Callable
from typing import Any

import pythoncom
import win32com.client

from . import compat, enums
from . import constants as swconst
from .finder import find_solidworks_exe, find_template, installed_versions

logger = logging.getLogger(__name__)


class SolidWorksError(RuntimeError):
    """A SolidWorks-level failure with a stable machine-readable code."""

    def __init__(self, code: str, message: str, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


# ---------------------------------------------------------------------------
# STA marshalling thread
# ---------------------------------------------------------------------------

class ComThread:
    """A single apartment-threaded worker that owns every COM interaction.

    All SolidWorks calls are funnelled through :meth:`call`, so COM objects are
    created and consumed on one apartment for the process lifetime. Objects
    never cross threads, which removes both the marshalling failures described
    in the module docstring and any need for ``CoMarshalInterThreadInterface``
    plumbing.
    """

    def __init__(self, default_timeout: float = 300.0):
        self._queue: queue.Queue[tuple | None] = queue.Queue()
        self._default_timeout = default_timeout
        self._ready = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name="solidworks-com-sta", daemon=True
        )
        self._thread.start()
        self._ready.wait(timeout=10.0)

    def _run(self) -> None:
        try:
            pythoncom.CoInitializeEx(pythoncom.COINIT_APARTMENTTHREADED)
        except Exception:  # already initialised on this thread
            pythoncom.CoInitialize()
        self._ready.set()
        logger.info("COM STA worker thread started")
        try:
            while True:
                item = self._queue.get()
                if item is None:
                    break
                func, args, kwargs, future = item
                if future.set_running_or_notify_cancel():
                    try:
                        future.set_result(func(*args, **kwargs))
                    except BaseException as exc:  # noqa: BLE001 - relayed to caller
                        future.set_exception(exc)
        finally:
            pythoncom.CoUninitialize()
            logger.info("COM STA worker thread stopped")

    def call(self, func: Callable, *args, timeout: float | None = None, **kwargs) -> Any:
        """Run ``func`` on the STA thread and return its result.

        Raises:
            TimeoutError: If the call outlives ``timeout``. The usual cause is a
                modal SolidWorks dialog; the worker stays alive so later calls
                can still succeed once the dialog is dismissed.
        """
        future: concurrent.futures.Future = concurrent.futures.Future()
        self._queue.put((func, args, kwargs, future))
        try:
            return future.result(timeout=timeout or self._default_timeout)
        except concurrent.futures.TimeoutError as exc:
            raise TimeoutError(
                f"SolidWorks did not respond within {timeout or self._default_timeout:.0f}s. "
                "A modal dialog is most likely open in the SolidWorks window -- dismiss it and retry."
            ) from exc

    def shutdown(self) -> None:
        self._queue.put(None)


# ---------------------------------------------------------------------------
# The SolidWorks session
# ---------------------------------------------------------------------------

class SolidWorksSession:
    """Owns the ``SldWorks.Application`` handle and the STA thread it lives on.

    A single instance is shared by every tool. All public methods are safe to
    call from the MCP server's worker threads; they marshal internally.
    """

    def __init__(self, config):
        self.config = config
        self.com = ComThread(default_timeout=config.call_timeout)
        self._app = None
        self._revision: str | None = None
        self._year: int | None = None
        self._exe_path: str | None = None
        self._capabilities: dict[str, bool] | None = None
        self._lock = threading.RLock()

    # -- lifecycle ---------------------------------------------------------

    @property
    def app(self):
        """The live ``ISldWorks`` handle, or ``None`` if not connected."""
        return self._app

    @property
    def revision(self) -> str | None:
        return self._revision

    @property
    def year(self) -> int | None:
        """Marketing year (e.g. 2025), derived from the COM revision number."""
        return self._year

    def is_alive(self) -> bool:
        """Whether the cached handle still points at a responsive SolidWorks."""
        if self._app is None:
            return False
        return bool(self.com.call(self._probe_alive, timeout=30.0))

    def _probe_alive(self) -> bool:
        try:
            return compat.pm(self._app, "RevisionNumber", default=None) is not None
        except Exception:
            self._app = None
            return False

    def ensure(self, launch: bool | None = None) -> dict:
        """Guarantee a usable connection, launching SolidWorks if necessary.

        Idempotent and cheap once connected -- every tool calls it first, so a
        user never has to remember to call ``connect_solidworks`` by hand.

        Args:
            launch: Whether to start SolidWorks when no instance is running.
                Defaults to the configured ``auto_launch``.

        Returns:
            A dict describing the connection (``connected``, ``revision``,
            ``version_year``, ``launched``, ``active_document``).

        Raises:
            SolidWorksError: If no connection could be established.
        """
        with self._lock:
            if self._app is not None and self.is_alive():
                return self._describe(launched=False)

            if self.com.call(self._attach):
                return self._describe(launched=False)

            # A tool argument must not override the deployment's launch policy.
            should_launch = self.config.auto_launch and launch is not False
            if not should_launch:
                raise SolidWorksError(
                    "NOT_RUNNING",
                    "SolidWorks is not running and auto-launch is disabled. "
                    "Start SolidWorks manually, or enable auto_launch.",
                )

            self._launch()
            deadline = time.time() + self.config.startup_timeout
            while time.time() < deadline:
                time.sleep(self.config.connect_retry_interval)
                if self.com.call(self._attach):
                    return self._describe(launched=True)

            raise SolidWorksError(
                "CONNECT_TIMEOUT",
                f"SolidWorks did not become available within "
                f"{self.config.startup_timeout:.0f}s. It may still be loading, or a "
                f"startup dialog (license, welcome screen) may be waiting for input.",
            )

    def _attach(self) -> bool:
        """Attach to a running or newly-created SolidWorks. Runs on the STA thread.

        Only use the running-object table here. GetObject(Class=...) and
        Dispatch(prog_id) may create a process, bypassing auto_launch=False.
        Explicit launching is handled by ensure(), subject to config policy.
        """
        strategies = (
            ("GetActiveObject", lambda: win32com.client.GetActiveObject("SldWorks.Application")),
        )

        for label, strategy in strategies:
            try:
                app = strategy()
            except Exception as exc:
                logger.debug("attach via %s failed: %s", label, exc)
                continue
            if app is None:
                continue

            revision = compat.pm(app, "RevisionNumber", default=None)
            if revision is None:
                logger.debug("attach via %s produced an unresponsive handle", label)
                continue

            self._app = app
            self._revision = str(revision)
            self._year = enums.year_for_revision(self._revision)
            self._capabilities = None

            # Replace the hand-maintained enum literals with the values this install
            # actually reports, before any tool can act on a wrong one. Cheap and
            # idempotent after the first call.
            swconst.sync_enums()

            # Keep the window visible: an invisible SolidWorks that raises a
            # modal dialog is unrecoverable for the user, since they cannot see
            # or dismiss it.
            if self.config.visible:
                try:
                    app.Visible = True
                except Exception:
                    pass

            # Suppress the "enter dimension value" popup: without it, every
            # AddDimension2 blocks on a modal input box that no agent can dismiss.
            #
            # This mutates a user preference, so the toggle index is only used when
            # it was VERIFIED against the install's own type library. Acting on an
            # unverified guess could flip an unrelated setting, which is worse than
            # leaving the popup enabled -- a blocked dimension call surfaces as a
            # clear timeout, whereas a silently changed preference does not.
            toggle, confirmed = swconst.verified(
                "swInputDimValOnCreate", enums.PREF_INPUT_DIM_VAL_ON_CREATE
            )
            if confirmed and self.config.configure_dimension_input:
                try:
                    app.SetUserPreferenceToggle(toggle, False)
                except Exception:
                    pass
            elif self.config.configure_dimension_input:
                logger.warning(
                    "Could not verify the swInputDimValOnCreate toggle index against this "
                    "install, so it was left untouched. If add_sketch_dimension times out, a "
                    "modal 'Modify' dialog is waiting in the SolidWorks window."
                )

            logger.info(
                "Connected to SolidWorks %s (%s) via %s",
                self._revision, self._year or "unknown year", label,
            )
            return True

        return False

    def _launch(self) -> None:
        """Start the SolidWorks executable (does not wait for COM readiness)."""
        exe = self._exe_path or self.config.exe_path or find_solidworks_exe()
        if not exe or not os.path.exists(exe):
            raise SolidWorksError(
                "NOT_INSTALLED",
                "Could not locate SLDWORKS.exe. Set exe_path in the server config "
                "(or the SOLIDWORKS_EXE environment variable) to the full path.",
                {"searched_versions": installed_versions()},
            )
        self._exe_path = exe
        logger.info("Launching SolidWorks: %s", exe)
        # DETACHED_PROCESS so SolidWorks outlives the MCP server; the user's
        # session should not die when the agent disconnects.
        subprocess.Popen(  # noqa: S603 - path resolved from registry/config
            [exe],
            creationflags=getattr(subprocess, "DETACHED_PROCESS", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
            close_fds=True,
        )

    def _describe(self, launched: bool) -> dict:
        title = self.com.call(self._active_title, timeout=30.0)
        return {
            "connected": True,
            "revision": self._revision,
            "version_year": self._year,
            "launched": launched,
            "active_document": title,
            "version_support": self.version_support(),
        }

    def version_support(self) -> dict:
        """Whether this SolidWorks install is one this server can trust fully.

        Combines the recognized marketing year, whether ``swconst.tlb`` verified
        this install's own enum values, and whether this exact year has been
        directly tested -- see :func:`enums.version_support` for the decision
        logic (kept pure and separate so it is unit-testable without COM).
        """
        return enums.version_support(self._year, self._revision, bool(swconst.table()))

    def _active_title(self) -> str | None:
        doc = compat.pm(self._app, "ActiveDoc", default=None)
        if doc is None:
            return None
        return compat.pm(doc, "GetTitle", default=None)

    def disconnect(self) -> None:
        """Release the handle without closing SolidWorks."""
        with self._lock:
            self._app = None
            self._revision = None
            self._year = None
            self._capabilities = None

    def shutdown(self) -> None:
        self.disconnect()
        self.com.shutdown()

    # -- document access ---------------------------------------------------

    def active_doc(self):
        """The active ``IModelDoc2``. **Call only from the STA thread.**

        Raises:
            SolidWorksError: If no document is open.
        """
        doc = compat.pm(self._app, "ActiveDoc", default=None)
        if doc is None:
            raise SolidWorksError(
                "NO_ACTIVE_DOCUMENT",
                "No document is open in SolidWorks. Create one with new_document "
                "or open one with open_document first.",
            )
        return doc

    def doc_type(self, doc) -> str:
        """``PART`` | ``ASSEMBLY`` | ``DRAWING`` for ``doc``.

        Uses ``GetType`` (the ``swDocumentTypes_e`` integer) rather than
        interface casting, which pure-Python late binding cannot do.
        """
        raw = compat.pm(doc, "GetType", default=None)
        try:
            code = int(raw)
        except (TypeError, ValueError):
            return "UNKNOWN"
        return {
            enums.DOC_PART: "PART",
            enums.DOC_ASSEMBLY: "ASSEMBLY",
            enums.DOC_DRAWING: "DRAWING",
        }.get(code, "UNKNOWN")

    def require_doc(self, *kinds: str):
        """The active document, asserted to be one of ``kinds``.

        Args:
            *kinds: Accepted document types, e.g. ``"PART"``, ``"ASSEMBLY"``.

        Raises:
            SolidWorksError: If nothing is open, or the type does not match.
        """
        doc = self.active_doc()
        if not kinds:
            return doc
        actual = self.doc_type(doc)
        if actual not in kinds:
            raise SolidWorksError(
                "WRONG_DOCUMENT_TYPE",
                f"This operation needs {' or '.join(kinds).lower()} document to be active, "
                f"but the active document is a {actual.lower()}.",
                {"active_type": actual, "required": list(kinds)},
            )
        return doc

    # -- templates ---------------------------------------------------------

    def template_for(self, kind: str, explicit: str | None = None) -> str:
        """Resolve a document template path for ``kind``.

        Order: an explicit path, then the user's configured default template,
        then a filesystem search. The search fallback is what lets the server
        work on installs where Tools > Options > Default Templates was never set
        -- a common failure mode for the reference projects.

        Args:
            kind: ``"part"``, ``"assembly"`` or ``"drawing"``.
            explicit: A caller-supplied template path, used verbatim if it exists.

        Raises:
            SolidWorksError: If no usable template could be found.
        """
        if explicit and os.path.exists(explicit):
            return explicit

        pref = {
            "part": enums.PREF_DEFAULT_TEMPLATE_PART,
            "assembly": enums.PREF_DEFAULT_TEMPLATE_ASSEMBLY,
            "drawing": enums.PREF_DEFAULT_TEMPLATE_DRAWING,
        }[kind]
        try:
            configured = self._app.GetUserPreferenceStringValue(pref)
        except Exception:
            configured = None
        if configured and os.path.exists(configured):
            return configured

        found = find_template(kind)
        if found:
            return found

        ext = {"part": ".prtdot", "assembly": ".asmdot", "drawing": ".drwdot"}[kind]
        raise SolidWorksError(
            "TEMPLATE_NOT_FOUND",
            f"No {kind} template ({ext}) could be found. Set one in SolidWorks under "
            f"Tools > Options > Default Templates, or pass template_path explicitly.",
        )

    # -- capability probing ------------------------------------------------

    def capabilities(self) -> dict[str, bool]:
        """Which optional API generations this SolidWorks build exposes.

        Probed from the live COM object rather than inferred from the version
        number, because availability also depends on installed add-ins (sheet metal
        and the 3D Interconnect translators in particular).

        The document-level probes need an open document to read a ``FeatureManager``
        from, so a probe taken with nothing open is **not** cached -- otherwise the
        first call (typically ``connect_solidworks``, before any document exists)
        would freeze a 4-entry result and the feature APIs would never be reported.
        """
        if self._capabilities is not None:
            return self._capabilities
        probed = self.com.call(self._probe_capabilities, timeout=60.0)
        if probed.get("_document_scope"):
            self._capabilities = probed
        return probed

    def _probe_capabilities(self) -> dict[str, bool]:
        app = self._app
        caps = {
            "app_load_file4": compat.has_member(app, "LoadFile4"),
            "app_activate_doc3": compat.has_member(app, "ActivateDoc3"),
            "app_close_all_documents": compat.has_member(app, "CloseAllDocuments"),
            "app_export_file_data": compat.has_member(app, "GetExportFileData"),
        }

        # Marks whether the document-scoped probes below actually ran, so
        # capabilities() knows if the result is complete enough to cache.
        caps["_document_scope"] = False

        doc = compat.pm(app, "ActiveDoc", default=None)
        if doc is not None:
            fm = compat.pm(doc, "FeatureManager", default=None)
            if fm is not None:
                caps["_document_scope"] = True
                caps.update(
                    {
                        "feature_extrusion3": compat.has_member(fm, "FeatureExtrusion3"),
                        "feature_cut4": compat.has_member(fm, "FeatureCut4"),
                        "feature_revolve2": compat.has_member(fm, "FeatureRevolve2"),
                        "linear_pattern3": compat.has_member(fm, "FeatureLinearPattern3"),
                        "circular_pattern4": compat.has_member(fm, "FeatureCircularPattern4"),
                        "mirror_feature2": compat.has_member(fm, "InsertMirrorFeature2"),
                        "create_definition": compat.has_member(fm, "CreateDefinition"),
                        "sheet_metal_edge_flange2": compat.has_member(
                            fm, "InsertSheetMetalEdgeFlange2"
                        ),
                        "sheet_metal_3d_bend": compat.has_member(fm, "InsertSheetMetal3dBend"),
                        "insert_rib": compat.has_member(fm, "InsertRib"),
                        "swept_protrusion4": compat.has_member(fm, "InsertProtrusionSwept4"),
                        "loft_protrusion2": compat.has_member(fm, "InsertProtrusionBlend2"),
                        "simple_hole": compat.has_member(fm, "SimpleHole2"),
                        "hole_wizard5": compat.has_member(fm, "HoleWizard5"),
                    }
                )
        else:
            caps["_note"] = (
                "Feature-level APIs were not probed: they need an open document to read a "
                "FeatureManager from. Open a document and call get_solidworks_info again."
            )
        return caps

    def info(self) -> dict:
        """Everything known about the installation and the live connection."""
        payload = {
            "installed_versions": installed_versions(),
            "executable": self._exe_path or self.config.exe_path or find_solidworks_exe(),
            "connected": self._app is not None,
        }
        if self._app is not None:
            payload.update(
                {
                    "revision": self._revision,
                    "version_year": self._year,
                    "capabilities": self.capabilities(),
                    "version_support": self.version_support(),
                }
            )
        return payload
