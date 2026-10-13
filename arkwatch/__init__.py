"""ark-watch — US macro monitoring engine.

Layers: fetchers → parser/validator → SQLite (raw append-only) → transforms
→ signals → brief → outbox senders.
"""

from . import timezones

__version__ = "0.1.0"
__all__ = ["timezones"]
