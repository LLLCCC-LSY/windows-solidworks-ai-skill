"""
Unit conversion at the tool boundary.

The SolidWorks API is metres-and-radians internally, with no exceptions. Exposing
that directly (as the C# reference project does) makes every tool call an
error-prone manual conversion -- ``0.05`` for a 50 mm extrude, and a silent
50-metre part when someone forgets. Exposing only millimetres (as the Python
reference project does) is friendlier but loses the ability to work in inches.

So: tool arguments carry an optional ``unit``, the default is configurable
(``mm``), and everything crossing into COM is converted to SI here. Values coming
back out are reported in **both** SI and the display unit, so a caller can verify
either way.
"""

from __future__ import annotations

import math
from enum import Enum


class Unit(str, Enum):
    METER = "m"
    CENTIMETER = "cm"
    MILLIMETER = "mm"
    MICRON = "um"
    INCH = "in"
    FOOT = "ft"


#: Multiply a value in this unit by the factor to obtain metres.
TO_METERS: dict[Unit, float] = {
    Unit.METER: 1.0,
    Unit.CENTIMETER: 1e-2,
    Unit.MILLIMETER: 1e-3,
    Unit.MICRON: 1e-6,
    Unit.INCH: 0.0254,
    Unit.FOOT: 0.3048,
}

ALIASES: dict[str, Unit] = {
    "m": Unit.METER, "meter": Unit.METER, "meters": Unit.METER, "metre": Unit.METER,
    "metres": Unit.METER, "si": Unit.METER,
    "cm": Unit.CENTIMETER, "centimeter": Unit.CENTIMETER, "centimeters": Unit.CENTIMETER,
    "mm": Unit.MILLIMETER, "millimeter": Unit.MILLIMETER, "millimeters": Unit.MILLIMETER,
    "millimetre": Unit.MILLIMETER, "millimetres": Unit.MILLIMETER,
    "um": Unit.MICRON, "micron": Unit.MICRON, "microns": Unit.MICRON, "micrometer": Unit.MICRON,
    "in": Unit.INCH, "inch": Unit.INCH, "inches": Unit.INCH, '"': Unit.INCH,
    "ft": Unit.FOOT, "foot": Unit.FOOT, "feet": Unit.FOOT, "'": Unit.FOOT,
}


class UnknownUnitError(ValueError):
    """Raised for an unrecognised unit string.

    Deliberately loud: the reference implementation logged a warning and silently
    fell back to millimetres, which turns a typo into wrong geometry.
    """

    def __init__(self, value):
        super().__init__(
            f"Unknown unit {value!r}. Supported: "
            + ", ".join(sorted({u.value for u in Unit}))
            + " (aliases such as 'inch', 'millimeters' also work)."
        )


def parse_unit(value: str | Unit | None, default: Unit = Unit.MILLIMETER) -> Unit:
    """Resolve a unit string to a :class:`Unit`, or raise :class:`UnknownUnitError`."""
    if value is None or value == "":
        return default
    if isinstance(value, Unit):
        return value
    try:
        return ALIASES[str(value).strip().lower()]
    except KeyError:
        raise UnknownUnitError(value) from None


class UnitConverter:
    """Converts between a caller-facing length unit and SolidWorks' metres."""

    def __init__(self, default_unit: str | Unit = Unit.MILLIMETER):
        self._default = parse_unit(default_unit)

    @property
    def default(self) -> Unit:
        return self._default

    @default.setter
    def default(self, value: str | Unit) -> None:
        self._default = parse_unit(value)

    def to_meters(self, value: float, unit: str | Unit | None = None) -> float:
        """Convert a caller-supplied length to metres for the COM call."""
        return float(value) * TO_METERS[parse_unit(unit, self._default)]

    def from_meters(self, value: float, unit: str | Unit | None = None) -> float:
        """Convert a COM length in metres back to the display unit."""
        return float(value) / TO_METERS[parse_unit(unit, self._default)]

    def points_to_meters(
        self, values, unit: str | Unit | None = None
    ) -> list[float]:
        """Convert a flat coordinate sequence to metres, preserving order."""
        factor = TO_METERS[parse_unit(unit, self._default)]
        return [float(v) * factor for v in values]

    def describe(self, meters: float, unit: str | Unit | None = None) -> dict:
        """Report a length both in SI and in the display unit.

        Feature tools return this for their driving dimensions so a caller can
        verify the result without knowing which unit convention was in play.
        """
        resolved = parse_unit(unit, self._default)
        return {
            "meters": round(float(meters), 12),
            "value": round(self.from_meters(meters, resolved), 9),
            "unit": resolved.value,
        }


def deg_to_rad(degrees: float) -> float:
    """Degrees to radians.

    Angles cross the tool boundary in **degrees** everywhere in this server --
    consistently, unlike the reference projects, where some tools took degrees
    and others radians.
    """
    return math.radians(float(degrees))


def rad_to_deg(radians: float) -> float:
    """Radians to degrees."""
    return math.degrees(float(radians))


def snap_full_turn(radians: float, tolerance: float = 1e-3) -> float:
    """Snap a near-full revolution to exactly 2*pi.

    A 360-degree revolve computed through floating point lands a hair short of a
    full turn, which SolidWorks renders as a hairline seam face. Snapping keeps
    "full revolve" clean without affecting genuine partial revolves.
    """
    if abs(radians - 2.0 * math.pi) < tolerance:
        return 2.0 * math.pi
    return radians
