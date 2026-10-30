"""Validate STDIO handshake and tools; optionally perform read-only live queries."""

import argparse
import asyncio
import json
from datetime import timedelta
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from mcp_config import server_settings


async def probe(workspace, mode="query", live=False):
    settings = server_settings(workspace, mode)
    params = StdioServerParameters(command=settings["command"], args=settings["args"],
        cwd=settings["cwd"], env=settings["env"])
    report = {"mode":mode, "calls_read_only":True, "live_queries":live, "ok":False}
    async with stdio_client(params) as (reader, writer):
        async with ClientSession(reader, writer, read_timeout_seconds=timedelta(seconds=45)) as client:
            info = await client.initialize()
            names = sorted(tool.name for tool in (await client.list_tools()).tools)
            if set(names) != set(settings["enabled_tools"]):
                raise RuntimeError(f"Unexpected tools: {names}")
            report.update(server=info.serverInfo.model_dump(mode="json"), tools=names)
            if live:
                report["queries"] = {}
                for tool in ("connect_solidworks", "get_solidworks_info", "list_documents"):
                    response = await client.call_tool(tool, {})
                    if response.isError:
                        raise RuntimeError(str(response))
                    result = json.loads(next(block.text for block in response.content if block.type == "text"))
                    report["queries"][tool] = result
                    if not result["ok"]:
                        return report
    report["ok"] = True
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--mode", choices=("query", "modeling"), default="query")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--save-report", action="store_true")
    args = parser.parse_args()
    result = asyncio.run(asyncio.wait_for(probe(args.workspace, args.mode, args.live), timeout=150))
    text = json.dumps(result, ensure_ascii=False, indent=2)
    print(text)
    if args.save_report:
        reports = args.workspace / "Reports"
        reports.mkdir(parents=True, exist_ok=True)
        (reports / f"mcp-{args.mode}-probe.json").write_text(text+"\n", encoding="utf-8")
    raise SystemExit(0 if result["ok"] else 1)


if __name__ == "__main__":
    main()
