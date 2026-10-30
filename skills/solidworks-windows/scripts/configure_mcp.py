"""Print a reviewable MCP entry; --apply explicitly writes it with a backup."""

import argparse
import json
import os
from pathlib import Path

from mcp_config import apply_config, render


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--mode", choices=("query", "modeling"), default="query")
    parser.add_argument("--sw-exe", type=Path)
    parser.add_argument("--server-id", default="solidworks")
    parser.add_argument("--config", type=Path, default=Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "config.toml")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--replace-existing", action="store_true")
    args = parser.parse_args()
    snippet = render(args.workspace, args.mode, args.sw_exe, args.server_id)
    if not args.apply:
        print(snippet)
        return
    runtime = args.workspace / "Scripts" / ("solidworks_query_server.py" if args.mode == "query" else "solidworks_modeling_server.py")
    if not runtime.is_file() or not (args.workspace / ".venv/Scripts/python.exe").is_file():
        parser.error("Provision this workspace before applying its MCP registration.")
    print(json.dumps(apply_config(args.config, snippet, args.server_id, args.replace_existing), indent=2))


if __name__ == "__main__":
    main()
