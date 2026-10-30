import json
from types import SimpleNamespace

import pytest

from document_policy import DocumentPolicy
from solidworks_mcp.ops import documents
from solidworks_mcp.util.result import ToolError


class Document:
    def __init__(self, title, dirty=False):
        self.title, self.dirty, self.closed = title, dirty, False

    def GetTitle(self):
        return self.title

    def GetSaveFlag(self):
        return self.dirty


class Session:
    def __init__(self, active):
        self.active = active
        self.app = SimpleNamespace(ActiveDoc=active)
        self.com = SimpleNamespace(call=lambda fn, *args, **kwargs: fn(*args))

    def active_doc(self):
        return self.active

    def ensure(self):
        pass

    def template_for(self, *_):
        return "example.prtdot"

    def doc_type(self, doc):
        return "PART"


def test_native_creation_tracks_returned_object_after_active_window_switch():
    created, existing = Document("created"), Document("existing-user-file")
    session = Session(existing)
    policy = DocumentPolicy(session, id)
    # The UI has already switched to a different document when NewDocument returns.
    session.app = SimpleNamespace(NewDocument=lambda *_: created)
    documents.new_document(session, "part")
    assert policy.records == [(id(created), True)]
    with pytest.raises(ToolError):
        policy.require_active()


def test_mutating_operation_pins_the_checked_document(monkeypatch):
    from solidworks_mcp import server
    owned, existing = Document("owned"), Document("existing-user-file")
    session = Session(owned)
    policy = DocumentPolicy(session, id)
    policy.remember(owned, True)
    monkeypatch.setattr(server, "SESSION", session)

    @server.tool("modify_dimension")
    def operation():
        session.active = existing
        return "checked", {"target":session.active_doc().title}

    result = json.loads(operation())
    assert result["ok"] and result["data"]["target"] == "owned"
    assert session.active_doc() is existing


def test_ownership_is_checked_at_actual_dispatch_not_only_earlier(monkeypatch):
    from solidworks_mcp import server
    owned, existing = Document("owned"), Document("existing-user-file")
    session = Session(owned)
    policy = DocumentPolicy(session, id)
    policy.remember(owned, True)
    policy.require_active()  # The earlier caller-side check succeeds.
    session.active = existing
    monkeypatch.setattr(server, "SESSION", session)
    touched = []

    @server.tool("modify_dimension")
    def operation():
        touched.append(session.active_doc())
        return "modified", {}

    result = json.loads(operation())
    assert not result["ok"] and result["error"]["code"] == "DOCUMENT_NOT_OWNED"
    assert not touched


def test_open_callback_never_grants_write_access():
    doc = Document("opened")
    session = Session(doc)
    policy = DocumentPolicy(session, id)
    session.on_open_document(doc)
    assert policy.require_active(writable=False) is doc
    with pytest.raises(ToolError):
        policy.require_active()


def test_close_rechecks_unsaved_changes_on_actual_target():
    doc = Document("unsaved", dirty=True)
    session = Session(doc)
    policy = DocumentPolicy(session, id)
    policy.remember(doc, True)
    with pytest.raises(ValueError, match="Save changes"):
        with policy.scope("close_document"):
            pytest.fail("A dirty document must never reach the close body")


def test_close_records_target_before_native_handle_is_closed():
    first, second = Document("first"), Document("second")
    session = Session(first)

    def identity(doc):
        if doc.closed:
            raise RuntimeError("closed native handle")
        return id(doc)

    policy = DocumentPolicy(session, identity)
    policy.remember(first, True)
    policy.remember(second, True)
    with policy.scope("close_document"):
        session.active = second
        assert session.active_doc() is first
        first.closed = True
    policy.forget_closed()
    assert policy.records == [(id(second), True)]
