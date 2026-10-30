"""Expose only read-only queries against the user's running SolidWorks session."""

import os

from workspace_config import WORKSPACE

# Set policy before importing the upstream server and its global configuration.
os.environ["SOLIDWORKS_AUTO_LAUNCH"] = "false"
os.environ["SOLIDWORKS_ALLOW_EXEC"] = "false"
os.environ["SOLIDWORKS_CONFIGURE_DIMENSION_INPUT"] = "false"
os.environ["SOLIDWORKS_LOG_FILE"] = str(WORKSPACE / "Reports" / "solidworks_mcp.log")

from mcp.server.fastmcp import FastMCP
from solidworks_mcp import server as upstream
from solidworks_mcp.util import result

mcp = FastMCP(
    "solidworks",
    instructions=(
        "Query-only SolidWorks connection. The user must start SolidWorks first. "
        "This server exposes only connect_solidworks, get_solidworks_info and "
        "list_documents. It cannot create, modify, save, close or export models. "
        "Check the JSON ok flag on every result. A capability being present is "
        "not proof that a modeling operation has been validated."
    ),
)


@mcp.tool()
def connect_solidworks() -> str:
    """Attach to the running SolidWorks process and report its version."""
    return upstream.connect_solidworks(launch=False)


@mcp.tool()
def get_solidworks_info() -> str:
    """Read the running SolidWorks version, constants and API member availability."""
    try:
        upstream.SESSION.ensure(launch=False)
        return upstream.get_solidworks_info()
    except Exception as exc:
        return result.from_exception("get_solidworks_info", exc)


@mcp.tool()
def list_documents() -> str:
    """List open document titles, paths and modification flags without changing them."""
    return upstream.list_documents()


if __name__ == "__main__":
    try:
        mcp.run(transport="stdio")
    finally:
        upstream.SESSION.shutdown()
