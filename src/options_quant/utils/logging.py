"""Logging utility placeholders."""

import logging


def get_logger(name: str) -> logging.Logger:
    """Return a standard library logger."""
    return logging.getLogger(name)
