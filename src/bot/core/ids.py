"""Deterministic client order IDs.

Every order intent gets a UUID that is stored before anything is sent. The
Binance client order ID is derived from it, so a resend after a timeout reuses
the same ID and Binance rejects it as a duplicate instead of placing a second order.
"""
from __future__ import annotations

import re
import uuid

# Binance allows 1-36 characters from this set for newClientOrderId / listClientOrderId.
_BINANCE_ID = re.compile(r"^[.A-Z:/a-z0-9_-]{1,36}$")

_KIND_PREFIX = {"entry": "e", "protect": "p", "exit": "x", "list": "l"}


def new_intent_id() -> uuid.UUID:
    return uuid.uuid4()


def client_order_id(intent_id: uuid.UUID, kind: str, leg: int = 0) -> str:
    """Same (intent, kind, leg) always gives the same ID."""
    if kind not in _KIND_PREFIX:
        raise ValueError(f"unknown order kind: {kind}")
    if not 0 <= leg <= 9:
        raise ValueError("leg must be 0-9")
    cid = f"sb{_KIND_PREFIX[kind]}{leg}{intent_id.hex[:32]}"
    assert _BINANCE_ID.match(cid), cid
    return cid


def is_valid_client_id(cid: str) -> bool:
    return bool(_BINANCE_ID.match(cid))
