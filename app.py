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

# ---------------------------
# Utilities
# ---------------------------

def now_ts() -> str:
    return datetime.utcnow().isoformat(timespec="seconds")


def days_from_now(n: float) -> str:
    return (datetime.utcnow() + timedelta(days=n)).isoformat(timespec="seconds")


# ---------------------------
# Database layer
# ---------------------------

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

        -- Graph: directed edges between topics (concept map)
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
    # Ensure root deck exists
    cur.execute("INSERT OR IGNORE INTO decks(name, parent_id) VALUES(?, NULL)", ("root",))
    conn.commit()
    conn.close()


# ---------------------------
# Data classes (for clarity)
# ---------------------------

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


# ---------------------------
# Repositories / CRUD
# ---------------------------

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

    # Topics
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


# ---------------------------
# Spaced Repetition (SM-2)
# ---------------------------

def sm2_update(card: Card, quality: int) -> Card:
    """Apply SM-2 algorithm to a card, returning an updated Card (not yet persisted)."""
    assert 0 <= quality <= 5
    e = card.easiness
    r = card.repetitions
    interval = card.interval

    if quality < 3:  # failure
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
        # E-Factor update
        e = e + (0.1 - (5 - quality) * (0.08 + (5 - quality) * 0.02))
        if e < 1.3:
            e = 1.3

    card.easiness = round(e, 4)
    card.interval = float(interval)
    card.repetitions = int(r)
    card.last_review_ts = now_ts()
    card.due_ts = days_from_now(card.interval)
    card.updated_ts = now_ts()
    return card


# ---------------------------
# Session state (DSA demo)
# ---------------------------

SessionHistory: List[Dict[str, Any]] = []  # List
UndoStack: List[Tuple[str, Dict[str, Any]]] = []  # Stack
RedoStack: List[Tuple[str, Dict[str, Any]]] = []  # Stack
QuizQueue: deque[int] = deque()  # Queue of card IDs
CardCache: Dict[int, Card] = {}  # Hash table cache of cards by id
DeckIndex: Dict[str, int] = {}  # Hash table of deck names → id
TopicGraph: Dict[str, Dict[str, float]] = {}  # Graph adjacency map


# ---------------------------
# Loading helpers
# ---------------------------

def load_indexes() -> None:
    DeckIndex.clear()
    TopicGraph.clear()
    conn = get_conn()
    cur = conn.cursor()
    for row in cur.execute("SELECT id, name FROM decks"):
        DeckIndex[row["name"]] = row["id"]
    # Build topic graph
    name_by_id = {}
    for row in cur.execute("SELECT id, name FROM topics"):
        name_by_id[row["id"]] = row["name"]
        TopicGraph.setdefault(row["name"], {})
    for row in cur.execute("SELECT src_topic_id, dst_topic_id, weight FROM topic_edges"):
        src, dst, w = row
        TopicGraph[name_by_id[src]][name_by_id[dst]] = w
    conn.close()


def card_from_row(row: sqlite3.Row) -> Card:
    return Card(**{k: row[k] for k in row.keys()})


def load_due_cards(deck: Optional[str], limit: int = 50) -> None:
    """Populate QuizQueue with due cards, earliest due first."""
    QuizQueue.clear()
    CardCache.clear()
    conn = get_conn()
    cur = conn.cursor()
    deck_clause = ""
    params: Tuple[Any, ...] = ()
    if deck:
        deck_id = get_deck_id_by_name(cur, deck)
        deck_clause = "AND deck_id = ?"
        params = (deck_id,)

    rows = cur.execute(
        f"""
        SELECT * FROM cards
        WHERE (due_ts IS NULL OR due_ts <= ?)
        {deck_clause}
        ORDER BY COALESCE(due_ts, '1970-01-01T00:00:00') ASC
        LIMIT ?
        """,
        (now_ts(),) + params + (limit,),
    ).fetchall()

    for r in rows:
        c = card_from_row(r)
        CardCache[c.id] = c
        QuizQueue.append(c.id)
    conn.close()


# ---------------------------
# Undo/Redo engine
# ---------------------------

def push_undo(op: Tuple[str, Dict[str, Any]]):
    UndoStack.append(op)
    RedoStack.clear()


def do_undo() -> str:
    if not UndoStack:
        return "Nothing to undo."
    op, data = UndoStack.pop()
    inverse = apply_inverse(op, data)
    RedoStack.append(inverse)
    return f"Undid {op}."


def do_redo() -> str:
    if not RedoStack:
        return "Nothing to redo."
    op, data = RedoStack.pop()
    inverse = apply_inverse(op, data)
    UndoStack.append(inverse)
    return f"Redid {op}."


