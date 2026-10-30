"""Provision an isolated Windows CAD workspace; default is a no-write dry run."""

import argparse
import json
import os
import shutil
import struct
import subprocess
import sys
import venv
from pathlib import Path

from mcp_config import render

SKILL_ROOT = Path(__file__).resolve().parents[1]


def source_files(workspace):
    root = Path(workspace).resolve()
    files = {}
    for source in (SKILL_ROOT / "assets/runtime").glob("*.py"):
        files[root / "Scripts" / source.name] = source
    vendor = SKILL_ROOT / "assets/vendor/solidworks-mcp"
    for source in vendor.rglob("*"):
        if source.is_file() and not any(part in {"__pycache__", ".pytest_cache", ".git"} or part.endswith(".egg-info") for part in source.relative_to(vendor).parts) and source.suffix != ".pyc":
            files[root / "vendor/solidworks-mcp" / source.relative_to(vendor)] = source
    return files


def prepare(workspace, mode="query", sw_exe=None):
    root = Path(workspace).resolve()
    files = source_files(root)
    # Check all conflicts before writing any workspace files.
    for destination, source in files.items():
        if destination.exists() and destination.read_bytes() != source.read_bytes():
            raise FileExistsError(f"Existing file differs: {destination}. Use a new workspace or review the update manually.")
    for directory in ("CAD", "Scripts", "Reports", "Backups"):
        (root / directory).mkdir(parents=True, exist_ok=True)
    for destination, source in files.items():
        if not destination.exists():
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
    snippet = render(root, mode, sw_exe)
    output = root / "Reports/solidworks-mcp.generated.toml"
    output.write_text(snippet, encoding="utf-8")
    return output


def check_existing_environment(workspace):
    root = Path(workspace).resolve()
    environment = root / ".venv"
    if not environment.exists():
        return
    if not environment.resolve().is_relative_to(root):
        raise ValueError("The existing .venv points outside the selected workspace.")
    executable = environment / "Scripts/python.exe"
    if not executable.is_file():
        if any(environment.iterdir()):
            raise FileExistsError("Existing .venv is incomplete; use a new workspace or repair it explicitly.")
        return
    code = "import json,struct,sys;print(json.dumps({'version':list(sys.version_info[:2]),'bits':struct.calcsize('P')*8,'prefix':sys.prefix,'is_venv':sys.prefix!=sys.base_prefix}))"
    completed = subprocess.run([str(executable), "-I", "-c", code], capture_output=True, text=True, check=True)
    info = json.loads(completed.stdout)
    if tuple(info["version"]) < (3, 12) or info["bits"] != 64:
        raise ValueError("Existing .venv must use 64-bit Python 3.12 or later.")
    if not info["is_venv"] or Path(info["prefix"]).resolve() != environment.resolve():
        raise ValueError("The interpreter does not belong to the selected workspace's .venv.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--mode", choices=("query", "modeling"), default="query")
    parser.add_argument("--sw-exe", type=Path)
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--dry-run", action="store_true")
    actions.add_argument("--prepare-only", action="store_true", help="Copy source and render config; do not create a venv or install.")
    actions.add_argument("--install", action="store_true", help="Copy source, create a venv and install dependencies; does not modify Codex config.")
    args = parser.parse_args()
    if not args.workspace.is_absolute():
        parser.error("--workspace must be an absolute path.")
    root = args.workspace.resolve()
    plan = {"workspace": str(root), "mode": args.mode,
            "runtime_files": len(source_files(root)), "install_dependencies": args.install,
            "codex_config_changed": False, "solidworks_started": False}
    print(json.dumps(plan, ensure_ascii=False, indent=2))
    if args.dry_run or not (args.prepare_only or args.install):
        return
    if sys.platform != "win32":
        parser.error("Installation is supported only on Windows.")
    if sys.version_info < (3, 12) or struct.calcsize("P") != 8:
        parser.error("Run this helper with 64-bit Python 3.12 or later.")
    # Validate the optional executable before copying files.
    render(root, args.mode, args.sw_exe)
    if args.install:
        check_existing_environment(root)
    output = prepare(root, args.mode, args.sw_exe)
    if args.install:
        executable = root / ".venv/Scripts/python.exe"
        if not executable.exists():
            venv.EnvBuilder(with_pip=True).create(root / ".venv")
        check_existing_environment(root)
        subprocess.run([str(executable), "-m", "pip", "install", "-e", str(root / "vendor/solidworks-mcp"), "mcp==1.30.0", "pywin32==312"], check=True)
        subprocess.run([str(executable), "-m", "pip", "check"], check=True)
    print(f"Prepared {output}. Review it, then use configure_mcp.py to apply the entry.")


if __name__ == "__main__":
    main()
