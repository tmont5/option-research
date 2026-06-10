"""Repository interface placeholders."""

from typing import Protocol


class InstrumentRepository(Protocol):
    """Boundary for instrument persistence/query implementations."""
