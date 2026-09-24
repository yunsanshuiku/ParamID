"""Reusable offline and online parameter identification core."""

from .core import IdentificationError, ModelSpec, ModelStream, RLS, fit_linear

__all__ = ["IdentificationError", "ModelSpec", "ModelStream", "RLS", "fit_linear"]
__version__ = "0.7.0"
