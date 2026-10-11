"""Deterministic, dependency-light MVP for the Mishka phone.

This package intentionally does not import the legacy GPU voice pipeline.
"""

from .service import MishkaMVPService

__all__ = ["MishkaMVPService"]
