"""
Server configuration.

Resolution order, lowest priority first: built-in defaults, then
``solidworks_mcp.json`` (next to the package, or ``$SOLIDWORKS_MCP_CONFIG``),
then ``SOLIDWORKS_*`` environment variables. Environment wins so an MCP client
can override settings per-launch without editing files.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_NAME = "solidworks_mcp.json"


@dataclass
class Config:
    """Runtime settings for the SolidWorks MCP server."""

    #: Full path to ``SLDWORKS.exe``. Empty means auto-detect from the registry.
    exe_path: str = ""

    #: Start SolidWorks when no running instance is found.
    auto_launch: bool = True

    #: Keep the SolidWorks window visible. Strongly recommended: an invisible
    #: instance that raises a modal dialog cannot be recovered by the user.
    visible: bool = True

    #: Explicit opt-in: connection checks must not change dimension preferences.
    configure_dimension_input: bool = False

    #: Seconds to wait for a freshly launched SolidWorks to answer COM.
    startup_timeout: float = 180.0

    #: Seconds between connection attempts while waiting for startup.
    connect_retry_interval: float = 3.0

    #: Per-call timeout on the COM thread. Exceeding it means a modal dialog is
    #: almost certainly open; the worker survives so later calls still work.
    call_timeout: float = 300.0

    #: Default length unit for tool arguments. The COM layer is always metres;
    #: this only governs the tool boundary.
    default_unit: str = "mm"

    #: Rebuild after each geometry-changing feature. Slower but catches failures
    #: at the operation that caused them instead of several steps later.
    rebuild_after_features: bool = True

    #: Allow the ``execute_python`` escape hatch. Disable for shared/untrusted
    #: deployments -- it grants arbitrary code execution in the server process.
    allow_execute_python: bool = True

    #: Log file path. Empty disables file logging.
    log_file: str = "solidworks_mcp.log"

    #: Logging level name.
    log_level: str = "INFO"

    #: Decimal places for coordinates reported by the analysis tools.
    precision: int = 6

    #: Cap on entities returned by an unfiltered ``analyze_model`` faces/edges
    #: dump, to keep a 50k-face import from flooding the context window.
    max_entities: int = 2000

    _source: str = field(default="defaults", repr=False)

    def numeric_log_level(self) -> int:
        return getattr(logging, str(self.log_level).upper(), logging.INFO)

    def to_dict(self) -> dict:
        payload = asdict(self)
        payload.pop("_source", None)
        payload["config_source"] = self._source
        return payload


def _config_path() -> Path:
    override = os.environ.get("SOLIDWORKS_MCP_CONFIG")
    if override:
        return Path(override)
    return Path(__file__).resolve().parent / DEFAULT_CONFIG_NAME


_ENV_MAP = {
    "exe_path": ("SOLIDWORKS_EXE", str),
    "auto_launch": ("SOLIDWORKS_AUTO_LAUNCH", "bool"),
    "visible": ("SOLIDWORKS_VISIBLE", "bool"),
    "configure_dimension_input": ("SOLIDWORKS_CONFIGURE_DIMENSION_INPUT", "bool"),
    "startup_timeout": ("SOLIDWORKS_STARTUP_TIMEOUT", float),
    "call_timeout": ("SOLIDWORKS_CALL_TIMEOUT", float),
    "default_unit": ("SOLIDWORKS_DEFAULT_UNIT", str),
    "allow_execute_python": ("SOLIDWORKS_ALLOW_EXEC", "bool"),
    "log_level": ("SOLIDWORKS_LOG_LEVEL", str),
    "log_file": ("SOLIDWORKS_LOG_FILE", str),
    "precision": ("SOLIDWORKS_PRECISION", int),
    "max_entities": ("SOLIDWORKS_MAX_ENTITIES", int),
}

_TRUTHY = {"1", "true", "yes", "on"}
_FALSY = {"0", "false", "no", "off"}


def _coerce(raw: str, kind):
    if kind == "bool":
        lowered = raw.strip().lower()
        if lowered in _TRUTHY:
            return True
        if lowered in _FALSY:
            return False
        raise ValueError(f"expected a boolean, got {raw!r}")
    return kind(raw)


def load_config() -> Config:
    """Build the effective configuration from file plus environment."""
    values: dict = {}
    source = "defaults"

    path = _config_path()
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            known = {f.name for f in fields(Config)} - {"_source"}
            values.update({k: v for k, v in loaded.items() if k in known})
            source = str(path)
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Ignoring unreadable config %s: %s", path, exc)

    env_overrides = []
    for name, (env_var, kind) in _ENV_MAP.items():
        raw = os.environ.get(env_var)
        if raw is None:
            continue
        try:
            values[name] = _coerce(raw, kind)
            env_overrides.append(env_var)
        except (TypeError, ValueError) as exc:
            logger.warning("Ignoring %s=%r: %s", env_var, raw, exc)

    if env_overrides:
        source = f"{source} + env({', '.join(env_overrides)})"

    config = Config(**values)
    config._source = source
    return config


def save_config(config: Config, path: Path | None = None) -> Path:
    """Persist ``config`` as JSON and return the path written."""
    target = path or _config_path()
    payload = {k: v for k, v in asdict(config).items() if k != "_source"}
    target.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return target
