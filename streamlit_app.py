import os, sqlite3
from collections import deque
from datetime import datetime
from typing import Dict, List, Optional
import streamlit as st

DB_PATH = os.environ.get("FLASHCARDS_DB", "flashcards.db")

def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_conn(); cur = conn.cursor()
    cur.execute("PRAGMA foreign_keys=ON;")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS cards(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            deck TEXT NOT NULL,
            front TEXT NOT NULL,
            back  TEXT NOT NULL,
            difficulty INTEGER,
            created_ts TEXT NOT NULL
        );
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_cards_deck ON cards(deck)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_cards_diff ON cards(difficulty)")
    conn.commit(); conn.close()

def normalize_deck(path: str) -> str:
    parts = [p.strip() for p in (path or "").split("/") if p.strip()]
    return "/".join(parts)

def add_card(deck: str, front: str, back: str):
    deck = normalize_deck(deck)
    ts = datetime.utcnow().isoformat(timespec="seconds")
    conn = get_conn(); cur = conn.cursor()
    cur.execute(
        "INSERT INTO cards(deck,front,back,created_ts) VALUES(?,?,?,?)",
        (deck, front.strip(), back.strip(), ts)
    )
    conn.commit(); conn.close()

def rename_deck(old_path: str, new_path: str) -> None:
    new_path = normalize_deck(new_path)
    conn = get_conn(); cur = conn.cursor()
    cur.execute("UPDATE cards SET deck=? WHERE deck=?", (new_path, old_path))
    conn.commit(); conn.close()

def delete_cards(ids: List[int]) -> int:
    if not ids: return 0
    conn = get_conn(); cur = conn.cursor()
    q = f"DELETE FROM cards WHERE id IN ({','.join(['?']*len(ids))})"
    cur.execute(q, ids)
    n = cur.rowcount
    conn.commit(); conn.close()
    return n

def get_decks() -> List[str]:
    conn = get_conn()
    vals = [normalize_deck(r[0]) for r in conn.execute(
        "SELECT DISTINCT deck FROM cards ORDER BY deck"
    ).fetchall()]
    conn.close()
    return vals

def fetch_cards_for_study(deck: str):
    conn = get_conn(); cur = conn.cursor()
    if deck == "(all)":
        rows = cur.execute("SELECT * FROM cards ORDER BY id").fetchall()
    else:
        rows = cur.execute(
            "SELECT * FROM cards WHERE deck = ? OR deck LIKE ? ORDER BY id",
            (deck, f"{deck}/%")
        ).fetchall()
    conn.close()
    return rows

def get_cards_preview(deck: str) -> List[Dict]:
    conn = get_conn(); cur = conn.cursor()
    if deck == "(all)":
        rows = cur.execute(
            "SELECT id, deck, front, back, difficulty, created_ts FROM cards ORDER BY id DESC"
        ).fetchall()
    else:
        rows = cur.execute(
            "SELECT id, deck, front, back, difficulty, created_ts FROM cards "
            "WHERE deck = ? OR deck LIKE ? ORDER BY id DESC",
            (deck, f"{deck}/%")
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]

def set_difficulty(card_id: int, difficulty: Optional[int]) -> None:
    conn = get_conn(); cur = conn.cursor()
    cur.execute("UPDATE cards SET difficulty=? WHERE id=?", (difficulty, int(card_id)))
    conn.commit(); conn.close()

def reset_all_cards():
    conn = get_conn(); conn.execute("DELETE FROM cards"); conn.commit(); conn.close()

def ensure_state():
    st.session_state.setdefault("queue", deque())
    st.session_state.setdefault("cache", {})
    st.session_state.setdefault("undo", [])
    st.session_state.setdefault("last_deck", "")

def build_deck_tree(paths: List[str]) -> Dict:
    tree: Dict[str, Dict] = {}
    for path in paths:
        node = tree
        for part in [p for p in path.split("/") if p]:
            node = node.setdefault(part, {})
    return tree

def count_cards_under(path: str) -> int:
    conn = get_conn(); cur = conn.cursor()
    c = cur.execute(
        "SELECT COUNT(*) FROM cards WHERE deck = ? OR deck LIKE ?",
        (path, f"{path}/%")
    ).fetchone()[0]
    conn.close()
    return int(c)

def build_tree_lines(node: Dict, prefix_path: str = "", prefix: str = "") -> List[str]:
    lines: List[str] = []
    names = sorted(node.keys())
    for i, name in enumerate(names):
        path = f"{prefix_path}/{name}" if prefix_path else name
        is_last = (i == len(names) - 1)
        branch = "└── " if is_last else "├── "
        count = count_cards_under(path)
        lines.append(f"{prefix}{branch}{name} ({count})")
        child_prefix = prefix + ("    " if is_last else "│   ")
        lines += build_tree_lines(node[name], path, child_prefix)
    return lines

st.set_page_config(page_title="Flashcard Deck", layout="wide")
init_db()
ensure_state()

