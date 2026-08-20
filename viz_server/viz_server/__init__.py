"""Bitemporal market-replay backend for the PJM viz tool.

Read-only consumer of `pjm_engine`. Boots a FastAPI server bound to 127.0.0.1
that pre-loads every registered feed at startup, wraps each in a CachedView,
and serves bitemporal slices over Arrow IPC.

The engine is the single source of truth — this package never mutates engine
state, never patches loaders, never reaches into private attributes.
"""

__version__ = "0.1.0"
