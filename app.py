from __future__ import annotations
import argparse
import sqlite3
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from collections import deque
from typing import Optional, List, Dict, Any, Tuple
import json
import os
from tabulate import tabulate

DB_PATH = os.environ.get("FLASHCARDS_DB", "flashcards.db")

def now_ts() -> str:
    return datetime.utcnow().isoformat(timespec="seconds")

def days_from_now(n: float) -> str:
    return (datetime.utcnow() + timedelta(days=n)).isoformat(timespec="seconds")

def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db() -> None:
    conn = get_conn()
    cur = conn.cursor()
    cur.executescript(
        """
        PRAGMA foreign_keys = ON;

        CREATE TABLE IF NOT EXISTS decks (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            parent_id INTEGER,
            UNIQUE(name),
            FOREIGN KEY(parent_id) REFERENCES decks(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS cards (
            id INTEGER PRIMARY KEY,
            deck_id INTEGER NOT NULL,
            front TEXT NOT NULL,
            back TEXT NOT NULL,
            tags TEXT DEFAULT '',
            easiness REAL DEFAULT 2.5,
            interval REAL DEFAULT 0,
            repetitions INTEGER DEFAULT 0,
            due_ts TEXT DEFAULT NULL,
            last_review_ts TEXT DEFAULT NULL,
            successes INTEGER DEFAULT 0,
            failures INTEGER DEFAULT 0,
            created_ts TEXT NOT NULL,
            updated_ts TEXT NOT NULL,
            FOREIGN KEY(deck_id) REFERENCES decks(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS topics (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL UNIQUE
        );

        CREATE TABLE IF NOT EXISTS card_topics (
            card_id INTEGER NOT NULL,
            topic_id INTEGER NOT NULL,
            PRIMARY KEY(card_id, topic_id),
            FOREIGN KEY(card_id) REFERENCES cards(id) ON DELETE CASCADE,
            FOREIGN KEY(topic_id) REFERENCES topics(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS topic_edges (
            src_topic_id INTEGER NOT NULL,
            dst_topic_id INTEGER NOT NULL,
            weight REAL DEFAULT 1.0,
            PRIMARY KEY(src_topic_id, dst_topic_id),
            FOREIGN KEY(src_topic_id) REFERENCES topics(id) ON DELETE CASCADE,
            FOREIGN KEY(dst_topic_id) REFERENCES topics(id) ON DELETE CASCADE
        );
        """
    )
    cur.execute("INSERT OR IGNORE INTO decks(name, parent_id) VALUES(?, NULL)", ("root",))
    conn.commit()
    conn.close()

@dataclass
class Deck:
    id: int
    name: str
    parent_id: Optional[int]

@dataclass
class Card:
    id: int
    deck_id: int
    front: str
    back: str
    tags: str
    easiness: float
    interval: float
    repetitions: int
    due_ts: Optional[str]
    last_review_ts: Optional[str]
    successes: int
    failures: int
    created_ts: str
    updated_ts: str

def get_deck_id_by_name(cur: sqlite3.Cursor, name: str) -> int:
    row = cur.execute("SELECT id FROM decks WHERE name = ?", (name,)).fetchone()
    if not row:
        raise ValueError(f"Deck '{name}' not found. Create it first with add-deck.")
    return int(row[0])

def add_deck(name: str, parent: str) -> Tuple[str, Dict[str, Any]]:
    conn = get_conn()
    cur = conn.cursor()
    parent_id = get_deck_id_by_name(cur, parent)
    cur.execute("INSERT INTO decks(name, parent_id) VALUES(?, ?)", (name, parent_id))
    conn.commit()
    conn.close()
    return ("add_deck", {"name": name})

def delete_deck(name: str) -> Tuple[str, Dict[str, Any]]:
    conn = get_conn()
    cur = conn.cursor()
    deck = cur.execute("SELECT * FROM decks WHERE name = ?", (name,)).fetchone()
    if not deck:
        raise ValueError(f"Deck '{name}' not found.")
    deck_dict = dict(deck)
    cur.execute("DELETE FROM decks WHERE id = ?", (deck["id"],))
    conn.commit()
    conn.close()
    return ("delete_deck", deck_dict)

def add_card(deck_name: str, front: str, back: str, tags: str, topics: List[str]) -> Tuple[str, Dict[str, Any]]:
    conn = get_conn()
    cur = conn.cursor()
    deck_id = get_deck_id_by_name(cur, deck_name)
    ts = now_ts()
    cur.execute(
        """
        INSERT INTO cards(deck_id, front, back, tags, created_ts, updated_ts, due_ts)
        VALUES(?, ?, ?, ?, ?, ?, ?)
        """,
        (deck_id, front, back, tags or "", ts, ts, now_ts()),
    )
    card_id = cur.lastrowid
    for t in [t.strip() for t in topics if t.strip()]:
        cur.execute("INSERT OR IGNORE INTO topics(name) VALUES(?)", (t,))
        topic_id = cur.execute("SELECT id FROM topics WHERE name=?", (t,)).fetchone()[0]
        cur.execute(
            "INSERT OR IGNORE INTO card_topics(card_id, topic_id) VALUES(?, ?)",
            (card_id, topic_id),
        )
    conn.commit()
    conn.close()
    return ("add_card", {"card_id": card_id})

def edit_card(card_id: int, **updates) -> Tuple[str, Dict[str, Any]]:
    conn = get_conn()
    cur = conn.cursor()
    row = cur.execute("SELECT * FROM cards WHERE id=?", (card_id,)).fetchone()
    if not row:
        raise ValueError("Card not found")
    before = dict(row)
    allowed = {k: v for k, v in updates.items() if k in before}
    if not allowed:
        raise ValueError("No valid fields to update")
    allowed["updated_ts"] = now_ts()
    sets = ",".join([f"{k} = :{k}" for k in allowed.keys()])
    allowed["id"] = card_id
    cur.execute(f"UPDATE cards SET {sets} WHERE id = :id", allowed)
    conn.commit()
    conn.close()
    return ("edit_card", {"before": before, "after": allowed})

def delete_card(card_id: int) -> Tuple[str, Dict[str, Any]]:
    conn = get_conn()
    cur = conn.cursor()
    row = cur.execute("SELECT * FROM cards WHERE id=?", (card_id,)).fetchone()
    if not row:
        raise ValueError("Card not found")
    backup = dict(row)
    cur.execute("DELETE FROM cards WHERE id=?", (card_id,))
    conn.commit()
    conn.close()
    return ("delete_card", backup)

def sm2_update(card: Card, quality: int) -> Card:
    assert 0 <= quality <= 5
    e = card.easiness
    r = card.repetitions
    interval = card.interval
    if quality < 3:
        r = 0
        interval = 0
        card.failures += 1
    else:
        if r == 0:
            interval = 1
        elif r == 1:
            interval = 6
        else:
            interval = round(interval * e, 2)
        r += 1
        card.successes += 1
        e = e + (0.1 - (5 - quality) * (0.08 + (5 - quality) * 0.02))
        if e < 1.3:
            e = 1.3
    card.easiness = round(e, 4)
    card.interval = float(interval)
    card.repetitions = int
