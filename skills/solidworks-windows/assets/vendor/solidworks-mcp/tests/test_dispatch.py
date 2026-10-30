"""
Tests for the server's dispatch path.

All 41 tools route through the same ``@tool`` decorator -- connection check, STA
marshalling, result envelope -- so a defect there is a defect in every tool. These
tests exercise that path against a fake session, plus the real STA thread (which
needs no SolidWorks: it is a plain COM-apartment worker).
"""

from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

pytest.importorskip("pythoncom", reason="the COM thread needs pywin32")

from solidworks_mcp.com.connection import ComThread, SolidWorksError  # noqa: E402
from solidworks_mcp.util import result as R  # noqa: E402

# ---------------------------------------------------------------------------
# The STA marshalling thread
# ---------------------------------------------------------------------------

class TestComThread:
    @pytest.fixture
    def com(self):
        thread = ComThread(default_timeout=10.0)
        yield thread
        thread.shutdown()

    def test_runs_work_and_returns_the_result(self, com):
        assert com.call(lambda a, b: a + b, 2, 3) == 5

    def test_keyword_arguments_pass_through(self, com):
        assert com.call(lambda a, b=0: a * b, 3, b=4) == 12

    def test_exceptions_propagate_to_the_caller(self, com):
        with pytest.raises(ValueError, match="boom"):
            com.call(lambda: (_ for _ in ()).throw(ValueError("boom")))

    def test_every_call_lands_on_the_same_thread(self, com):
        # The whole point: COM objects are created and consumed on one apartment,
        # so they never need marshalling between threads.
        observed = {com.call(threading.get_ident) for _ in range(25)}
        assert len(observed) == 1

    def test_work_does_not_run_on_the_calling_thread(self, com):
        assert com.call(threading.get_ident) != threading.get_ident()

    def test_a_hung_call_times_out_rather_than_blocking_forever(self, com):
        # This is what a modal SolidWorks dialog looks like from here.
        blocked = threading.Event()
        with pytest.raises(TimeoutError) as excinfo:
            com.call(blocked.wait, timeout=0.4)
        assert "dialog" in str(excinfo.value).lower()
        blocked.set()

    def test_the_worker_survives_a_failed_call(self, com):
        with pytest.raises(ValueError):
            com.call(lambda: (_ for _ in ()).throw(ValueError("boom")))
        # A raised call must not poison the queue for later work.
        assert com.call(lambda: "still alive") == "still alive"

    def test_calls_are_serialised_in_submission_order(self, com):
        order: list[int] = []
        for index in range(10):
            com.call(order.append, index)
        assert order == list(range(10))


# ---------------------------------------------------------------------------
# The tool decorator
# ---------------------------------------------------------------------------

class FakeConfig:
    default_unit = "mm"
    call_timeout = 10.0
    log_file = ""
    log_level = "INFO"
    precision = 6
    max_entities = 2000
    rebuild_after_features = True
    allow_execute_python = True

    def numeric_log_level(self):
        return 20


class FakeSession:
    """A session stand-in: real STA thread, no SolidWorks."""

    def __init__(self, ensure_error: Exception | None = None):
        self.config = FakeConfig()
        self.com = ComThread(default_timeout=10.0)
        self.ensure_error = ensure_error
        self.ensure_calls = 0

    def ensure(self, launch=None):
        self.ensure_calls += 1
        if self.ensure_error:
            raise self.ensure_error
        return {"connected": True}

    def shutdown(self):
        self.com.shutdown()


def build_decorator(session, state=None):
    """Reproduce ``server.tool`` against a fake session.

    Mirrors the real decorator's structure so the behaviour under test is the
    behaviour that ships, without importing the module-level live session.
    """
    import functools

    def tool(operation: str, *, needs_connection: bool = True):
        def decorator(func):
            @functools.wraps(func)
            def wrapper(*args, **kwargs):
                try:
                    if needs_connection:
                        session.ensure()
                    message, data = session.com.call(lambda: func(*args, **kwargs))
                    return R.ok(operation, message, data, state)
                except BaseException as exc:  # noqa: BLE001
                    return R.from_exception(operation, exc)

            return wrapper

        return decorator

    return tool


