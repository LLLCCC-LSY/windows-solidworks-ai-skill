"""
Offline tests -- no SolidWorks, and no Windows, required.

These cover the layers that can be tested without CAD: unit conversion, the result
envelope, argument coercion, the enum tables, and the compatibility layer's
dispatch logic (exercised against fake COM objects that reproduce the real
failure modes -- a property where a method was expected, an arity mismatch, a null
return).

The COM-touching layers are covered by ``test_live.py``, which is skipped unless
SolidWorks is present.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from solidworks_mcp.com import enums  # noqa: E402
from solidworks_mcp.util import result as R  # noqa: E402
from solidworks_mcp.util import units  # noqa: E402

# ---------------------------------------------------------------------------
# Units
# ---------------------------------------------------------------------------

class TestUnits:
    def test_millimetres_are_the_default(self):
        converter = units.UnitConverter()
        assert converter.default is units.Unit.MILLIMETER
        assert converter.to_meters(50) == pytest.approx(0.05)

    @pytest.mark.parametrize(
        ("value", "unit", "expected"),
        [
            (50, "mm", 0.05),
            (5, "cm", 0.05),
            (0.05, "m", 0.05),
            (1, "in", 0.0254),
            (1, "inch", 0.0254),
            (1, "ft", 0.3048),
            (1000, "um", 0.001),
        ],
    )
    def test_conversion_to_metres(self, value, unit, expected):
        assert units.UnitConverter().to_meters(value, unit) == pytest.approx(expected)

    def test_round_trip_is_lossless(self):
        converter = units.UnitConverter("in")
        for value in (0.5, 1.0, 12.75, 100.0):
            assert converter.from_meters(converter.to_meters(value)) == pytest.approx(value)

    def test_unknown_unit_raises_rather_than_guessing(self):
        # The reference implementation logged a warning and silently fell back to
        # millimetres, turning a typo into wrong geometry. This must be loud.
        with pytest.raises(units.UnknownUnitError) as excinfo:
            units.parse_unit("furlong")
        assert "furlong" in str(excinfo.value)
        assert "mm" in str(excinfo.value)  # the message lists valid options

    def test_empty_unit_falls_back_to_the_default(self):
        assert units.parse_unit(None, units.Unit.INCH) is units.Unit.INCH
        assert units.parse_unit("", units.Unit.INCH) is units.Unit.INCH

    def test_points_conversion_preserves_order(self):
        converter = units.UnitConverter("mm")
        assert converter.points_to_meters([10, 20, 30]) == pytest.approx([0.01, 0.02, 0.03])

    def test_describe_reports_both_si_and_display(self):
        described = units.UnitConverter("mm").describe(0.025)
        assert described["meters"] == pytest.approx(0.025)
        assert described["value"] == pytest.approx(25.0)
        assert described["unit"] == "mm"

    def test_angles_convert_in_degrees(self):
        assert units.deg_to_rad(180) == pytest.approx(3.14159265, abs=1e-6)
        assert units.rad_to_deg(3.14159265) == pytest.approx(180, abs=1e-4)

    def test_full_turn_snaps_to_exactly_two_pi(self):
        # A 360-degree revolve computed through floating point lands a hair short
        # of a full turn, which SolidWorks renders as a hairline seam face.
        import math

        assert units.snap_full_turn(units.deg_to_rad(360)) == 2 * math.pi
        # A genuine partial revolve must not be touched.
        assert units.snap_full_turn(units.deg_to_rad(180)) == pytest.approx(math.pi)


# ---------------------------------------------------------------------------
# Result envelope
# ---------------------------------------------------------------------------

class TestResultEnvelope:
    def test_success_shape(self):
        payload = json.loads(R.ok("demo", "it worked", {"feature": "Boss-Extrude1"}))
        assert payload["ok"] is True
        assert payload["op"] == "demo"
        assert payload["data"]["feature"] == "Boss-Extrude1"

    def test_failure_carries_a_code_and_a_hint(self):
        payload = json.loads(
            R.fail("demo", "NO_ACTIVE_DOCUMENT", "nothing open", hint="call new_document")
        )
        assert payload["ok"] is False
        assert payload["error"]["code"] == "NO_ACTIVE_DOCUMENT"
        assert payload["error"]["hint"] == "call new_document"

    def test_tool_error_keeps_its_code_through_conversion(self):
        error = R.ToolError("FACE_NOT_FOUND", "index 9 out of range", hint="run analyze_model")
        payload = json.loads(R.from_exception("demo", error))
        assert payload["error"]["code"] == "FACE_NOT_FOUND"
        assert payload["error"]["hint"] == "run analyze_model"

    def test_unexpected_exception_becomes_a_traceback_not_a_crash(self):
        payload = json.loads(R.from_exception("demo", ValueError("boom")))
        assert payload["ok"] is False
        assert payload["error"]["code"] == "UNEXPECTED_ERROR"
        assert "traceback" in payload["error"]["details"]

    def test_timeout_gets_a_dialog_hint(self):
        payload = json.loads(R.from_exception("demo", TimeoutError("no response")))
        assert payload["error"]["code"] == "TIMEOUT"
        assert "dialog" in payload["error"]["hint"].lower()

    def test_unserialisable_values_degrade_to_repr(self):
        class Opaque:
            def __repr__(self):
                return "<com object>"

        payload = json.loads(R.ok("demo", "ok", {"handle": Opaque()}))
        assert payload["data"]["handle"] == "<com object>"


# ---------------------------------------------------------------------------
# Argument coercion
# ---------------------------------------------------------------------------

class TestArgumentCoercion:
    def test_accepts_a_real_list(self):
        assert R.parse_json_arg([1, 2, 3], "values") == [1, 2, 3]

    def test_accepts_a_json_string(self):
        # MCP clients differ in whether nested arrays arrive parsed or stringified;
        # both must work, or callers have to guess.
        assert R.parse_json_arg("[1, 2, 3]", "values") == [1, 2, 3]

    def test_accepts_a_json_object_string(self):
        parsed = R.parse_json_arg('{"component": "Shaft-1"}', "entity_a", dict)
        assert parsed["component"] == "Shaft-1"

    def test_malformed_json_names_the_parameter_and_shows_an_example(self):
        with pytest.raises(R.ToolError) as excinfo:
            R.parse_json_arg("[1, 2,", "entities")
        assert excinfo.value.code == "INVALID_PARAMETER"
        assert "entities" in excinfo.value.message

    def test_wrong_container_type_is_rejected(self):
        with pytest.raises(R.ToolError):
            R.parse_json_arg('{"a": 1}', "values", list)

    def test_missing_value_is_rejected(self):
        with pytest.raises(R.ToolError) as excinfo:
            R.parse_json_arg(None, "entities")
        assert excinfo.value.code == "MISSING_PARAMETER"

    def test_require_positive_rejects_zero_and_negatives(self):
        assert R.require_positive(5, "depth") == 5.0
        for bad in (0, -1):
            with pytest.raises(R.ToolError):
                R.require_positive(bad, "depth")

    def test_require_positive_rejects_non_numbers(self):
        with pytest.raises(R.ToolError) as excinfo:
            R.require_positive("deep", "depth")
        assert excinfo.value.code == "INVALID_PARAMETER"

    def test_one_of_normalises_case_and_whitespace(self):
        assert R.one_of("  BOSS ", "feature_type", {"boss", "cut"}) == "boss"

    def test_one_of_lists_the_valid_options_on_failure(self):
        with pytest.raises(R.ToolError) as excinfo:
            R.one_of("bevel", "feature_type", {"boss", "cut"})
        assert "boss" in excinfo.value.hint and "cut" in excinfo.value.hint


# ---------------------------------------------------------------------------
# Enum tables
# ---------------------------------------------------------------------------

class TestEnums:
    def test_revision_maps_to_marketing_year(self):
        assert enums.year_for_revision("33.2.0") == 2025
        assert enums.year_for_revision("31") == 2023
        assert enums.year_for_revision("34.1.5") == 2026

    def test_unknown_revision_returns_none_rather_than_guessing(self):
        assert enums.year_for_revision("not-a-version") is None
        assert enums.year_for_revision("99.0.0") is None

    def test_document_type_inferred_from_extension(self):
        assert enums.DOC_TYPE_BY_EXT[".sldprt"] == enums.DOC_PART
        assert enums.DOC_TYPE_BY_EXT[".sldasm"] == enums.DOC_ASSEMBLY
        assert enums.DOC_TYPE_BY_EXT[".slddrw"] == enums.DOC_DRAWING

    def test_mate_type_tables_are_inverses(self):
        for name, code in enums.MATE_TYPE_BY_NAME.items():
            assert enums.MATE_NAME_BY_TYPE[code] == name

    def test_plane_aliases_cover_short_and_geometric_forms(self):
        for alias in ("front", "Front Plane", "xy"):
            assert enums.PLANE_ALIASES[alias.lower()] == "Front Plane"

    def test_bend_position_encodings_differ_between_apis(self):
        # A real trap: InsertSheetMetal3dBend encodes centreline as 0, while
        # ISketchedBendFeatureData.PositionType encodes it as 4. Conflating them
        # silently produces the wrong bend position.
        assert enums.BEND_3D_POS_BY_NAME["centerline"] == 0
        assert enums.FLANGE_POS_BY_NAME["centerline"] == 4

    def test_sketch_constraints_declare_their_entity_count(self):
        assert enums.SKETCH_CONSTRAINT_BY_NAME["horizontal"][1] == 1
        assert enums.SKETCH_CONSTRAINT_BY_NAME["coincident"][1] == 2
        assert enums.SKETCH_CONSTRAINT_BY_NAME["symmetric"][1] == 3


# ---------------------------------------------------------------------------
# Compatibility layer
# ---------------------------------------------------------------------------

pytest.importorskip("pythoncom", reason="the compat module imports pywin32")

from solidworks_mcp.com import compat  # noqa: E402


class FakeProperty:
    """A build where ``GetTitle`` binds as a property."""

    GetTitle = "Part1"
    GetTypeName2 = "Extrusion"


class FakeMethod:
    """A build where the same accessors bind as methods."""

    def GetTitle(self):
        return "Part1"

    def GetTypeName2(self):
        return "Extrusion"


class TestPropertyOrMethod:
    def test_reads_a_property_binding(self):
        assert compat.pm(FakeProperty(), "GetTitle") == "Part1"

    def test_reads_a_method_binding(self):
        assert compat.pm(FakeMethod(), "GetTitle") == "Part1"

    def test_missing_member_returns_the_default(self):
        assert compat.pm(FakeProperty(), "NoSuchThing", default="fallback") == "fallback"

    def test_missing_member_raises_without_a_default(self):
        with pytest.raises(AttributeError):
            compat.pm(FakeProperty(), "NoSuchThing")

    def test_a_raising_method_returns_the_default(self):
        class Raising:
            def GetTitle(self):
                raise RuntimeError("com_error")

        assert compat.pm(Raising(), "GetTitle", default=None) is None

    def test_has_member_probes_without_calling(self):
        assert compat.has_member(FakeMethod(), "GetTitle") is True
        assert compat.has_member(FakeMethod(), "GetNothing") is False

    def test_first_member_picks_the_newest_available(self):
        assert compat.first_member(
            FakeMethod(), ["GetTypeName3", "GetTypeName2", "GetTypeName"]
        ) == "GetTypeName2"


class FakeFeatureManager:
    """A feature manager standing in for a specific SolidWorks generation.

    ``generation`` selects which API is exposed and with how many arguments,
    reproducing the two real failure modes: the method does not exist at all, or it
    exists with a different arity.
    """

    def __init__(self, generation: int):
        self.generation = generation
        self.calls: list[tuple[str, int]] = []

    def FeatureExtrusion3(self, *args):
        self.calls.append(("FeatureExtrusion3", len(args)))
        if self.generation < 3:
            raise AttributeError("not available on this build")
        if len(args) != 23:
            raise TypeError(f"expected 23 arguments, got {len(args)}")
        return "Boss-Extrude1"

    def FeatureExtrusion2(self, *args):
        self.calls.append(("FeatureExtrusion2", len(args)))
        if len(args) != 20:
            raise TypeError(f"expected 20 arguments, got {len(args)}")
        return "Boss-Extrude1-legacy"


class TestInvokeFirst:
    def test_modern_build_wins_on_the_first_candidate(self):
        manager = FakeFeatureManager(generation=3)
        report = compat.invoke_first(manager, [
            ("FeatureExtrusion3", tuple(range(23))),
            ("FeatureExtrusion2", tuple(range(20))),
        ])
        assert report.ok
        assert report.value == "Boss-Extrude1"
        assert report.signature == "FeatureExtrusion3/23"
        # The legacy candidate must not have been touched.
        assert len(manager.calls) == 1

    def test_older_build_falls_through_to_the_legacy_signature(self):
        manager = FakeFeatureManager(generation=2)
        report = compat.invoke_first(manager, [
            ("FeatureExtrusion3", tuple(range(23))),
            ("FeatureExtrusion2", tuple(range(20))),
        ])
        assert report.ok
        assert report.value == "Boss-Extrude1-legacy"
        assert report.signature == "FeatureExtrusion2/20"

    def test_arity_mismatch_falls_through_to_the_next_vector(self):
        # Same method name, different argument counts -- how a mid-generation
        # parameter addition is absorbed.
        manager = FakeFeatureManager(generation=2)
        report = compat.invoke_first(manager, [
            ("FeatureExtrusion2", tuple(range(23))),
            ("FeatureExtrusion2", tuple(range(20))),
        ])
        assert report.ok
        assert report.signature == "FeatureExtrusion2/20"

    def test_total_failure_reports_every_attempt(self):
        manager = FakeFeatureManager(generation=1)
        report = compat.invoke_first(manager, [
            ("FeatureExtrusion3", tuple(range(23))),
            ("FeatureExtrusion2", tuple(range(99))),
        ])
        assert not report.ok
        assert report.value is None
        diagnostic = report.diagnostic()
        # The diagnostic is what makes a failure on an unusual build debuggable.
        assert "FeatureExtrusion3/23" in diagnostic
        assert "FeatureExtrusion2/99" in diagnostic

    def test_null_return_is_treated_as_failure(self):
        # SolidWorks signals feature-creation failure by returning null, not by
        # raising, so the default predicate must reject None.
        class Nulling:
            def MakeFeature(self, *args):
                return None

        report = compat.invoke_first(Nulling(), [("MakeFeature", ())])
        assert not report.ok
        assert "returned null" in report.diagnostic()

    def test_invoke_bool_treats_false_as_failure(self):
        # InsertAxis2 and SetSuppression2 return False on failure; accepting any
        # non-None value would report those as successes.
        class Boolish:
            def InsertAxis2(self, *args):
                return False

        assert not compat.invoke_bool(Boolish(), [("InsertAxis2", (False,))]).ok

    def test_invoke_any_accepts_void_returns(self):
        # InsertRib returns void; success means "dispatched without raising", and
        # the caller verifies the effect separately.
        class Void:
            def InsertRib(self, *args):
                return None

        assert compat.invoke_any(Void(), [("InsertRib", ())]).ok

    def test_a_bare_value_where_arguments_were_needed_is_reported_clearly(self):
        class PropertyNotMethod:
            FeatureExtrusion3 = "oops"

        report = compat.invoke_first(
            PropertyNotMethod(), [("FeatureExtrusion3", tuple(range(23)))]
        )
        assert not report.ok
        diagnostic = report.diagnostic()
        assert "bare str" in diagnostic
        assert "could not be passed" in diagnostic


class TestArrayNormalisation:
    def test_none_becomes_an_empty_list(self):
        # SolidWorks returns None rather than an empty array for "no entities".
        assert compat.as_list(None) == []

    def test_tuple_is_preserved_and_nulls_dropped(self):
        assert compat.as_list((1, None, 2)) == [1, 2]

    def test_a_bare_object_is_wrapped(self):
        # A SAFEARRAY-typed property returning a single element arrives unwrapped.
        assert compat.as_list("solo") == ["solo"]

    def test_non_numeric_entries_are_skipped_by_as_floats(self):
        assert compat.as_floats((1.0, "x", 3)) == [1.0, 3.0]


class FakeFeature:
    """A node in a fake feature tree, exercising the property-style link."""

    def __init__(self, name: str, type_name: str, nxt=None):
        self.Name = name
        self.GetTypeName2 = type_name
        self.GetNextFeature = nxt


class FakeDoc:
    def __init__(self, head):
        self.FirstFeature = head


def build_tree() -> FakeDoc:
    extrude = FakeFeature("Boss-Extrude1", "Extrusion")
    sketch = FakeFeature("Sketch1", "ProfileFeature", extrude)
    plane = FakeFeature("Front Plane", "RefPlane", sketch)
    return FakeDoc(plane)


class TestTreeTraversal:
    def test_walks_in_tree_order(self):
        names = [compat.feature_name(f) for f in compat.iter_features(build_tree())]
        assert names == ["Front Plane", "Sketch1", "Boss-Extrude1"]

    def test_reads_types(self):
        types = [compat.feature_type(f) for f in compat.iter_features(build_tree())]
        assert types == ["RefPlane", "ProfileFeature", "Extrusion"]

    def test_find_by_name(self):
        found = compat.find_feature(build_tree(), "Sketch1")
        assert compat.feature_name(found) == "Sketch1"

    def test_find_collects_available_names_for_the_error_message(self):
        names: list[str] = []
        assert compat.find_feature(build_tree(), "Nope", collect=names) is None
        assert names == ["Front Plane", "Sketch1", "Boss-Extrude1"]

    def test_find_by_type(self):
        found = compat.find_feature_by_type(build_tree(), "ProfileFeature")
        assert compat.feature_name(found) == "Sketch1"

    def test_last_feature_name_is_the_void_api_anchor(self):
        # InsertRib returns void, so creation is verified by comparing this before
        # and after the call.
        assert compat.last_feature_name(build_tree()) == "Boss-Extrude1"

    def test_empty_tree_is_handled(self):
        assert compat.feature_names(FakeDoc(None)) == []
        assert compat.last_feature_name(FakeDoc(None)) is None

    def test_a_cyclic_tree_cannot_hang_the_server(self):
        node = FakeFeature("Loop", "Extrusion")
        node.GetNextFeature = node
        assert len(list(compat.iter_features(FakeDoc(node)))) == compat.WALK_LIMIT


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

from solidworks_mcp.config import Config, load_config  # noqa: E402


class TestConfig:
    def test_defaults_are_sensible(self):
        config = Config()
        assert config.default_unit == "mm"
        assert config.auto_launch is True
        assert config.visible is True  # an invisible instance with a modal dialog is unrecoverable

    def test_log_level_name_resolves_to_a_number(self):
        assert Config(log_level="DEBUG").numeric_log_level() == 10
        assert Config(log_level="nonsense").numeric_log_level() == 20  # INFO

    def test_environment_overrides_file_and_defaults(self, monkeypatch):
        monkeypatch.setenv("SOLIDWORKS_DEFAULT_UNIT", "in")
        monkeypatch.setenv("SOLIDWORKS_AUTO_LAUNCH", "false")
        config = load_config()
        assert config.default_unit == "in"
        assert config.auto_launch is False
        assert "env(" in config.to_dict()["config_source"]

    def test_a_malformed_environment_value_is_ignored_not_fatal(self, monkeypatch):
        monkeypatch.setenv("SOLIDWORKS_STARTUP_TIMEOUT", "soon")
        assert load_config().startup_timeout == Config().startup_timeout


# ---------------------------------------------------------------------------
# Export format table
# ---------------------------------------------------------------------------

class TestExportFormats:
    def test_iges_uses_the_extension_the_translator_accepts(self):
        from solidworks_mcp.ops.documents import EXPORT_FORMATS

        # The IGES translator rejects '.iges' and fails with SaveAs error 256.
        assert EXPORT_FORMATS["IGES"] == ".igs"

    def test_drawing_only_formats_are_declared(self):
        from solidworks_mcp.ops.documents import DRAWING_ONLY_FORMATS

        assert {"DWG", "DXF"} == DRAWING_ONLY_FORMATS

    def test_every_format_declares_an_extension(self):
        from solidworks_mcp.ops.documents import EXPORT_FORMATS

        for fmt, extension in EXPORT_FORMATS.items():
            assert extension.startswith("."), fmt


# ---------------------------------------------------------------------------
# Regressions found against live SolidWorks 2023
# ---------------------------------------------------------------------------

class CDispatch:
    """Stands in for a ``win32com`` ``CDispatch``.

    Deliberately named ``CDispatch``, because that is how
    :func:`compat.is_com_object` recognises a COM pointer without importing
    ``win32com`` types into the test.

    The defining trait is that it is **callable** -- the real ``CDispatch``
    implements ``__call__`` for COM default members -- while being a *value*, not a
    method. Calling one raises "Member not found", which is exactly how a bare
    ``callable()`` test silently turned every object-valued property into ``None``.
    """

    def __init__(self, **members):
        self._members = members

    def __call__(self, *args, **kwargs):
        raise RuntimeError("(-2147352573, 'Member not found.', None, None)")

    def __getattr__(self, name):
        try:
            return self._members[name]
        except KeyError:
            raise AttributeError(f"<unknown>.{name}") from None


#: Readable alias for use in the tests below.
FakeCOMObject = CDispatch


class TestObjectValuedPropertyRegression:
    """``pm()`` must not call a property that returned a COM object.

    Found live on SolidWorks 2023: ``sw.ActiveDoc`` returns the document as a
    callable ``CDispatch``, so ``pm()`` invoked it, got "Member not found", and
    returned its default of ``None``. Every tool then reported NO_ACTIVE_DOCUMENT
    with a part clearly open, and the same applied to ``SketchManager``,
    ``FeatureManager``, ``Extension`` and ``SelectionManager``.
    """

    def test_a_callable_com_value_is_recognised_as_an_object(self):
        com = FakeCOMObject()
        assert callable(com), "the stand-in must be callable to reproduce the bug"
        assert compat.is_com_object(com) is True
        assert compat.should_invoke(com) is False

    def test_object_valued_property_is_returned_not_called(self):
        document = FakeCOMObject(GetTitle="Part1")
        app = FakeCOMObject(ActiveDoc=document)
        resolved = compat.pm(app, "ActiveDoc", default=None)
        assert resolved is document
        assert compat.pm(resolved, "GetTitle") == "Part1"

    def test_the_whole_object_property_chain_resolves(self):
        # app -> ActiveDoc -> SketchManager -> ActiveSketch, the real call chain.
        sketch = FakeCOMObject(Name="Sketch1")
        manager = FakeCOMObject(ActiveSketch=sketch)
        document = FakeCOMObject(SketchManager=manager, GetTitle="Part1")
        app = FakeCOMObject(ActiveDoc=document)

        doc = compat.pm(app, "ActiveDoc", default=None)
        sm = compat.pm(doc, "SketchManager", default=None)
        active = compat.pm(sm, "ActiveSketch", default=None)
        assert compat.pm(active, "Name") == "Sketch1"

    def test_a_genuine_method_is_still_invoked(self):
        class WithMethod:
            def GetTitle(self):
                return "Part1"

        assert compat.pm(WithMethod(), "GetTitle") == "Part1"
        assert compat.should_invoke(WithMethod().GetTitle) is True

    def test_plain_values_are_never_invoked(self):
        # Late binding invokes zero-argument members during attribute access, so
        # these arrive already resolved.
        for value in ("Part1", 1, True, 0.5, (1, 2), [1], {"a": 1}, None):
            assert compat.should_invoke(value) is False

    def test_an_empty_object_property_reads_as_none(self):
        manager = FakeCOMObject(ActiveSketch=None)
        assert compat.pm(manager, "ActiveSketch", default="sentinel") is None


class TestAlreadyInvokedMemberRegression:
    """``invoke_first`` must accept a zero-argument member as having run.

    Late binding invokes zero-argument members during attribute access, so
    ``getattr(doc, "EditRebuild3")`` returns a ``bool`` -- the rebuild already
    happened. Reporting that as "resolved as a property, not a method" turned every
    successful void call into a spurious failure.
    """

    def test_a_zero_arg_member_that_already_ran_counts_as_success(self):
        target = FakeCOMObject(EditRebuild3=True)
        report = compat.invoke_any(target, [("EditRebuild3", ())])
        assert report.ok
        assert report.value is True

    def test_a_falsy_already_invoked_result_is_reported_honestly(self):
        # invoke_bool must still treat False as failure.
        target = FakeCOMObject(InsertAxis2=False)
        assert not compat.invoke_bool(target, [("InsertAxis2", ())]).ok

    def test_an_object_valued_member_is_returned_by_invoke_first(self):
        feature = FakeCOMObject(Name="Boss-Extrude1")
        target = FakeCOMObject(CreateFeature=feature)
        report = compat.invoke_first(target, [("CreateFeature", ())])
        assert report.ok
        assert report.value is feature

    def test_arguments_against_a_bare_value_are_reported_clearly(self):
        # A member that resolved to a plain value cannot accept arguments; the
        # diagnostic should say so rather than claiming the member is absent.
        target = FakeCOMObject(GetTitle="Part1")
        report = compat.invoke_first(target, [("GetTitle", (1, 2, 3))])
        assert not report.ok
        assert "could not be passed" in report.diagnostic()

    def test_a_real_method_still_wins_on_arity(self):
        class Manager:
            def FeatureExtrusion3(self, *args):
                if len(args) != 23:
                    raise TypeError("wrong arity")
                return "Boss-Extrude1"

        report = compat.invoke_first(Manager(), [
            ("FeatureExtrusion3", tuple(range(20))),
            ("FeatureExtrusion3", tuple(range(23))),
        ])
        assert report.ok
        assert report.signature == "FeatureExtrusion3/23"


class TestTreeContainerFiltering:
    """Every part tree opens with a dozen UI container nodes."""

    def test_folder_types_are_containers(self):
        for type_name in (
            "CommentsFolder", "FavoriteFolder", "HistoryFolder", "SelectionSetFolder",
            "SensorFolder", "DocsFolder", "SurfaceBodyFolder", "SolidBodyFolder",
            "EnvFolder", "InkMarkupFolder", "EqnFolder", "MaterialFolder",
        ):
            assert compat.is_tree_container(type_name), type_name

    def test_the_two_non_folder_containers_are_covered(self):
        assert compat.is_tree_container("DetailCabinet")
        assert compat.is_tree_container("OriginProfileFeature")

    def test_real_features_are_not_containers(self):
        for type_name in (
            "ProfileFeature", "Extrusion", "RefPlane", "FlatPattern", "Fillet",
            "MateGroup", "SMBaseFlange",
        ):
            assert not compat.is_tree_container(type_name), type_name


class TestEnumSync:
    """The type-library binding table that overrides the hand-maintained literals.

    Checking these literals against a real SolidWorks 2023 type library found nine
    wrong values, so the binding table is load-bearing: a typo in it silently
    reverts a constant to the bad fallback.
    """

    @staticmethod
    def _bindings():
        from solidworks_mcp.com.constants import _ENUM_BINDINGS

        return _ENUM_BINDINGS

    def test_every_bound_attribute_exists_on_the_enums_module(self):
        # A typo here means sync_enums() would create a NEW attribute nobody reads,
        # leaving the real constant on its unverified fallback.
        missing = [name for name in self._bindings() if not hasattr(enums, name)]
        assert not missing, f"bindings reference non-existent enums attributes: {missing}"

    def test_every_bound_attribute_is_currently_an_int(self):
        for name in self._bindings():
            assert isinstance(getattr(enums, name), int), name

    def test_swconst_member_names_look_like_swconst_members(self):
        bad = [m for m in self._bindings().values() if not m.startswith("sw")]
        assert not bad, f"these do not look like swconst members: {bad}"

    def test_no_duplicate_member_bindings(self):
        members = list(self._bindings().values())
        duplicates = {m for m in members if members.count(m) > 1}
        assert not duplicates, f"the same swconst member is bound twice: {duplicates}"

    def test_the_constants_verified_live_carry_their_corrected_values(self):
        # Regression guards for the nine wrong values found against SolidWorks 2023.
        assert enums.FEATURE_ID_BASE_FLANGE == 34
        assert enums.FEATURE_ID_EDGE_FLANGE == 37
        assert enums.FEATURE_ID_SHELL == 8
        assert enums.INSERT_DIMS_MARKED_FOR_DRAWING == 32768
        assert enums.ROLLBACK_TO_END == 1
        assert enums.ROLLBACK_TO_BEFORE_FEATURE == 3
        assert enums.REFPLANE_OPTION_FLIP == 256
        assert enums.MATE_LOCK == 16
        assert enums.PREF_INPUT_DIM_VAL_ON_CREATE == 10

    def test_section_view_default_is_zero_not_a_stray_bit(self):
        # There is no "auto hatch" member; bit 1 is NotAligned, so a supposedly
        # harmless default of 1 silently un-aligned every section view.
        assert enums.SECTION_VIEW_DEFAULT == 0
        assert enums.SECTION_VIEW_NOT_ALIGNED == 1
        assert enums.SECTION_VIEW_CHANGE_DIRECTION == 4

    def test_derived_tables_rebuild_from_the_current_scalars(self):
        original = enums.MATE_LOCK
        try:
            enums.MATE_LOCK = 99
            enums.rebuild_derived_tables()
            assert enums.MATE_TYPE_BY_NAME["lock"] == 99
            assert enums.MATE_NAME_BY_TYPE[99] == "lock"
        finally:
            enums.MATE_LOCK = original
            enums.rebuild_derived_tables()
        assert enums.MATE_TYPE_BY_NAME["lock"] == original

    def test_sync_is_a_no_op_without_a_type_library(self, monkeypatch):
        from solidworks_mcp.com import constants as swconst

        monkeypatch.setattr(swconst, "_table", {})
        monkeypatch.setattr(swconst, "_sync_report", None)
        report = swconst.sync_enums()
        assert report["synced"] is False
        assert "fallback" in report["reason"]

    def test_sync_reports_and_applies_corrections(self, monkeypatch):
        from solidworks_mcp.com import constants as swconst

        original = enums.MATE_LOCK
        try:
            monkeypatch.setattr(swconst, "_table", {"swMateLOCK": 42})
            monkeypatch.setattr(swconst, "_sync_report", None)
            report = swconst.sync_enums()
            assert report["synced"] is True
            assert report["corrected"]["MATE_LOCK"] == {
                "was": original, "now": 42, "member": "swMateLOCK",
            }
            assert enums.MATE_LOCK == 42
            # ...and the derived table followed.
            assert enums.MATE_TYPE_BY_NAME["lock"] == 42
            # Members absent from the (stubbed) library are reported, not applied.
            assert "swDocPART" in report["unknown_members"]
        finally:
            enums.MATE_LOCK = original
            enums.rebuild_derived_tables()
            monkeypatch.setattr(swconst, "_sync_report", None)


# ---------------------------------------------------------------------------
# Version support
# ---------------------------------------------------------------------------

class TestVersionSupport:
    """The verdict combining recognized year, live-tested year, and constant
    verification -- kept as a pure function so it needs no COM connection."""

    def test_directly_tested_year_is_verified(self):
        result = enums.version_support(2023, "31.1.0", constants_verified=True)
        assert result["level"] == "verified"
        assert result["supported"] is True
        assert result["tested_live"] is True

    def test_recognized_untested_year_with_verified_constants_is_compatible(self):
        result = enums.version_support(2025, "33.2.0", constants_verified=True)
        assert result["level"] == "compatible"
        assert result["supported"] is True
        assert result["tested_live"] is False
        assert result["recognized_version"] is True

    def test_recognized_year_without_verified_constants_is_degraded(self):
        result = enums.version_support(2025, "33.2.0", constants_verified=False)
        assert result["level"] == "degraded"
        assert result["supported"] is False

    def test_unrecognized_revision_is_unsupported(self):
        result = enums.version_support(None, "99.0.0", constants_verified=False)
        assert result["level"] == "unsupported"
        assert result["supported"] is False
        assert result["recognized_version"] is False
        assert "99.0.0" in result["message"]

    def test_unrecognized_revision_with_constants_still_unsupported(self):
        # constants_verified alone cannot compensate for a year this server has
        # never even heard of -- REVISION_TO_YEAR itself must be extended first.
        result = enums.version_support(None, "99.0.0", constants_verified=True)
        assert result["level"] == "unsupported"
        assert result["supported"] is False

    def test_only_2023_is_pre_marked_as_directly_tested(self):
        assert {2023} == enums.VERIFIED_VERSIONS


# ---------------------------------------------------------------------------
# Drawing view geometry parsing
# ---------------------------------------------------------------------------

class TestPolylineRecordParsing:
    """``GetPolyLinesAndCurves``' flat array, split into line/curve records.

    The fixture below is the exact array captured live from a rectangular plate
    with one circular hole: 4 straight edges (2 points each, 9-value header)
    plus the hole tessellated as a 50-point curve (21-value header). Reproduces
    the real record shapes rather than a synthetic simplification, so a change
    that breaks the header-length resolution shows up here, not just live.
    """

    @staticmethod
    def _parse():
        from solidworks_mcp.ops.drawings import _parse_polyline_records

        return _parse_polyline_records

    @staticmethod
    def _line_record(count=2):
        # 9-value header: type flag (0 = line) + 7 unused metadata + point count.
        return [0.0, 0.0, 0.0, 0.0, -1.0, 1.0, -1.0, 0.0, float(count)] + [
            0.01 * i for i in range(count * 3)
        ]

    @staticmethod
    def _curve_record(count=50):
        # 21-value header: type flag (non-zero) + 19 unused metadata + point count.
        return [1.0] + [0.0] * 18 + [0.0, float(count)] + [
            0.01 * i for i in range(count * 3)
        ]

    def test_four_lines_and_one_curve_parse_with_no_leftover(self):
        values = (
            self._line_record(2) + self._curve_record(50)
            + self._line_record(2) + self._line_record(2) + self._line_record(2)
        )
        records = self._parse()(values)
        assert records is not None
        assert [is_line for is_line, _ in records] == [True, False, True, True, True]
        assert len(records[1][1]) == 150  # 50 points * 3

    def test_line_record_yields_segments_between_consecutive_points(self):
        values = self._line_record(2)
        records = self._parse()(values)
        assert records == [(True, values[9:])]

    def test_unparseable_array_returns_none(self):
        assert self._parse()([1.0, 2.0, 3.0]) is None

    def test_empty_array_parses_to_no_records(self):
        assert self._parse()([]) == []

    def test_an_unrecognized_header_length_is_still_resolved(self):
        # A hypothetical curve subtype whose header is neither 9 nor 21 long --
        # the bounded fallback scan must still find it rather than giving up.
        header_len = 15
        count = 6
        weird_curve = [1.0] + [0.0] * (header_len - 2) + [float(count)] + [
            0.02 * i for i in range(count * 3)
        ]
        values = self._line_record(2) + weird_curve
        records = self._parse()(values)
        assert records is not None
        assert [is_line for is_line, _ in records] == [True, False]
        assert len(records[1][1]) == count * 3
