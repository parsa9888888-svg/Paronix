import sqlite3
import time

conn = sqlite3.connect("bot.db", check_same_thread=False)
cur = conn.cursor()

# ---------------- USERS ----------------
cur.execute("""
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER,
    group_id INTEGER,
    warns INTEGER DEFAULT 0,
    mute_until INTEGER DEFAULT 0,
    PRIMARY KEY (user_id, group_id)
)
""")

# ---------------- GROUPS ----------------
cur.execute("""
CREATE TABLE IF NOT EXISTS groups (
    group_id INTEGER PRIMARY KEY,
    is_closed INTEGER DEFAULT 0
)
""")

conn.commit()

# ---------- USERS ----------
def add_user(uid, gid):
    cur.execute("INSERT OR IGNORE INTO users (user_id, group_id) VALUES (?, ?)", (uid, gid))
    conn.commit()

def get_user(uid, gid):
    cur.execute("SELECT * FROM users WHERE user_id=? AND group_id=?", (uid, gid))
    return cur.fetchone()

def add_warn(uid, gid):
    cur.execute("UPDATE users SET warns = warns + 1 WHERE user_id=? AND group_id=?", (uid, gid))
    conn.commit()

def set_mute(uid, gid, seconds):
    cur.execute("""
    UPDATE users SET mute_until=? WHERE user_id=? AND group_id=?
    """, (int(time.time() + seconds), uid, gid))
    conn.commit()


# ---------- GROUP ----------
def close_group(gid):
    cur.execute("INSERT OR REPLACE INTO groups (group_id, is_closed) VALUES (?, 1)", (gid,))
    conn.commit()

def open_group(gid):
    cur.execute("INSERT OR REPLACE INTO groups (group_id, is_closed) VALUES (?, 0)", (gid,))
    conn.commit()

def is_closed(gid):
    cur.execute("SELECT is_closed FROM groups WHERE group_id=?", (gid,))
    r = cur.fetchone()
    return r and r[0] == 1