with st.sidebar:
    st.title("📒 Flashcard Deck")
    st.caption("Create cards and then study them; choose 1–5 after revealing the answer.")
    st.markdown("---")
    st.subheader("Add a card")
    default_deck = st.session_state.last_deck
    with st.form("add_form", clear_on_submit=True):
        deck = st.text_input("Deck / Set", placeholder="e.g., Math/Algebra", value=default_deck)
        front = st.text_area("Front", height=80, placeholder="Question / prompt")
        back  = st.text_area("Back",  height=80, placeholder="Answer")
        submitted = st.form_submit_button("Add")
        if submitted:
            if not deck.strip() or not front.strip() or not back.strip():
                st.error("Deck, Front, and Back are required.")
            else:
                add_card(deck, front, back)
                st.session_state.last_deck = normalize_deck(deck)
                st.success(f"Card added to {normalize_deck(deck)}.")
                st.rerun()
    st.markdown("---")
    with st.expander("Deck tools"):
        decks = get_decks()
        if decks:
            src = st.selectbox("Rename / move this deck", decks, key="deck_src")
            dst = st.text_input("New deck path", placeholder="e.g., Math/Algebra/Linear", key="deck_dst")
            if st.button("Rename/Move deck"):
                if dst.strip():
                    rename_deck(src, dst)
                    st.success(f"Moved '{src}' → '{normalize_deck(dst)}'")
                    st.rerun()
                else:
                    st.warning("Please enter a new deck path.")
        else:
            st.caption("No decks yet.")
    st.markdown("---")
    with st.expander("Full Reset"):
        st.caption("This permanently deletes all cards from the local database file.")
        confirm = st.text_input("Type DELETE to confirm", value="").strip().upper()
        if st.button("Reset database (delete all cards)", type="secondary", disabled=(confirm != "DELETE")):
            reset_all_cards()
            st.session_state.queue.clear()
            st.session_state.cache.clear()
            st.session_state.undo.clear()
            st.success("All cards deleted.")
            st.rerun()

st.header("Study")

decks = ["(all)"] + get_decks()
chosen_deck = st.selectbox("Choose deck", decks, index=0, help="Picking a parent deck includes all its sub-decks.")

col_a, col_b = st.columns([1, 1])
with col_a:
    if st.button("Start Studying", type="primary"):
        rows = fetch_cards_for_study(chosen_deck)
        st.session_state.queue.clear()
        st.session_state.cache.clear()
        if not rows:
            st.warning("No cards found for this deck (including sub-decks). Add a card on the left.")
        else:
            for r in rows:
                cid = int(r["id"])
                st.session_state.queue.append(cid)
                st.session_state.cache[cid] = dict(r)
            st.success(f"Loaded {len(st.session_state.queue)} cards.")
        st.rerun()
with col_b:
    if st.button("Reset Session"):
        st.session_state.queue.clear()
        st.info("Queue cleared.")
        st.rerun()

st.caption(f"Cards remaining: {len(st.session_state.queue)}")

with st.expander("View cards in selected deck"):
    preview = get_cards_preview(chosen_deck)
    st.write(f"Total: {len(preview)}")
    st.dataframe(preview, use_container_width=True, hide_index=True)
    ids_in_deck = [row["id"] for row in preview]
    del_ids = st.multiselect("Select card IDs to delete", ids_in_deck, key="del_ids")
    if st.button("Delete selected cards", disabled=(len(del_ids) == 0)):
        n = delete_cards(del_ids)
        st.success(f"Deleted {n} card(s).")
        st.session_state.queue = deque([cid for cid in st.session_state.queue if cid not in del_ids])
        for cid in del_ids:
            st.session_state.cache.pop(cid, None)
        st.rerun()

with st.expander("Deck Tree"):
    paths = get_decks()
    tree = build_deck_tree(paths)
    if tree:
        st.caption("Parents aggregate sub-decks. Counts include all descendants.")
        lines = build_tree_lines(tree)
        st.code("\n".join(lines))
    else:
        st.caption("Use slashes in deck names, e.g., Math/Algebra/Linear.")

if len(st.session_state.queue) == 0:
    st.info("Click Start Studying to load cards.")
else:
    current_id = st.session_state.queue[0]
    card = st.session_state.cache.get(current_id)
    if not card:
        conn = get_conn(); row = conn.execute("SELECT * FROM cards WHERE id=?", (current_id,)).fetchone(); conn.close()
        card = dict(row) if row else {"deck": "", "front": "", "back": ""}
        st.session_state.cache[current_id] = card
    st.subheader(card.get('deck', ''))
    st.markdown(f"### {card.get('front','')}")
    if st.toggle("Reveal answer"):
        st.markdown(f"Answer: {card.get('back','')}")
    st.write("Rate difficulty:")
    cols = st.columns(5)
    for q in range(1, 6):
        if cols[q - 1].button(f"{q}", key=f"q{q}"):
            conn = get_conn()
            row = conn.execute("SELECT difficulty FROM cards WHERE id=?", (current_id,)).fetchone()
            prev = row[0] if row else None
            conn.close()
            st.session_state.undo.append({"id": current_id, "prev": prev})
            set_difficulty(current_id, q)
            st.session_state.queue.popleft()
            st.rerun()
    if st.button("Undo last rating"):
        if st.session_state.undo:
            last = st.session_state.undo.pop()
            set_difficulty(last["id"], last["prev"])
            st.success("Undid last rating.")
            st.session_state.queue.appendleft(last["id"])
        else:
            st.info("Nothing to undo.")
        st.rerun()
