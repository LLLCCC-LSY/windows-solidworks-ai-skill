import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills/solidworks-windows"
os.environ["SW_AI_WORKSPACE"] = str(Path(tempfile.gettempdir()) / "solidworks-skill-offline-tests")
os.environ["SOLIDWORKS_AUTO_LAUNCH"] = "false"
os.environ["SOLIDWORKS_ALLOW_EXEC"] = "false"
os.environ["SOLIDWORKS_CONFIGURE_DIMENSION_INPUT"] = "false"
os.environ["SOLIDWORKS_ENABLE_EQUATIONS"] = "false"
for folder in (SKILL / "scripts", SKILL / "assets/runtime", SKILL / "assets/vendor/solidworks-mcp/src"):
    sys.path.insert(0, str(folder))
