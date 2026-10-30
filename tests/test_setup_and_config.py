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
