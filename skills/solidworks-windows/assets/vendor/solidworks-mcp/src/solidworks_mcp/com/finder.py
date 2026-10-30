"""
Locate the SolidWorks installation and its document templates.

Version-agnostic on purpose: the registry is enumerated rather than probed for a
specific year, and template discovery walks whatever ``SOLIDWORKS <year>``
directories actually exist. That is what allows one build of this server to
drive SolidWorks 2018 through 2026 without a per-version code path.
"""

from __future__ import annotations

import glob
import logging
import os
from collections.abc import Iterable

logger = logging.getLogger(__name__)

try:
    import winreg

    HAS_WINREG = True
except ImportError:  # non-Windows, for offline tests
    winreg = None  # type: ignore[assignment]
    HAS_WINREG = False


#: Parent keys under which each release registers itself as a ``SOLIDWORKS <year>``
#: child. Verified against a SolidWorks 2023 install: the release key sits directly
#: under ``SOFTWARE\SolidWorks``, *not* nested under an extra ``SOLIDWORKS`` level,
#: and the install directory lives in its ``Setup`` subkey rather than in a value on
#: the release key itself.
_REGISTRY_ROOTS: tuple[tuple[int, str], ...] = ()
if HAS_WINREG:
    _REGISTRY_ROOTS = (
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\SolidWorks"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\SolidWorks"),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\SolidWorks"),
    )

#: Value names that have held the executable path or install directory across
#: releases. ``SolidWorks Folder`` (under ``Setup``) is what a 2023 install uses.
_EXE_VALUE_NAMES = ("SolidWorks Exe",)
_FOLDER_VALUE_NAMES = ("SolidWorks Folder", "InstallDir", "SolidWorks Path")

#: Fallback executable locations, for installs the registry does not describe.
_COMMON_EXE_GLOBS = (
    r"C:\Program Files\SOLIDWORKS Corp\SOLIDWORKS\SLDWORKS.exe",
    r"C:\Program Files\SolidWorks Corp\SolidWorks\SLDWORKS.exe",
    r"C:\Program Files\SOLIDWORKS Corp*\SOLIDWORKS\SLDWORKS.exe",
    r"D:\Program Files\SOLIDWORKS Corp\SOLIDWORKS\SLDWORKS.exe",
    r"?:\SOLIDWORKS Corp\SOLIDWORKS\SLDWORKS.exe",
)

_TEMPLATE_EXTENSIONS = {
    "part": ".prtdot",
    "assembly": ".asmdot",
    "drawing": ".drwdot",
}


def _registry_subkeys(key) -> list[str]:
    names = []
    index = 0
    while True:
        try:
            names.append(winreg.EnumKey(key, index))
        except OSError:
            break
        index += 1
    return names


def _read_value(key, names: Iterable[str]) -> str | None:
    """The first present value among ``names``, or ``None``."""
    for name in names:
        try:
            value, _ = winreg.QueryValueEx(key, name)
        except OSError:
            continue
        if value:
            return str(value)
    return None


def _executable_for_release(root, release: str) -> str | None:
    """Resolve a ``SOLIDWORKS <year>`` release key to its ``SLDWORKS.exe`` path.

    Three layouts appear across releases, so all are tried: an explicit executable
    value on the release key, an install-directory value on the release key, and --
    what a 2023 install actually uses -- an install-directory value in the release's
    ``Setup`` subkey.
    """
    try:
        with winreg.OpenKey(root, release) as release_key:
            exe = _read_value(release_key, _EXE_VALUE_NAMES)
            if exe:
                return exe

            folder = _read_value(release_key, _FOLDER_VALUE_NAMES)
            if not folder:
                try:
                    with winreg.OpenKey(release_key, "Setup") as setup_key:
                        exe = _read_value(setup_key, _EXE_VALUE_NAMES)
                        if exe:
                            return exe
                        folder = _read_value(setup_key, _FOLDER_VALUE_NAMES)
                except OSError:
                    folder = None

            if folder:
                return os.path.join(folder, "SLDWORKS.exe")
    except OSError:
        pass
    return None


