"""Warehouse handlers. Retry protection is implemented during recorded tasks."""

import re

from .store import adjust_stock, claim_event, connect


def receive(connection, event):
    with connection:
        if not claim_event(connection, event):
            return {"status": "duplicate", "event_id": event["id"]}
        adjust_stock(connection, event, event["quantity"])
    return {"status": "processed", "event_id": event["id"]}


def dispatch(connection, event):
    with connection:
        if not claim_event(connection, event):
            return {"status": "duplicate", "event_id": event["id"]}
        adjust_stock(connection, event, -event["quantity"])
    return {"status": "processed", "event_id": event["id"]}


def returned(connection, event):
    with connection:
        if not claim_event(connection, event):
            return {"status": "duplicate", "event_id": event["id"]}
        adjust_stock(connection, event, event["quantity"])
    return {"status": "processed", "event_id": event["id"]}


def damaged(connection, event):
    with connection:
        if not claim_event(connection, event):
            return {"status": "duplicate", "event_id": event["id"]}
        adjust_stock(connection, event, -event["quantity"])
    return {"status": "processed", "event_id": event["id"]}


HANDLERS = {"received": receive, "dispatched": dispatch, "returned": returned, "damaged": damaged}


def process(path, data):
    if not isinstance(data, dict) or set(data) != {"id", "kind", "sku", "quantity"}:
        raise ValueError("Supply id, kind, sku and quantity")
    for key in ("id", "sku"):
        if not isinstance(data[key], str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,120}", data[key]):
            raise ValueError(f"Invalid {key}")
    if not isinstance(data["kind"], str) or data["kind"] not in HANDLERS:
        raise ValueError("Unknown event kind")
    if type(data["quantity"]) is not int or not 0 < data["quantity"] <= 1_000_000:
        raise ValueError("Quantity must be a positive integer")
    connection = connect(path)
    try:
        return HANDLERS[data["kind"]](connection, data)
    finally:
        connection.close()
