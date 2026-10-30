"""Check workspace path and document-ownership boundaries without touching CAD."""

from pathlib import Path
import json

import pytest

import solidworks_modeling_server as server


@pytest.mark.parametrize("path", [r"C:\other\plate.SLDPRT", r"C:\AI-Workspace\CAD-extra\plate.SLDPRT", r"C:\AI-Workspace\CAD\..\plate.SLDPRT", r"C:\AI-Workspace\CAD\plate.step", r"C:\AI-Workspace\CAD\plate.SLDPRT:stream", r"C:\AI-Workspace\CAD\CON.SLDPRT"])
def test_unapproved_save_paths_are_rejected(path):
    with pytest.raises(ValueError):
        server._checked_path(path, must_exist=False)


def test_existing_file_is_not_overwritten(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "CAD_ROOT", tmp_path.resolve())
    target = tmp_path / "existing.SLDPRT"
    target.write_bytes(b"original")
    with pytest.raises(FileExistsError):
        server._checked_path(str(target), must_exist=False)
    response = json.loads(server.save_document(str(target)))
    assert not response["ok"] and response["error"]["code"] == "FILE_EXISTS"
    assert target.read_bytes() == b"original"


def test_unmanaged_and_readonly_documents_cannot_be_mutated(monkeypatch):
    doc = object()
    monkeypatch.setattr(server.POLICY, "_active_doc", lambda: doc)
    monkeypatch.setattr(server.POLICY, "identity", lambda _: 123)
    monkeypatch.setattr(server.POLICY, "records", [])
    with pytest.raises(server.R.ToolError):
        server._require_managed()
    server.POLICY.records.append((123, False))
    assert server._require_managed(writable=False) is doc
    with pytest.raises(server.R.ToolError):
        server._require_managed(writable=True)
