"""Deterministic client order IDs.

Every order intent gets a UUID that is stored before anything is sent. The
Binance client order ID is derived from it, so after a timeout the order can be
looked up by that ID. A resend is NOT safe: Binance rejects a duplicate ID only
while the first order is still open, and accepts it again once that one has
filled (Spot API docs, New order). So the trading adapter never re-sends an order
whose outcome is unknown; it looks the order up instead (bot.exchange.trading).
"""
from __future__ import annotations

import re
import uuid

# Binance allows 1-36 characters from this set for newClientOrderId / listClientOrderId.
_BINANCE_ID = re.compile(r"^[.A-Z:/a-z0-9_-]{1,36}$")

_KIND_PREFIX = {"entry": "e", "protect": "p", "exit": "x", "list": "l"}
_BOT_ID = re.compile(r"^sb[epxl][0-9][0-9a-f]{32}$")


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


def is_bot_client_id(cid: str) -> bool:
    """True if the ID has the shape of one made by `client_order_id` (whatever ledger it belongs to)."""
    return bool(_BOT_ID.match(cid))
