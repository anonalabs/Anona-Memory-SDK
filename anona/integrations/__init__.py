"""Framework adapters for Anona Memory.

:class:`MemoryBridge` is the shared core every adapter (LangChain, LlamaIndex,
Google ADK, Microsoft Agent Framework, CrewAI, AWS Strands) is built on —
import it from here. ``anona.integrations._core`` still works and always
will (nothing is moving out from under it), but every doc page and every
adapter's own usage example treats the underscore-prefixed path as the
public one, which is exactly backwards: an underscore module name means
"internal, may change without notice." This re-export makes the path
actually match what it has always meant in practice.
"""
from __future__ import annotations

from ._core import MemoryBridge, require

__all__ = ["MemoryBridge", "require"]
