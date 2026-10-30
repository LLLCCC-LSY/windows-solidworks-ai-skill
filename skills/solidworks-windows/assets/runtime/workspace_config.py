"""Resolve this deployment's workspace without a machine-specific install path."""

import os
from pathlib import Path, PureWindowsPath

WORKSPACE = Path(os.environ.get("SW_AI_WORKSPACE", str(Path(__file__).resolve().parents[1]))).resolve()


def is_reserved_path(path):
    native = getattr(os.path, "isreserved", None)
    if native is not None:
        return native(str(path))
    # os.path.isreserved was added in Python 3.13; support Python 3.12 too.
    devices = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"} | {
        f"{prefix}{number}" for prefix in ("COM", "LPT") for number in "123456789¹²³"
    }
    windows = PureWindowsPath(path)
    for part in windows.parts:
        if part == windows.anchor or part in (".", ".."):
            continue
        if part.endswith((" ", ".")) or any(ord(char) < 32 or char in '"*:<>?|/\\' for char in part):
            return True
        if part.split(".", 1)[0].rstrip(" ").upper() in devices:
            return True
    return False