class TestToolDispatch:
    @pytest.fixture
    def session(self):
        fake = FakeSession()
        yield fake
        fake.shutdown()

    def test_success_produces_a_complete_envelope(self, session):
        tool = build_decorator(session, state={"document": "Part1", "type": "PART"})

        @tool("demo_operation")
        def run():
            return "did the thing", {"feature": "Boss-Extrude1"}

        payload = json.loads(run())
        assert payload["ok"] is True
        assert payload["op"] == "demo_operation"
        assert payload["message"] == "did the thing"
        assert payload["data"]["feature"] == "Boss-Extrude1"
        assert payload["state"]["document"] == "Part1"

    def test_connection_is_ensured_before_the_work_runs(self, session):
        tool = build_decorator(session)

        @tool("demo")
        def run():
            return "ok", {}

        run()
        assert session.ensure_calls == 1

    def test_connection_check_is_skipped_when_declared_unnecessary(self, session):
        tool = build_decorator(session)

        @tool("demo", needs_connection=False)
        def run():
            return "ok", {}

        run()
        assert session.ensure_calls == 0

    def test_a_connection_failure_becomes_a_coded_envelope(self):
        session = FakeSession(
            ensure_error=SolidWorksError("NOT_INSTALLED", "no SolidWorks here")
        )
        try:
            tool = build_decorator(session)

            @tool("demo")
            def run():
                raise AssertionError("the tool body must not run when connect fails")

            payload = json.loads(run())
            assert payload["ok"] is False
            assert payload["error"]["code"] == "NOT_INSTALLED"
        finally:
            session.shutdown()

    def test_a_tool_error_keeps_its_code_and_hint(self, session):
        tool = build_decorator(session)

        @tool("demo")
        def run():
            raise R.ToolError(
                "FACE_NOT_FOUND", "index 9 out of range", hint="run analyze_model"
            )

        payload = json.loads(run())
        assert payload["error"]["code"] == "FACE_NOT_FOUND"
        assert payload["error"]["hint"] == "run analyze_model"

    def test_a_solidworks_error_keeps_its_code_and_details(self, session):
        tool = build_decorator(session)

        @tool("demo")
        def run():
            raise SolidWorksError(
                "FEATURE_FAILED", "returned null", {"attempts": "FeatureExtrusion3/23 -> null"}
            )

        payload = json.loads(run())
        assert payload["error"]["code"] == "FEATURE_FAILED"
        assert "FeatureExtrusion3" in payload["error"]["details"]["attempts"]

    def test_an_unexpected_crash_is_reported_not_raised(self, session):
        # A tool must never propagate an exception to the MCP transport: the client
        # would see a protocol error instead of an actionable message.
        tool = build_decorator(session)

        @tool("demo")
        def run():
            return 1 / 0

        payload = json.loads(run())
        assert payload["ok"] is False
        assert payload["error"]["code"] == "UNEXPECTED_ERROR"
        assert "ZeroDivisionError" in payload["message"]

    def test_arguments_reach_the_tool_body_through_the_com_thread(self, session):
        tool = build_decorator(session)

        @tool("demo")
        def run(depth, unit="mm"):
            return f"depth={depth}{unit}", {"depth": depth, "unit": unit}

        payload = json.loads(run(25, unit="in"))
        assert payload["data"] == {"depth": 25, "unit": "in"}

    def test_the_body_executes_on_the_com_thread(self, session):
        tool = build_decorator(session)
        caller = threading.get_ident()

        @tool("demo")
        def run():
            return "ok", {"thread": threading.get_ident()}

        payload = json.loads(run())
        assert payload["data"]["thread"] != caller

    def test_every_result_is_valid_json(self, session):
        # The envelope is the contract; an unserialisable value must degrade rather
        # than break the response.
        tool = build_decorator(session)

        class Opaque:
            def __repr__(self):
                return "<IFeature>"

        @tool("demo")
        def run():
            return "ok", {"handle": Opaque()}

        payload = json.loads(run())
        assert payload["data"]["handle"] == "<IFeature>"


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

