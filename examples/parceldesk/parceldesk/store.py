"""A receipt and its stock movement must commit in the same SQLite transaction."""

import hashlib
import json
import sqlite3

PRODUCTS = [
    ("BAG-01", "Everyday tote", 20),
    ("MUG-02", "Studio mug", 12),
    ("TEE-03", "Cotton tee", 30),
]


def connect(path):
    connection = sqlite3.connect(path, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def initialize(path):
    connection = connect(path)
    try:
        with connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS stock (
                    sku TEXT PRIMARY KEY, name TEXT NOT NULL,
                    quantity INTEGER NOT NULL CHECK(quantity >= 0)
                );
                CREATE TABLE IF NOT EXISTS receipts (
                    event_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL,
                    kind TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS movements (
                    id INTEGER PRIMARY KEY, event_id TEXT NOT NULL, sku TEXT NOT NULL,
                    delta INTEGER NOT NULL, kind TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(sku) REFERENCES stock(sku)
                );
            """)
            connection.executemany(
                "INSERT INTO stock VALUES (?, ?, ?) ON CONFLICT(sku) DO NOTHING", PRODUCTS
            )
    finally:
        connection.close()


def claim_event(connection, event):
    fingerprint = hashlib.sha256(
        json.dumps(event, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    inserted = connection.execute(
        "INSERT INTO receipts(event_id, fingerprint, kind) VALUES (?, ?, ?) "
        "ON CONFLICT(event_id) DO NOTHING",
        (event["id"], fingerprint, event["kind"]),
    )
    if inserted.rowcount:
        return True
    previous = connection.execute(
        "SELECT fingerprint FROM receipts WHERE event_id = ?", (event["id"],)
    ).fetchone()
    if previous["fingerprint"] != fingerprint:
        raise ValueError("This event ID was already used with different data")
    return False


def adjust_stock(connection, event, delta):
    updated = connection.execute(
        "UPDATE stock SET quantity = quantity + ? WHERE sku = ? AND quantity + ? >= 0",
        (delta, event["sku"], delta),
    )
    if not updated.rowcount:
        raise ValueError("Unknown SKU or insufficient stock")
    connection.execute(
        "INSERT INTO movements(event_id, sku, delta, kind) VALUES (?, ?, ?, ?)",
        (event["id"], event["sku"], delta, event["kind"]),
    )


def snapshot(path):
    connection = connect(path)
    try:
        return {
            "stock": [dict(row) for row in connection.execute("SELECT * FROM stock ORDER BY sku")],
            "movements": [
                dict(row)
                for row in connection.execute("SELECT * FROM movements ORDER BY id DESC LIMIT 50")
            ],
            "receipts": connection.execute("SELECT count(*) FROM receipts").fetchone()[0],
        }
    finally:
        connection.close()