def installed_versions() -> list[dict]:
    """Every SolidWorks install the registry describes, newest first.

    Returns:
        ``[{"version": "SOLIDWORKS 2023", "executable": "...", "exists": True}, ...]``
    """
    if not HAS_WINREG:
        return []

    found: list[dict] = []
    seen: set[str] = set()

    for hive, path in _REGISTRY_ROOTS:
        try:
            with winreg.OpenKey(hive, path) as root:
                releases = [
                    name for name in _registry_subkeys(root)
                    if name.upper().startswith("SOLIDWORKS")
                ]
                # Newest year first, so callers can take the first entry.
                for release in sorted(releases, reverse=True):
                    exe = _executable_for_release(root, release)
                    if not exe:
                        continue
                    normalised = os.path.normcase(os.path.abspath(exe))
                    if normalised in seen:
                        continue
                    seen.add(normalised)
                    found.append(
                        {"version": release, "executable": exe, "exists": os.path.exists(exe)}
                    )
        except OSError:
            continue

    return found


def find_solidworks_exe() -> str | None:
    """The best available ``SLDWORKS.exe`` path, or ``None``.

    Prefers the newest registered install that actually exists on disk, then
    falls back to well-known paths.
    """
    for entry in installed_versions():
        if entry["exists"]:
            logger.info("SolidWorks found via registry: %s", entry["executable"])
            return entry["executable"]

    for pattern in _COMMON_EXE_GLOBS:
        for candidate in sorted(_expand(pattern), reverse=True):
            if os.path.exists(candidate):
                logger.info("SolidWorks found at fallback path: %s", candidate)
                return candidate

    logger.warning("No SolidWorks installation located")
    return None


def _expand(pattern: str) -> Iterable[str]:
    """Expand a glob, including the ``?:`` drive-letter wildcard."""
    if pattern.startswith("?:"):
        for drive in "CDEFGH":
            yield from glob.glob(drive + pattern[1:])
    else:
        yield from glob.glob(pattern)


def template_directories() -> list[str]:
    """Candidate template directories, newest SolidWorks year first."""
    directories: list[str] = []

    program_data = os.environ.get("ProgramData", r"C:\ProgramData")
    for base in (
        os.path.join(program_data, "SolidWorks"),
        os.path.join(program_data, "SOLIDWORKS"),
    ):
        if not os.path.isdir(base):
            continue
        # Newest release directory first, so a machine with several years
        # installed picks templates matching the newest one.
        for entry in sorted(os.listdir(base), reverse=True):
            candidate = os.path.join(base, entry, "templates")
            if os.path.isdir(candidate):
                directories.append(candidate)
        if os.path.isdir(os.path.join(base, "templates")):
            directories.append(os.path.join(base, "templates"))

    exe = find_solidworks_exe()
    if exe:
        install_root = os.path.dirname(exe)
        for relative in ("data/templates", "lang/english/Tutorial", "lang/english/templates"):
            candidate = os.path.join(install_root, *relative.split("/"))
            if os.path.isdir(candidate):
                directories.append(candidate)

    return directories


def find_template(kind: str) -> str | None:
    """A template file for ``kind`` (``part`` | ``assembly`` | ``drawing``).

    Prefers a canonically-named template (``Part.prtdot``) over the first
    arbitrary match, so a folder full of company templates still yields the
    neutral default.
    """
    extension = _TEMPLATE_EXTENSIONS[kind]
    preferred_stem = kind.lower()
    fallback: str | None = None

    for directory in template_directories():
        try:
            entries = os.listdir(directory)
        except OSError:
            continue
        for name in sorted(entries):
            if not name.lower().endswith(extension):
                continue
            full = os.path.join(directory, name)
            if os.path.splitext(name)[0].lower() == preferred_stem:
                return full
            if fallback is None:
                fallback = full

    if fallback:
        return fallback

    # Last resort: a recursive sweep of the ProgramData tree.
    program_data = os.environ.get("ProgramData", r"C:\ProgramData")
    for base in (os.path.join(program_data, "SolidWorks"), os.path.join(program_data, "SOLIDWORKS")):
        if not os.path.isdir(base):
            continue
        matches = glob.glob(os.path.join(base, "**", f"*{extension}"), recursive=True)
        if matches:
            return sorted(matches)[0]

    return None
