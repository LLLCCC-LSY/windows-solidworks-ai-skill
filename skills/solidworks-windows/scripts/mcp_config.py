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


def _table_path(line):
    if not line.lstrip().startswith("["):
        return None
    try:
        node = tomllib.loads(line)
    except tomllib.TOMLDecodeError:
        return None
    path = []
    while node:
        if isinstance(node, list) and len(node) == 1:
            node = node[0]
        elif isinstance(node, dict) and len(node) == 1:
            key, node = next(iter(node.items()))
            path.append(key)
        else:
            return None
    return tuple(path) if path else None


def _string_state(line, multiline):
    """Track TOML strings so a table-looking line in a value is preserved."""
    index = 0
    while index < len(line):
        if multiline:
            if multiline == '"""' and line[index] == "\\":
                index += 2
            elif line.startswith(multiline, index):
                quote = multiline[0]
                while index < len(line) and line[index] == quote:
                    index += 1
                multiline = None
            else:
                index += 1
        elif line[index] == "#":
            break
        elif line[index] in ('"', "'"):
            quote = line[index]
            if line.startswith(quote * 3, index):
                multiline = quote * 3
                index += 3
            else:
                index += 1
                while index < len(line):
                    if quote == '"' and line[index] == "\\":
                        index += 2
                    elif line[index] == quote:
                        index += 1
                        break
                    else:
                        index += 1
        else:
            index += 1
    return multiline


def _without_server_tables(original, server_id):
    retained, state, dropping = [], None, False
    for line in original.splitlines(keepends=True):
        header = _table_path(line) if state is None else None
        if header:
            dropping = header[:2] == ("mcp_servers", server_id)
        if not dropping:
            retained.append(line)
        state = _string_state(line, state)
    return "".join(retained)


def apply_config(config_path, snippet, server_id="solidworks", replace_existing=False):
    path = Path(config_path)
    original = path.read_text(encoding="utf-8-sig") if path.exists() else ""
    before = tomllib.loads(original)
    incoming = tomllib.loads(snippet)
    if set(incoming) != {"mcp_servers"} or set(incoming["mcp_servers"]) != {server_id}:
        raise ValueError("The snippet must contain only the selected MCP server.")
    desired = incoming["mcp_servers"][server_id]
    old = before.get("mcp_servers", {}).get(server_id)
    if old == desired:
        return {"changed": False, "config": str(path), "backup": None}
    if old is not None and not replace_existing:
        raise FileExistsError("This server is already configured. Review it, then use --replace-existing to replace only its tables.")
    candidate = _without_server_tables(original, server_id).rstrip() + "\n\n" + snippet
    checked = tomllib.loads(candidate)
    for key, value in before.items():
        if key != "mcp_servers":
            if checked.get(key) != value:
                raise ValueError("Refusing to change unrelated config settings.")
        else:
            if not all(checked[key].get(name) == settings for name, settings in value.items() if name != server_id):
                raise ValueError("Refusing to change another MCP server.")
    if checked["mcp_servers"][server_id] != desired:
        raise ValueError("The generated MCP entry failed validation.")
    backup = None
    if path.exists():
        backup = path.with_name(path.name + datetime.now().strftime(".before-solidworks-%Y%m%d-%H%M%S-%f.bak"))
        shutil.copy2(path, backup)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(candidate, encoding="utf-8", newline="\n")
    return {"changed": True, "config": str(path), "backup": str(backup) if backup else None}
