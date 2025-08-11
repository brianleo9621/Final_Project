import pandas as pd
import streamlit as st
from datetime import datetime
from app import (
    init_db,
    get_conn,
    load_indexes,
    load_due_cards,
    QuizQueue,
    CardCache,
    sm2_update,
    now_ts,
)

st.set_page_config(page_title="Adaptive Flashcards", layout="wide")

QUALITY_HELP = {
    0: "Blackout. Treat as fail; review again very soon.",
    1: "Incorrect. Major difficulty.",
    2: "Incorrect, partial recall.",
    3: "Correct but hard. Interval grows slowly.",
    4: "Correct with brief hesitation. Interval grows faster.",
    5: "Perfect recall. Fastest interval growth.",
}

def get_decks():
    conn = get_conn()
    rows = pd.read_sql_query("SELECT id, name, parent_id FROM decks ORDER BY name", conn)
    conn.close()
    return rows

def get_cards(deck_id=None, limit=200):
    conn = get_conn()
    if deck_id is None:
        q = "SELECT id, deck_id, front, back, tags, due_ts, successes, failures FROM cards ORDER BY id DESC LIMIT ?"
        df = pd.read_sql_query(q, conn, params=(limit,))
    else:
        q = "SELECT id, deck_id, front, back, tags, due_ts, successes, failures FROM cards WHERE deck_id=? ORDER BY id DESC LIMIT ?"
        df = pd.read_sql_query(q, conn, params=(deck_id, limit))
    conn.close()
    return df

def update_card_spaced_repetition(card_id: int, quality: int):
    conn = get_conn()
    cur = conn.cursor()
    row = cur.execute("SELECT * FROM cards WHERE id=?", (card_id,)).fetchone()
    if not row:
        conn.close()
        return False, "Card not found"
    from app import card_from_row
    c = card_from_row(row)
    c = sm2_update(c, quality)
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
    return True, None

with st.sidebar:
    st.title("⚡ Flashcards")
    if st.button("Initialize DB", use_container_width=True):
        init_db()
        load_indexes()
        st.success("DB initialized.")
    decks_df = get_decks()
    deck_names = ["(any deck)"] + decks_df["name"].tolist()
    deck_choice = st.selectbox("Deck", deck_names, index=0)
    chosen_deck_id = None if deck_choice == "(any deck)" else int(
        decks_df.loc[decks_df["name"] == deck_choice, "id"].iloc[0]
    )
    st.markdown("---")
    st.subheader("Add Deck")
    new_deck_name = st.text_input("Name", key="new_deck_name")
    parent_name = st.selectbox(
        "Parent",
        decks_df["name"].tolist() if not decks_df.empty else ["root"],
        index=0,
        key="parent_select",
    )
    if st.button("Create Deck"):
        conn = get_conn()
        cur = conn.cursor()
        parent_row = cur.execute("SELECT id FROM decks WHERE name=?", (parent_name,)).fetchone()
        if parent_row is None:
            st.error("Parent deck not found.")
        else:
            try:
                cur.execute("INSERT INTO decks(name, parent_id) VALUES(?, ?)", (new_deck_name, parent_row[0]))
                conn.commit()
                st.success(f"Deck '{new_deck_name}' created under '{parent_name}'.")
            except Exception as e:
                st.error(str(e))
        conn.close()
        st.rerun()
    st.markdown("---")
    st.subheader("Add Card")
    ac_front = st.text_area("Front", height=80)
    ac_back = st.text_area("Back", height=80)
    ac_tags = st.text_input("Tags (comma-separated)")
    ac_topics = st.text_input("Topics (comma-separated)")
    if st.button("Add Card to Selected Deck"):
        if chosen_deck_id is None:
            st.error("Pick a specific deck to add cards.")
        else:
            conn = get_conn()
            cur = conn.cursor()
            ts = now_ts()
            try:
                cur.execute(
                    """
                    INSERT INTO cards(deck_id, front, back, tags, created_ts, updated_ts, due_ts)
                    VALUES(?, ?, ?, ?, ?, ?, ?)
                    """,
                    (chosen_deck_id, ac_front, ac_back, ac_tags, ts, ts, now_ts()),
                )
                card_id = cur.lastrowid
                topics = [t.strip() for t in ac_topics.split(",") if t.strip()]
                for t in topics:
                    cur.execute("INSERT OR IGNORE INTO topics(name) VALUES(?)", (t,))
                    topic_id = cur.execute("SELECT id FROM topics WHERE name=?", (t,)).fetchone()[0]
                    cur.execute("INSERT OR IGNORE INTO card_topics(card_id, topic_id) VALUES(?, ?)", (card_id, topic_id))
                conn.commit()
                st.success("Card added.")
            except Exception as e:
                st.error(str(e))
            finally:
                conn.close()

