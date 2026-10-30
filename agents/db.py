"""Mémoire persistante SQLite : conversations, file de commandes, base documentaire."""
import os
import re
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

DB_PATH = Path(os.getenv("AGENTS_DB", Path(__file__).resolve().parent.parent / "data" / "agents.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,
    agent TEXT NOT NULL,
    repo TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages(conversation_id, id);
CREATE TABLE IF NOT EXISTS proposals (
    id TEXT PRIMARY KEY,
    repo TEXT NOT NULL,
    command TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL,
    exit_code INTEGER,
    output TEXT
);
CREATE TABLE IF NOT EXISTS docs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    source TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS workflows (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    repo TEXT,
    graph TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    workflow_id TEXT NOT NULL REFERENCES workflows(id) ON DELETE CASCADE,
    status TEXT NOT NULL,
    input TEXT NOT NULL DEFAULT '',
    repo TEXT,
    states TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_wf ON runs(workflow_id, created_at);
CREATE TABLE IF NOT EXISTS triggers (
    workflow_id TEXT PRIMARY KEY REFERENCES workflows(id) ON DELETE CASCADE,
    kind TEXT NOT NULL DEFAULT 'manual',
    every_minutes INTEGER,
    daily_at TEXT,
    enabled INTEGER NOT NULL DEFAULT 0,
    webhook_token TEXT UNIQUE,
    input TEXT NOT NULL DEFAULT '',
    last_run_at TEXT
);
CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    path TEXT NOT NULL UNIQUE,
    stacks TEXT NOT NULL DEFAULT '[]',
    check_command TEXT,
    created_at TEXT NOT NULL
);
CREATE VIRTUAL TABLE IF NOT EXISTS docs_fts USING fts5(
    title, content, content='docs', content_rowid='id', tokenize='unicode61 remove_diacritics 2'
);
CREATE TRIGGER IF NOT EXISTS docs_ai AFTER INSERT ON docs BEGIN
    INSERT INTO docs_fts(rowid, title, content) VALUES (new.id, new.title, new.content);
END;
CREATE TRIGGER IF NOT EXISTS docs_ad AFTER DELETE ON docs BEGIN
    INSERT INTO docs_fts(docs_fts, rowid, title, content) VALUES ('delete', old.id, old.title, old.content);
END;
"""

HISTORY_LIMIT = 10  # derniers messages rejoués à l'agent
DOC_CHUNK = 1500  # caractères par fragment indexé


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


@contextmanager
def connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# --- Conversations -------------------------------------------------------

def new_conversation(agent: str, repo: str | None) -> str:
    cid = uuid.uuid4().hex[:12]
    with connect() as db:
        db.execute("INSERT INTO conversations VALUES (?, ?, ?, ?)", (cid, agent, repo, now()))
    return cid


def conversation_exists(cid: str) -> bool:
    with connect() as db:
        return db.execute("SELECT 1 FROM conversations WHERE id = ?", (cid,)).fetchone() is not None


def history(cid: str) -> list[dict]:
    with connect() as db:
        rows = db.execute(
            "SELECT role, content FROM messages WHERE conversation_id = ? ORDER BY id DESC LIMIT ?",
            (cid, HISTORY_LIMIT),
        ).fetchall()
    return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]


def add_messages(cid: str, pairs: list[tuple[str, str]]) -> None:
    with connect() as db:
        db.executemany(
            "INSERT INTO messages (conversation_id, role, content, created_at) VALUES (?, ?, ?, ?)",
            [(cid, role, content, now()) for role, content in pairs],
        )


def list_conversations() -> list[dict]:
    with connect() as db:
        rows = db.execute(
            "SELECT c.id, c.agent, c.repo, c.created_at, COUNT(m.id) AS messages "
            "FROM conversations c LEFT JOIN messages m ON m.conversation_id = c.id "
            "GROUP BY c.id ORDER BY c.created_at DESC LIMIT 100"
        ).fetchall()
    return [dict(r) for r in rows]


def get_conversation(cid: str) -> list[dict]:
    with connect() as db:
        rows = db.execute(
            "SELECT role, content, created_at FROM messages WHERE conversation_id = ? ORDER BY id", (cid,)
        ).fetchall()
    return [dict(r) for r in rows]


def delete_conversation(cid: str) -> bool:
    with connect() as db:
        return db.execute("DELETE FROM conversations WHERE id = ?", (cid,)).rowcount > 0


# --- Base documentaire ---------------------------------------------------

def _chunks(text: str) -> list[str]:
    """Découpe par paragraphes en fragments d'environ DOC_CHUNK caractères."""
    out, current = [], ""
    for para in re.split(r"\n\s*\n", text):
        if current and len(current) + len(para) > DOC_CHUNK:
            out.append(current)
            current = ""
        current += ("\n\n" if current else "") + para
        while len(current) > DOC_CHUNK * 2:
            out.append(current[:DOC_CHUNK])
            current = current[DOC_CHUNK:]
    if current.strip():
        out.append(current)
    return out


def add_doc(title: str, content: str, source: str | None = None) -> int:
    parts = _chunks(content) or [content]
    with connect() as db:
        for i, part in enumerate(parts, 1):
            label = title if len(parts) == 1 else f"{title} ({i}/{len(parts)})"
            db.execute(
                "INSERT INTO docs (title, content, source, created_at) VALUES (?, ?, ?, ?)",
                (label, part, source, now()),
            )
    return len(parts)


def search_docs(query: str, limit: int = 5) -> list[dict]:
    # Chaque mot est mis entre guillemets : pas d'injection de syntaxe FTS.
    terms = re.findall(r"\w+", query)
    if not terms:
        return []
    match = " OR ".join(f'"{t}"' for t in terms)
    with connect() as db:
        rows = db.execute(
            "SELECT d.id, d.title, d.source, snippet(docs_fts, 1, '[', ']', '…', 40) AS extrait, d.content "
            "FROM docs_fts JOIN docs d ON d.id = docs_fts.rowid "
            "WHERE docs_fts MATCH ? ORDER BY bm25(docs_fts) LIMIT ?",
            (match, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def list_docs() -> list[dict]:
    with connect() as db:
        rows = db.execute("SELECT id, title, source, created_at FROM docs ORDER BY id DESC LIMIT 200").fetchall()
    return [dict(r) for r in rows]


def delete_doc(doc_id: int) -> bool:
    with connect() as db:
        return db.execute("DELETE FROM docs WHERE id = ?", (doc_id,)).rowcount > 0
