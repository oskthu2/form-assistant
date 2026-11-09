"""SQLite-databasen: uppladdade filer, formulär och deras versioner, urvalsbiblioteket och lärdomar.

Allt sparas beständigt – bilderna också – så att formulären kan itereras vidare, kännas igen
igen och användas som underlag när instruktionsprompten förbättras.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .model import FormDraft

SCHEMA = """
CREATE TABLE IF NOT EXISTS uploads (
    id TEXT PRIMARY KEY,
    created TEXT NOT NULL,
    filename TEXT NOT NULL,
    media_type TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    size INTEGER NOT NULL,
    dhash TEXT,
    text_fingerprint TEXT,
    data BLOB NOT NULL
);
CREATE INDEX IF NOT EXISTS uploads_sha ON uploads(sha256);
CREATE TABLE IF NOT EXISTS forms (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'utkast',
    created TEXT NOT NULL,
    updated TEXT NOT NULL,
    created_by TEXT NOT NULL,
    approved_by TEXT,
    approved_at TEXT,
    derived_from TEXT,
    fingerprint TEXT,
    search_text TEXT
);
CREATE TABLE IF NOT EXISTS form_uploads (
    form_id TEXT NOT NULL,
    upload_id TEXT NOT NULL,
    position INTEGER NOT NULL,
    PRIMARY KEY (form_id, upload_id)
);
CREATE TABLE IF NOT EXISTS revisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    form_id TEXT NOT NULL,
    number INTEGER NOT NULL,
    created TEXT NOT NULL,
    author TEXT NOT NULL,
    kind TEXT NOT NULL,
    instruction TEXT,
    draft TEXT NOT NULL,
    provenance TEXT
);
CREATE INDEX IF NOT EXISTS revisions_form ON revisions(form_id, number);
CREATE TABLE IF NOT EXISTS valuesets (
    url TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    title TEXT NOT NULL,
    concepts TEXT NOT NULL,
    source_form TEXT,
    created TEXT NOT NULL,
    uses INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS guidelines (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    text TEXT NOT NULL,
    status TEXT NOT NULL,
    source TEXT,
    created TEXT NOT NULL,
    decided_by TEXT
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_id() -> str:
    return secrets.token_hex(6)


class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)

    def execute(self, sql: str, params: tuple | dict = ()) -> sqlite3.Cursor:
        with self._lock, self.conn:
            return self.conn.execute(sql, params)

    def one(self, sql: str, params: tuple | dict = ()) -> sqlite3.Row | None:
        with self._lock:
            return self.conn.execute(sql, params).fetchone()

    def all(self, sql: str, params: tuple | dict = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self.conn.execute(sql, params).fetchall()

    # -- uppladdningar ----------------------------------------------------------------

    def add_upload(self, filename: str, media_type: str, data: bytes, sha256: str, dhash: str | None,
                   text_fingerprint: list[int] | None) -> str:
        existing = self.one("SELECT id FROM uploads WHERE sha256 = ?", (sha256,))
        if existing:
            return existing["id"]
        uid = new_id()
        self.execute("INSERT INTO uploads (id, created, filename, media_type, sha256, size, dhash, text_fingerprint, "
                     "data) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                     (uid, now(), filename, media_type, sha256, len(data), dhash,
                      json.dumps(text_fingerprint) if text_fingerprint else None, data))
        return uid

    def upload(self, uid: str, with_data: bool = False) -> sqlite3.Row | None:
        cols = "*" if with_data else "id, created, filename, media_type, sha256, size, dhash, text_fingerprint"
        return self.one(f"SELECT {cols} FROM uploads WHERE id = ?", (uid,))

    def form_uploads(self, form_id: str, with_data: bool = False) -> list[sqlite3.Row]:
        cols = "u.*" if with_data else "u.id, u.filename, u.media_type, u.sha256, u.size, u.dhash"
        return self.all(f"SELECT {cols} FROM uploads u JOIN form_uploads f ON f.upload_id = u.id "
                        "WHERE f.form_id = ? ORDER BY f.position", (form_id,))

    # -- formulär och versioner -----------------------------------------------------------

    def create_form(self, title: str, author: str, upload_ids: list[str], derived_from: str | None = None) -> str:
        fid = new_id()
        stamp = now()
        self.execute("INSERT INTO forms (id, title, created, updated, created_by, derived_from) "
                     "VALUES (?, ?, ?, ?, ?, ?)", (fid, title, stamp, stamp, author, derived_from))
        for position, uid in enumerate(upload_ids):
            self.execute("INSERT OR IGNORE INTO form_uploads (form_id, upload_id, position) VALUES (?, ?, ?)",
                         (fid, uid, position))
        return fid

    def form(self, fid: str) -> sqlite3.Row | None:
        return self.one("SELECT * FROM forms WHERE id = ?", (fid,))

    def forms(self, status: str | None = None) -> list[sqlite3.Row]:
        if status:
            return self.all("SELECT * FROM forms WHERE status = ? ORDER BY updated DESC", (status,))
        return self.all("SELECT * FROM forms ORDER BY updated DESC")

    def add_revision(self, fid: str, draft: FormDraft, author: str, kind: str, instruction: str | None = None,
                     provenance: dict | None = None, fingerprint: list[int] | None = None,
                     search_text: str | None = None) -> int:
        last = self.one("SELECT MAX(number) AS n FROM revisions WHERE form_id = ?", (fid,))
        number = (last["n"] or 0) + 1
        self.execute("INSERT INTO revisions (form_id, number, created, author, kind, instruction, draft, provenance) "
                     "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                     (fid, number, now(), author, kind, instruction, draft.model_dump_json(),
                      json.dumps(provenance, ensure_ascii=False) if provenance else None))
        self.execute("UPDATE forms SET title = ?, updated = ?, fingerprint = COALESCE(?, fingerprint), "
                     "search_text = COALESCE(?, search_text), status = CASE WHEN status = 'godkänd' "
                     "THEN 'utkast' ELSE status END WHERE id = ?",
                     (draft.title, now(), json.dumps(fingerprint) if fingerprint else None, search_text, fid))
        return number

    def revisions(self, fid: str) -> list[sqlite3.Row]:
        return self.all("SELECT * FROM revisions WHERE form_id = ? ORDER BY number", (fid,))

    def revision(self, fid: str, number: int | None = None) -> sqlite3.Row | None:
        if number is None:
            return self.one("SELECT * FROM revisions WHERE form_id = ? ORDER BY number DESC LIMIT 1", (fid,))
        return self.one("SELECT * FROM revisions WHERE form_id = ? AND number = ?", (fid, number))

    def draft(self, fid: str, number: int | None = None) -> FormDraft | None:
        row = self.revision(fid, number)
        return FormDraft.model_validate_json(row["draft"]) if row else None

    def approve(self, fid: str, user: str) -> None:
        self.execute("UPDATE forms SET status = 'godkänd', approved_by = ?, approved_at = ?, updated = ? WHERE id = ?",
                     (user, now(), now(), fid))

    # -- urval ----------------------------------------------------------------------------

    def valuesets(self) -> list[dict[str, Any]]:
        return [dict(r) | {"concepts": json.loads(r["concepts"])}
                for r in self.all("SELECT * FROM valuesets ORDER BY uses DESC, title")]

    def valueset(self, url: str) -> dict[str, Any] | None:
        row = self.one("SELECT * FROM valuesets WHERE url = ?", (url,))
        return dict(row) | {"concepts": json.loads(row["concepts"])} if row else None

    def save_valueset(self, url: str, name: str, title: str, concepts: list[dict], source_form: str | None) -> None:
        self.execute("INSERT INTO valuesets (url, name, title, concepts, source_form, created, uses) "
                     "VALUES (?, ?, ?, ?, ?, ?, 1) ON CONFLICT(url) DO UPDATE SET uses = uses + 1",
                     (url, name, title, json.dumps(concepts, ensure_ascii=False), source_form, now()))

    def use_valueset(self, url: str) -> None:
        self.execute("UPDATE valuesets SET uses = uses + 1 WHERE url = ?", (url,))

    # -- lärdomar ---------------------------------------------------------------------------

    def guidelines(self, status: str | None = None) -> list[sqlite3.Row]:
        if status:
            return self.all("SELECT * FROM guidelines WHERE status = ? ORDER BY id", (status,))
        return self.all("SELECT * FROM guidelines ORDER BY CASE status WHEN 'förslag' THEN 0 WHEN 'aktiv' THEN 1 "
                        "ELSE 2 END, id")

    def add_guideline(self, text: str, status: str, source: str | None, user: str | None = None) -> int:
        cur = self.execute("INSERT INTO guidelines (text, status, source, created, decided_by) VALUES (?, ?, ?, ?, ?)",
                           (text.strip(), status, source, now(), user if status != "förslag" else None))
        return cur.lastrowid

    def decide_guideline(self, gid: int, status: str, user: str) -> None:
        self.execute("UPDATE guidelines SET status = ?, decided_by = ? WHERE id = ?", (status, user, gid))

    def feedback(self, limit: int = 60) -> list[sqlite3.Row]:
        """Instruktionerna användarna gett vid iteration – underlaget för nya lärdomar."""
        return self.all("SELECT r.instruction, r.created, f.title FROM revisions r JOIN forms f ON f.id = r.form_id "
                        "WHERE r.kind = 'ai-revidering' AND r.instruction IS NOT NULL "
                        "ORDER BY r.id DESC LIMIT ?", (limit,))
