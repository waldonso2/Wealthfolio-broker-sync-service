"""Registry of broker adapters. A new broker is one module plus one line here."""

from __future__ import annotations

from .base import AdapterError, AuthRequired, BrokerAdapter, Challenge, CredentialField
from .dkb import DkbAdapter
from .dummy import DummyAdapter

ADAPTERS: dict[str, type[BrokerAdapter]] = {
    DkbAdapter.key: DkbAdapter,
    DummyAdapter.key: DummyAdapter,
}

__all__ = [
    "ADAPTERS",
    "AdapterError",
    "AuthRequired",
    "BrokerAdapter",
    "Challenge",
    "CredentialField",
]
