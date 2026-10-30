#!/usr/bin/env python
"""
Launch the SolidWorks MCP server without installing the package.

Convenient for an MCP client config that points at a source checkout:

    {
      "mcpServers": {
        "solidworks": {
          "command": "python",
          "args": ["C:/solidworks-mcp/run_server.py"]
        }
      }
    }

After ``pip install -e .`` the ``solidworks-mcp`` console script does the same job.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from solidworks_mcp.server import main  # noqa: E402

if __name__ == "__main__":
    main()
