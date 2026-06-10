"""Instrument model placeholders."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Instrument:
    """Generic tradable instrument identifier."""

    symbol: str
