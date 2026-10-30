import math
import json
import threading
from types import SimpleNamespace

import pytest

import parametric_plate_tools as tools


@pytest.mark.parametrize("values", [
    (120, 80, 0, 8, 15), (math.nan, 80, 10, 8, 15),
    (120, 80, 10, 8, 4), (38, 80, 10, 8, 15),
])
def test_invalid_or_touching_geometry_is_rejected(values):
    with pytest.raises(ValueError):
        tools.validate_parameters(*values)


def test_dimension_dialog_preference_can_be_restored_after_failure(monkeypatch):
    class App:
        value = True

        def GetUserPreferenceToggle(self, index):
            assert index == 10
            return self.value

        def SetUserPreferenceToggle(self, index, value):
            assert index == 10
            self.value = value

    monkeypatch.setattr(tools.constants, "table", lambda: {"swInputDimValOnCreate": 10})
    session = SimpleNamespace(app=App())
    token = tools.suspend_dimension_input(session)
    try:
        assert session.app.value is False
        raise ValueError("native dimension creation failed")
    except ValueError:
        pass
    finally:
        tools.restore_dimension_input(session, token)
    assert session.app.value is True


def test_disabled_equation_engine_is_rejected_before_creating_a_document(monkeypatch):
    monkeypatch.delenv("SOLIDWORKS_ENABLE_EQUATIONS", raising=False)
    registered = {}

    class Server:
        def tool(self):
            def register(function):
                registered[function.__name__] = function
                return function
            return register

    tools.register(Server(), object(), None, threading.RLock())
    response = json.loads(registered["create_parametric_plate"](use_equations=True))
    assert not response["ok"]
    assert response["error"]["details"]["stage"] == "validate_parameters"


def test_preference_readback_failure_before_token_restores_original(monkeypatch):
    class App:
        value = True
        reads = 0

        def GetUserPreferenceToggle(self, index):
            self.reads += 1
            if self.reads == 2:
                raise RuntimeError("readback failed")
            return self.value

        def SetUserPreferenceToggle(self, index, value):
            self.value = value

    monkeypatch.setattr(tools.constants, "table", lambda: {"swInputDimValOnCreate":10})
    session = SimpleNamespace(app=App())
    with pytest.raises(RuntimeError, match="readback failed"):
        tools.suspend_dimension_input(session)
    assert session.app.value is True


def test_unknown_preference_does_not_modify_user_settings(monkeypatch):
    from unittest.mock import Mock
    app = SimpleNamespace(GetUserPreferenceToggle=lambda _: None, SetUserPreferenceToggle=Mock())
    monkeypatch.setattr(tools.constants, "table", lambda: {"swInputDimValOnCreate":10})
    with pytest.raises(RuntimeError):
        tools.suspend_dimension_input(SimpleNamespace(app=app))
    app.SetUserPreferenceToggle.assert_not_called()
