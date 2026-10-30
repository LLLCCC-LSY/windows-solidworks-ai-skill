"""Render and update a single local STDIO registration; preserve other settings."""

import json
import re
import shutil
import tomllib
from datetime import datetime
from pathlib import Path

QUERY_TOOLS = ["connect_solidworks", "get_solidworks_info", "list_documents"]
MODELING_TOOLS = sorted(QUERY_TOOLS + [
    "new_document", "open_document", "close_document", "save_document",
    "create_sketch", "edit_sketch", "exit_sketch", "add_sketch_entities",
    "add_sketch_constraint", "add_sketch_dimension", "extrude_feature",
    "modify_dimension", "rebuild_document", "analyze_model",
    "create_parametric_plate", "set_plate_parameters",
])


def server_settings(workspace, mode="query", sw_exe=None):
    root = Path(workspace).resolve()
    if mode not in {"query", "modeling"}:
        raise ValueError("Mode must be query or modeling.")
    entry = "solidworks_query_server.py" if mode == "query" else "solidworks_modeling_server.py"
    env = {
        "SW_AI_WORKSPACE": str(root), "SOLIDWORKS_ALLOW_EXEC": "false",
        "SOLIDWORKS_AUTO_LAUNCH": "false", "SOLIDWORKS_VISIBLE": "true",
        "SOLIDWORKS_CONFIGURE_DIMENSION_INPUT": "false", "SOLIDWORKS_ENABLE_EQUATIONS": "false",
        "SOLIDWORKS_DEFAULT_UNIT": "mm",
        "SOLIDWORKS_LOG_FILE": str(root / "Reports/solidworks_mcp.log"),
    }
    if sw_exe:
        executable = Path(sw_exe).resolve()
        if not executable.is_file() or executable.name.lower() != "sldworks.exe":
            raise ValueError("--sw-exe must name an existing SLDWORKS.exe.")
        env["SOLIDWORKS_EXE"] = str(executable)
    return {"command": str(root / ".venv/Scripts/python.exe"),
            "args": [str(root / "Scripts" / entry)], "cwd": str(root), "enabled": True,
            "startup_timeout_sec": 30, "tool_timeout_sec": 60,
            "enabled_tools": QUERY_TOOLS if mode == "query" else MODELING_TOOLS, "env": env}


def render(workspace, mode="query", sw_exe=None, server_id="solidworks"):
    if not re.fullmatch(r"[a-z][a-z0-9_]*", server_id):
        raise ValueError("Invalid server ID.")
    data = server_settings(workspace, mode, sw_exe)
    lines = [f"[mcp_servers.{server_id}]"]
    for key, value in data.items():
        if key == "env":
            continue
        lines.append(f"{key} = {json.dumps(value, ensure_ascii=False)}")
    lines += ["", f"[mcp_servers.{server_id}.env]"]
    lines += [f"{key} = {json.dumps(value, ensure_ascii=False)}" for key, value in data["env"].items()]
    text = "\n".join(lines) + "\n"
    assert tomllib.loads(text)["mcp_servers"][server_id] == data
    return text


def apply_config(config_path, snippet, server_id="solidworks", replace_existing=False):
    path = Path(config_path)
    original = path.read_text(encoding="utf-8-sig") if path.exists() else ""
    before = tomllib.loads(original)
    desired = tomllib.loads(snippet)["mcp_servers"][server_id]
    old = before.get("mcp_servers", {}).get(server_id)
    if old == desired:
        return {"changed": False, "config": str(path), "backup": None}
    if old is not None and not replace_existing:
        raise FileExistsError("This server is already configured. Review it, then use --replace-existing to replace only its tables.")
    prefix = f"mcp_servers.{server_id}"
    chunks = re.split(r"(?m)(?=^\[)", original)
    retained = []
    for chunk in chunks:
        header = re.match(r"\[([^\]\r\n]+)\]", chunk)
        if header and (header[1] == prefix or header[1].startswith(prefix + ".")):
            continue
        retained.append(chunk)
    candidate = "".join(retained).rstrip() + "\n\n" + snippet
    checked = tomllib.loads(candidate)
    for key, value in before.items():
        if key != "mcp_servers":
            assert checked[key] == value
        else:
            assert all(checked[key][name] == settings for name, settings in value.items() if name != server_id)
    assert checked["mcp_servers"][server_id] == desired
    backup = None
    if path.exists():
        backup = path.with_name(path.name + datetime.now().strftime(".before-solidworks-%Y%m%d-%H%M%S-%f.bak"))
        shutil.copy2(path, backup)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(candidate, encoding="utf-8", newline="\n")
    return {"changed": True, "config": str(path), "backup": str(backup) if backup else None}
