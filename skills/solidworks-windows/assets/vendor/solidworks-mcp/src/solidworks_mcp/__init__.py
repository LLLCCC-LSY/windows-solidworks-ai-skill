"""
SolidWorks MCP Server.

A Model Context Protocol server that drives SolidWorks through its COM API from
pure Python, with no version-locked interop assemblies. Supports SolidWorks 2018
through 2026 from one codebase.
"""

__version__ = "1.0.0"

#: SolidWorks releases this server is built and tested against. The compatibility
#: layer resolves API differences at runtime rather than at build time, so newer
#: releases generally work without changes here.
SUPPORTED_SOLIDWORKS_YEARS = (2018, 2019, 2020, 2021, 2022, 2023, 2024, 2025, 2026)

__all__ = ["__version__", "SUPPORTED_SOLIDWORKS_YEARS"]