def apply_inverse(op: str, data: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    conn = get_conn()
    cur = conn.cursor()
    if op == "add_deck":
        cur.execute("DELETE FROM decks WHERE name=?", (data["name"],))
        conn.commit()
        conn.close()
        return ("delete_deck", data)
    if op == "delete_deck":
        cur.execute("INSERT INTO decks(id, name, parent_id) VALUES(?, ?, ?)", (
            data["id"], data["name"], data["parent_id"],
        ))
        conn.commit()
        conn.close()
        return ("add_deck", {"name": data["name"]})

    if op == "add_card":
        cid = data["card_id"]
        cur.execute("DELETE FROM cards WHERE id=?", (cid,))
        conn.commit()
        conn.close()
        return ("delete_card", {"id": cid})
    if op == "delete_card":
        cols = ",".join(data.keys())
        qmarks = ",".join([":" + k for k in data.keys()])
        cur.execute(f"INSERT INTO cards({cols}) VALUES({qmarks})", data)
        conn.commit()
        conn.close()
        return ("add_card", {"card_id": data["id"]})
    if op == "edit_card":
        before = data["before"]
        cid = before["id"]
        sets = ",".join([f"{k} = :{k}" for k in before.keys() if k != "id"])
        before2 = before.copy()
        cur.execute(f"UPDATE cards SET {sets} WHERE id = :id", before2)
        conn.commit()
        conn.close()
        return ("edit_card", {"before": data["after"], "after": before})
    if op == "answer_card":
        prev = data["before"]
        cid = prev["id"]
        sets = ",".join([f"{k} = :{k}" for k in prev.keys() if k != "id"])
        cur.execute(f"UPDATE cards SET {sets} WHERE id = :id", prev)
        conn.commit()
        conn.close()
        return ("answer_card", {"before": data["after"]})

    conn.close()
    return (op, data)


# ---------------------------
# CLI commands
# ---------------------------

def cmd_init_db(args):
    init_db()
    load_indexes()
    print(f"Initialized DB at {DB_PATH} with root deck.")


def cmd_add_deck(args):
    load_indexes()
    op = add_deck(args.name, args.parent)
    push_undo(op)
    print(f"Added deck '{args.name}' under '{args.parent}'.")


def cmd_delete_deck(args):
    load_indexes()
    op = delete_deck(args.name)
    push_undo(op)
    print(f"Deleted deck '{args.name}'.")


def cmd_add_card(args):
    load_indexes()
    topics = args.topics.split(",") if args.topics else []
    op = add_card(args.deck, args.front, args.back, args.tags or "", topics)
    push_undo(op)
    print("Added card to deck:", args.deck)


def cmd_edit_card(args):
    updates = {}
    if args.front: updates["front"] = args.front
    if args.back: updates["back"] = args.back
    if args.tags is not None: updates["tags"] = args.tags
    op = edit_card(args.id, **updates)
    push_undo(op)
    print(f"Edited card {args.id}.")


def cmd_delete_card(args):
    op = delete_card(args.id)
    push_undo(op)
    print(f"Deleted card {args.id}.")


def cmd_list(args):
    conn = get_conn()
    cur = conn.cursor()
    q = "SELECT id, front, back, tags, due_ts, successes, failures FROM cards"
    params: Tuple[Any, ...] = ()
    if args.deck:
        deck_id = get_deck_id_by_name(cur, args.deck)
        q += " WHERE deck_id = ?"
        params = (deck_id,)
    q += " ORDER BY COALESCE(due_ts, '1970-01-01T00:00:00') ASC, id ASC LIMIT ?"
    params = params + (args.limit,)
    rows = cur.execute(q, params).fetchall()
    conn.close()
    print(tabulate(rows, headers="keys", tablefmt="simple"))


def cmd_quiz(args):
    load_indexes()
    load_due_cards(args.deck, args.limit)
    if not QuizQueue:
        print("No due cards. You're caught up! 🎉")
        return
    cid = QuizQueue[0]
    c = CardCache[cid]
    print(f"\n[Card {c.id}] {c.front}\n(Deck: {args.deck or 'any'})\n")
    print("Use: python app.py answer --quality [0-5]  OR press Enter here to enter interactively.")


def cmd_answer(args):
    if args.quality is None:
        try:
            q = int(input("Quality 0..5 (0=complete blackout, 5=perfect): "))
        except Exception:
            print("Invalid input.")
            return
    else:
        q = args.quality

    if not QuizQueue:
        print("No card loaded. Run quiz first.")
        return

    cid = QuizQueue.popleft()
    conn = get_conn()
    cur = conn.cursor()
    row = cur.execute("SELECT * FROM cards WHERE id=?", (cid,)).fetchone()
    if not row:
        print("Card missing.")
        conn.close()
        return
    before = dict(row)
    c = card_from_row(row)

    c = sm2_update(c, q)

    sets = (
        "easiness = :e, interval = :i, repetitions = :r, due_ts = :due, last_review_ts=:last, "
        "successes = :s, failures = :f, updated_ts = :u"
    )
    cur.execute(
        f"UPDATE cards SET {sets} WHERE id = :id",
        {
            "e": c.easiness,
            "i": c.interval,
            "r": c.repetitions,
            "due": c.due_ts,
            "last": c.last_review_ts,
            "s": c.successes,
            "f": c.failures,
            "u": c.updated_ts,
            "id": c.id,
        },
    )
    conn.commit()
    conn.close()

    SessionHistory.append({"card_id": c.id, "quality": q, "answered_ts": now_ts()})
    op = ("answer_card", {"before": before, "after": asdict(c)})
    push_undo(op)

    print(f"Answered card {c.id} with quality {q}. Next due: {c.due_ts} (in ~{c.interval} days)")
    if QuizQueue:
        print(f"{len(QuizQueue)} more due in queue. Run 'python app.py quiz' to continue.")


def cmd_undo(args):
    print(do_undo())


def cmd_redo(args):
    print(do_redo())


def cmd_stats(args):
    conn = get_conn()
    cur = conn.cursor()
    decks = cur.execute("SELECT id, name FROM decks ORDER BY name").fetchall()
    rows = []
    for d in decks:
        total = cur.execute("SELECT COUNT(*) FROM cards WHERE deck_id=?", (d["id"],)).fetchone()[0]
        due = cur.execute(
            "SELECT COUNT(*) FROM cards WHERE deck_id=? AND (due_ts IS NULL OR due_ts <= ?)",
            (d["id"], now_ts()),
        ).fetchone()[0]
        rows.append({"deck": d["name"], "total": total, "due_now": due})
    print(tabulate(rows, headers="keys", tablefmt="simple"))
    conn.close()


# ---------------------------
# Argparse wiring
# ---------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Adaptive Flashcards (MVP)")
    sub = p.add_subparsers(dest="cmd")

    s = sub.add_parser("init-db", help="Initialize database")
    s.set_defaults(func=cmd_init_db)

    s = sub.add_parser("add-deck", help="Add a deck")
    s.add_argument("name")
    s.add_argument("--parent", default="root")
    s.set_defaults(func=cmd_add_deck)

    s = sub.add_parser("delete-deck", help="Delete a deck")
    s.add_argument("name")
    s.set_defaults(func=cmd_delete_deck)

    s = sub.add_parser("add-card", help="Add a card")
    s.add_argument("--deck", required=True)
    s.add_argument("--front", required=True)
    s.add_argument("--back", required=True)
    s.add_argument("--tags", default="")
    s.add_argument("--topics", default="")
    s.set_defaults(func=cmd_add_card)

    s = sub.add_parser("edit-card", help="Edit a card")
    s.add_argument("id", type=int)
    s.add_argument("--front")
    s.add_argument("--back")
    s.add_argument("--tags")
    s.set_defaults(func=cmd_edit_card)

    s = sub.add_parser("delete-card", help="Delete a card")
    s.add_argument("id", type=int)
    s.set_defaults(func=cmd_delete_card)

    s = sub.add_parser("list", help="List cards")
    s.add_argument("--deck")
    s.add_argument("--limit", type=int, default=50)
    s.set_defaults(func=cmd_list)

    s = sub.add_parser("quiz", help="Load next due card")
    s.add_argument("--deck")
    s.add_argument("--limit", type=int, default=50)
    s.set_defaults(func=cmd_quiz)

    s = sub.add_parser("answer", help="Answer the current card")
    s.add_argument("--quality", type=int)
    s.set_defaults(func=cmd_answer)

    s = sub.add_parser("undo", help="Undo last action (session)")
    s.set_defaults(func=cmd_undo)

    s = sub.add_parser("redo", help="Redo last undone action (session)")
    s.set_defaults(func=cmd_redo)

    s = sub.add_parser("stats", help="Deck stats (due/total)")
    s.set_defaults(func=cmd_stats)

    return p


def main():
    parser = build_parser()
    args = parser.parse_args()
    if not hasattr(args, "func"):
        parser.print_help()
        return
    args.func(args)


if __name__ == "__main__":
    main()