TAB_REVIEW, TAB_BROWSE, TAB_STATS = st.tabs(["Review", "Browse", "Stats"])

with TAB_REVIEW:
    st.header("Review due cards")
    if st.button("Load Due Cards", type="primary"):
        load_due_cards(deck_choice if deck_choice != "(any deck)" else None, limit=100)
        st.success(f"Loaded {len(QuizQueue)} due cards.")
    if len(QuizQueue) == 0:
        st.info("No cards in the queue. Click 'Load Due Cards'.")
    else:
        cid = QuizQueue[0]
        c = CardCache[cid]
        with st.container(border=True):
            st.subheader(f"Card #{c.id}")
            st.markdown(f"**Front**: {c.front}")
            if st.toggle("Reveal answer"):
                st.markdown(f"**Back**: {c.back}")
            st.caption(f"Tags: {c.tags or '(none)'}")
            cols = st.columns(6)
            for q in range(6):
                if cols[q].button(f"Quality {q}", key=f"q{q}", help=QUALITY_HELP[q]):
                    ok, _ = update_card_spaced_repetition(c.id, q)
                    if ok:
                        QuizQueue.popleft()
                        st.success(f"Recorded quality {q}. Next due set.")
                        st.rerun()
                    else:
                        st.error("Failed to update card.")
            with st.expander("What do these numbers mean?"):
                st.markdown(
                    """
- **0–2 (fail):** you didn’t recall → repetitions reset; due again very soon; E-factor decreases.
- **3 (hard):** first success → 1 day; second → 6 days; then interval × E-factor.
- **4 (good):** like 3 but grows faster.
- **5 (perfect):** fastest growth.
*E-factor* starts at **2.5** and never drops below **1.3**.
                    """
                )
        st.caption(f"Cards remaining in queue: {len(QuizQueue)}")

with TAB_BROWSE:
    st.header("Browse cards")
    df_cards = get_cards(chosen_deck_id)
    st.dataframe(df_cards, use_container_width=True, hide_index=True)

with TAB_STATS:
    st.header("Deck stats")
    conn = get_conn()
    decks = pd.read_sql_query("SELECT id, name FROM decks ORDER BY name", conn)
    rows = []
    for _, d in decks.iterrows():
        total = pd.read_sql_query("SELECT COUNT(*) AS c FROM cards WHERE deck_id=?", conn, params=(int(d.id),)).iloc[0, 0]
        due = pd.read_sql_query(
            "SELECT COUNT(*) AS c FROM cards WHERE deck_id=? AND (due_ts IS NULL OR due_ts <= ?)",
            conn,
            params=(int(d.id), datetime.utcnow().isoformat(timespec="seconds")),
        ).iloc[0, 0]
        rows.append({"deck": d.name, "total": total, "due_now": due})
    conn.close()
    stats_df = pd.DataFrame(rows)
    st.dataframe(stats_df, use_container_width=True, hide_index=True)
    try:
        st.bar_chart(stats_df.set_index("deck")["due_now"])
    except Exception:
        pass