class TestToolRegistration:
    """The tool surface itself: names, schemas, and documentation."""

    @pytest.fixture(scope="class")
    @classmethod
    def tools(cls):
        import asyncio

        from solidworks_mcp.server import mcp

        return asyncio.run(mcp.list_tools())

    @staticmethod
    def _describes_index_selection(description: str) -> bool:
        """Whether a description points the caller at index-based selection."""
        lowered = description.lower()
        return "index" in lowered or "indices" in lowered

    def test_the_expected_tools_are_registered(self, tools):
        names = {t.name for t in tools}
        expected = {
            # connection
            "connect_solidworks", "get_solidworks_info", "set_units",
            # documents
            "new_document", "open_document", "save_document", "close_document",
            "activate_document", "list_documents", "rebuild_document",
            "export_document", "set_configuration",
            # sketching
            "create_sketch", "edit_sketch", "exit_sketch", "add_sketch_entities",
            "add_sketch_constraint", "add_sketch_dimension",
            # features
            "extrude_feature", "add_edge_feature", "create_rib", "create_shell",
            "create_hole", "add_reference_geometry", "create_pattern",
            "set_material", "modify_dimension", "edit_feature",
            # sheet metal
            "sheet_metal_feature",
            # drawings
            "create_drawing_view", "annotate_drawing", "analyze_drawing",
            # assemblies
            "insert_component", "add_mate", "analyze_assembly", "save_body_as_part",
            # analysis
            "analyze_model", "get_selection", "compare_documents",
            # scripting
            "execute_python", "run_macro",
        }
        assert expected <= names, f"missing: {expected - names}"

    def test_no_redundant_draw_tools_survived_the_merge(self, tools):
        # The five single-call draw_* tools collapse into one batch
        # add_sketch_entities; separate extrude/cut tools into one extrude_feature.
        names = {t.name for t in tools}
        retired = {
            "draw_line", "draw_circle", "draw_rectangle", "draw_arc", "draw_polygon",
            "extrude_sketch", "cut_extrude", "fillet_edges", "chamfer_edges",
            "close_sketch", "get_sketch_status", "list_features", "get_document_info",
            "list_open_documents", "create_new_part", "create_new_assembly",
            "open_new_part", "open_new_assembly", "create_drawing",
            "add_drawing_view", "add_flat_pattern_view", "add_section_view",
            "add_drawing_dimension", "auto_dimension_drawing", "auto_center_marks",
            "add_hole_callout", "batch_export", "verify_state", "ensure_ready",
        }
        assert not (retired & names), f"redundant tools present: {retired & names}"

    def test_every_tool_documents_itself(self, tools):
        # The description is what an agent selects on, so an undocumented tool is an
        # unusable one.
        for entry in tools:
            assert entry.description, f"{entry.name} has no description"
            assert len(entry.description) > 60, f"{entry.name} description is too thin"

    def test_every_tool_exposes_an_object_schema(self, tools):
        for entry in tools:
            assert entry.inputSchema.get("type") == "object", entry.name

    def test_required_parameters_are_declared_where_they_matter(self, tools):
        by_name = {t.name: t for t in tools}
        expectations = {
            "open_document": {"file_path"},
            "modify_dimension": {"name", "value"},
            "add_sketch_entities": {"entities"},
            "add_mate": {"mate_type", "entity_a", "entity_b"},
            "compare_documents": {"document_a", "document_b"},
            "create_hole": {"face_index", "position", "diameter"},
        }
        for name, required in expectations.items():
            declared = set(by_name[name].inputSchema.get("required", []))
            assert required <= declared, f"{name} should require {required - declared}"

    def test_tools_that_should_work_with_no_arguments_do(self, tools):
        # A zero-argument default matters: `analyze_model()` must be a valid first
        # call, not an error about a missing analysis_type.
        by_name = {t.name: t for t in tools}
        for name in (
            "analyze_model", "analyze_assembly", "analyze_drawing", "get_selection",
            "list_documents", "new_document", "create_sketch", "extrude_feature",
            "exit_sketch", "connect_solidworks", "get_solidworks_info",
        ):
            assert not by_name[name].inputSchema.get("required"), (
                f"{name} should be callable with no arguments"
            )

    def test_index_based_selection_is_advertised_on_the_tools_that_need_it(self, tools):
        # The single most important usage rule; if the descriptions do not say it,
        # an agent will reach for coordinates and hit ambiguous picks.
        by_name = {t.name: t for t in tools}
        for name in ("create_sketch", "add_edge_feature", "sheet_metal_feature",
                     "add_mate", "analyze_model", "get_selection"):
            assert self._describes_index_selection(by_name[name].description), name
