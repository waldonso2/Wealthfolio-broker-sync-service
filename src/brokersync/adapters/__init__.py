"""Registry of broker adapters. A new broker is one module plus one line here."""

from __future__ import annotations

from .base import AdapterError, AuthRequired, BrokerAdapter, Challenge, CredentialField
from .comdirect import ComdirectAdapter
from .deutschebank import DeutscheBankAdapter
from .dkb import DkbAdapter
from .enablebanking import BANKS as PSD2_BANKS
from .fintsbank import BANKS as FINTS_BANKS
from .scalable import ScalableAdapter
from .tr import TradeRepublicAdapter

ADAPTERS: dict[str, type[BrokerAdapter]] = {
    DkbAdapter.key: DkbAdapter,
    TradeRepublicAdapter.key: TradeRepublicAdapter,
    ScalableAdapter.key: ScalableAdapter,
    ComdirectAdapter.key: ComdirectAdapter,
    DeutscheBankAdapter.key: DeutscheBankAdapter,
    **{a.key: a for a in FINTS_BANKS},
    **{a.key: a for a in PSD2_BANKS},
}

__all__ = [
    "ADAPTERS",
    "AdapterError",
    "AuthRequired",
    "BrokerAdapter",
    "Challenge",
    "CredentialField",
]
