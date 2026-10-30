"""SolidWorks operations, grouped by domain.

Every function here runs on the STA COM thread and is called by ``server.py``
through the marshalling decorator. They raise ``ToolError`` or ``SolidWorksError``
for expected failures; the server converts those into the result envelope.
"""

from . import (
    analysis,
    assemblies,
    documents,
    drawings,
    features,
    scripting,
    sheetmetal,
    sketches,
)

__all__ = [
    "analysis",
    "assemblies",
    "documents",
    "drawings",
    "features",
    "scripting",
    "sheetmetal",
    "sketches",
]
