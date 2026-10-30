"""Regression checks for the local query-only connection policy."""

from unittest.mock import Mock

import pytest

from solidworks_mcp.com import compat, connection
from solidworks_mcp.com.connection import SolidWorksError, SolidWorksSession
from solidworks_mcp.config import Config


def test_missing_running_instance_never_calls_a_creating_dispatch(monkeypatch):
    monkeypatch.setattr(connection.win32com.client, "GetActiveObject", Mock(side_effect=RuntimeError("not running")))
    create = Mock(side_effect=AssertionError("must not create an instance"))
    monkeypatch.setattr(connection.win32com.client, "Dispatch", create)
    monkeypatch.setattr(connection.win32com.client, "GetObject", create)
    session = SolidWorksSession(Config(auto_launch=False))
    try:
        with pytest.raises(SolidWorksError) as error:
            session.ensure(launch=True)
        assert error.value.code == "NOT_RUNNING"
        create.assert_not_called()
    finally:
        session.shutdown()


def test_attach_preserves_dimension_input_preference(monkeypatch):
    class App:
        RevisionNumber = "31.0.1"
        SetUserPreferenceToggle = Mock()

    app = App()
    monkeypatch.setattr(connection.win32com.client, "GetActiveObject", lambda _: app)
    monkeypatch.setattr(connection.swconst, "sync_enums", lambda: None)
    monkeypatch.setattr(connection.swconst, "verified", lambda *_: (123, True))
    session = SolidWorksSession(Config(auto_launch=False, visible=False))
    try:
        assert session.com.call(session._attach)
        app.SetUserPreferenceToggle.assert_not_called()
    finally:
        session.shutdown()


def test_capability_probe_resolves_name_without_invoking_member():
    class Dispatch:
        def GetIDsOfNames(self, name):
            if name == "CloseAllDocuments":
                return 12
            raise AttributeError(name)

    class App:
        _oleobj_ = Dispatch()

        def __getattr__(self, name):
            raise AssertionError("probe must not invoke COM attribute access")

    assert compat.has_member(App(), "CloseAllDocuments")
    assert not compat.has_member(App(), "MissingMember")


def test_underdefined_sketch_is_never_reported_as_fully_defined():
    # SolidWorks swConstrainedStatus_e: 2=under, 3=fully, 4=over.
    assert compat.sketch_constraint_status(2) == "under_defined"
    assert compat.sketch_constraint_status(3) == "fully_defined"
    assert compat.sketch_constraint_status(4) == "over_defined"


def test_feature_dimensions_are_followed_through_the_owning_feature():
    displays = [object(), object(), object()]

    class Feature:
        def GetFirstDisplayDimension(self):
            return displays[0]

        def GetNextDisplayDimension(self, current):
            index = displays.index(current) + 1
            return displays[index] if index < len(displays) else None

    assert list(compat.iter_display_dimensions(Feature())) == displays
