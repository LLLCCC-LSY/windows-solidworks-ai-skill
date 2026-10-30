import json
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

import mcp_config
import setup_workspace


def test_default_setup_dry_run_makes_no_workspace(tmp_path):
    target = tmp_path / "dry-run"
    script = setup_workspace.SKILL_ROOT / "scripts/setup_workspace.py"
    completed = subprocess.run([sys.executable, str(script), "--workspace", str(target)], capture_output=True, text=True, check=True)
    assert json.loads(completed.stdout)["codex_config_changed"] is False
    assert not target.exists()


def test_prepare_is_relocatable_idempotent_and_refuses_modified_runtime(tmp_path):
    root = tmp_path / "CAD work area"
    snippet = setup_workspace.prepare(root, "modeling")
    data = tomllib.loads(snippet.read_text(encoding="utf-8"))["mcp_servers"]["solidworks"]
    assert Path(data["args"][0]) == root / "Scripts/solidworks_modeling_server.py"
    assert data["env"]["SW_AI_WORKSPACE"] == str(root)
    assert len(data["enabled_tools"]) == 19
    setup_workspace.prepare(root, "modeling")
    runtime = root / "Scripts/solidworks_modeling_server.py"
    runtime.write_text("# user modification\n", encoding="utf-8")
    with pytest.raises(FileExistsError):
        setup_workspace.prepare(root, "modeling")
    assert runtime.read_text() == "# user modification\n"


def test_config_backup_preserves_unrelated_servers_and_user_settings(tmp_path):
    config = tmp_path / "config.toml"
    original = 'model = "example-model"\n[mcp_servers.other]\ncommand = "existing-command"\n[mcp_servers.other.env]\nEXAMPLE_SETTING = "preserve-me"\n'
    config.write_text(original, encoding="utf-8")
    snippet = mcp_config.render(tmp_path / "workspace", "query")
    result = mcp_config.apply_config(config, snippet)
    assert Path(result["backup"]).read_text() == original
    parsed = tomllib.loads(config.read_text())
    assert parsed["model"] == "example-model"
    assert parsed["mcp_servers"]["other"]["env"] == {"EXAMPLE_SETTING":"preserve-me"}
    assert mcp_config.apply_config(config, snippet)["changed"] is False
    with pytest.raises(FileExistsError):
        mcp_config.apply_config(config, mcp_config.render(tmp_path / "workspace", "modeling"))


def test_explicit_query_to_modeling_upgrade_keeps_other_tables(tmp_path):
    config = tmp_path / "config.toml"
    mcp_config.apply_config(config, mcp_config.render(tmp_path, "query"))
    with config.open("a", encoding="utf-8") as output:
        output.write('\n[projects."example"]\ntrust_level = "trusted"\n')
    mcp_config.apply_config(config, mcp_config.render(tmp_path, "modeling"), replace_existing=True)
    parsed = tomllib.loads(config.read_text())
    assert parsed["projects"]["example"]["trust_level"] == "trusted"
    assert len(parsed["mcp_servers"]["solidworks"]["enabled_tools"]) == 19


def test_python312_reserved_device_name_fallback(monkeypatch):
    import workspace_config
    monkeypatch.delattr(workspace_config.os.path, "isreserved", raising=False)
    assert workspace_config.is_reserved_path(Path(r"C:\work\CON.SLDPRT"))
    assert not workspace_config.is_reserved_path(Path(r"C:\work\plate.SLDPRT"))


def test_quoted_indented_server_headers_can_be_upgraded(tmp_path):
    config = tmp_path / "quoted.toml"
    config.write_text('  [mcp_servers."solidworks"] # comment\ncommand="old"\n[mcp_servers."solidworks".env]\nOLD="value"\n', encoding="utf-8")
    mcp_config.apply_config(config, mcp_config.render(tmp_path, "modeling"), replace_existing=True)
    data = tomllib.loads(config.read_text())
    assert len(data["mcp_servers"]["solidworks"]["enabled_tools"]) == 19


def test_table_looking_multiline_values_are_not_removed(tmp_path):
    config = tmp_path / "multiline.toml"
    original = 'instructions = """\n[mcp_servers.solidworks]\nThis is user text, not a table.\n"""\n[mcp_servers.solidworks]\ncommand="old"\n[[examples]]\nname="preserve"\n'
    config.write_text(original, encoding="utf-8")
    mcp_config.apply_config(config, mcp_config.render(tmp_path), replace_existing=True)
    before, after = tomllib.loads(original), tomllib.loads(config.read_text())
    assert before["instructions"] == after["instructions"]
    assert before["examples"] == after["examples"]


def test_config_snippet_cannot_add_other_settings(tmp_path):
    config = tmp_path / "config.toml"
    snippet = 'model="unexpected"\n' + mcp_config.render(tmp_path)
    with pytest.raises(ValueError, match="only the selected"):
        mcp_config.apply_config(config, snippet)
    assert not config.exists()


@pytest.mark.parametrize("name", ["COM¹.SLDPRT", "LPT².SLDPRT", "AUX .SLDPRT", "plate?.SLDPRT"])
def test_python312_reserved_name_fallback_rejects_device_aliases(monkeypatch, name):
    import workspace_config
    monkeypatch.delattr(workspace_config.os.path, "isreserved", raising=False)
    assert workspace_config.is_reserved_path(Path(r"C:\work") / name)


@pytest.mark.parametrize("version,bits", [([3,11],64), ([3,12],32)])
def test_incompatible_existing_venv_is_rejected_before_writes(tmp_path, monkeypatch, version, bits):
    root = tmp_path / "workspace"
    python = root / ".venv/Scripts/python.exe"
    python.parent.mkdir(parents=True)
    python.touch()
    info = {"version":version,"bits":bits,"prefix":str(root/".venv"),"is_venv":True}
    monkeypatch.setattr(setup_workspace.subprocess, "run", lambda *a,**kw: subprocess.CompletedProcess(a,0,json.dumps(info)))
    monkeypatch.setattr(sys,"argv",["setup_workspace.py","--workspace",str(root),"--install"])
    with pytest.raises(ValueError, match="Existing .venv"):
        setup_workspace.main()
    assert not (root/"Scripts").exists()


def test_existing_venv_cannot_silently_use_another_project_interpreter(tmp_path,monkeypatch):
    python = tmp_path/".venv/Scripts/python.exe"
    python.parent.mkdir(parents=True)
    python.touch()
    info={"version":[3,14],"bits":64,"prefix":str(tmp_path/"other-project"),"is_venv":True}
    monkeypatch.setattr(setup_workspace.subprocess,"run",lambda *a,**kw:subprocess.CompletedProcess(a,0,json.dumps(info)))
    with pytest.raises(ValueError,match="does not belong"):
        setup_workspace.check_existing_environment(tmp_path)
