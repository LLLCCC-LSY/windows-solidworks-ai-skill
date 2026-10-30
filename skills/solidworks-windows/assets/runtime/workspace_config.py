"""Resolve this deployment's workspace without a machine-specific install path."""

import os
from pathlib import Path

WORKSPACE = Path(os.environ.get("SW_AI_WORKSPACE", str(Path(__file__).resolve().parents[1]))).resolve()


def is_reserved_path(path):
    native = getattr(os.path, "isreserved", None)
    if native is not None:
        return native(str(path))
    # os.path.isreserved was added in Python 3.13; support Python 3.12 too.
    devices = {"CON", "PRN", "AUX", "NUL"} | {f"{prefix}{number}" for prefix in ("COM", "LPT") for number in range(1, 10)}
    return any(part.rstrip(" .").split(".", 1)[0].upper() in devices for part in Path(path).parts[1:])
