import json
import secrets
import sqlite3
import time
from contextlib import contextmanager
from cryptography.fernet import Fernet


class Store:
    def __init__(self, settings):
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        self.path = settings.data_dir / "portal.sqlite3"
        self.cipher = Fernet(settings.encryption_key.encode())
        with self.connection() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY, user_id TEXT NOT NULL, name TEXT NOT NULL,
                    token BLOB NOT NULL, csrf TEXT NOT NULL, expires INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS requests (
                    id TEXT PRIMARY KEY, kind TEXT NOT NULL, provider_id INTEGER NOT NULL,
                    title TEXT NOT NULL, year INTEGER, folder TEXT NOT NULL,
                    upstream_id INTEGER, state TEXT NOT NULL DEFAULT 'requested',
                    detail TEXT NOT NULL DEFAULT '', progress INTEGER,
                    snapshot TEXT NOT NULL DEFAULT '{}', series_type TEXT NOT NULL DEFAULT 'standard', created INTEGER NOT NULL,
                    updated INTEGER NOT NULL, UNIQUE(kind, provider_id)
                );
                CREATE TABLE IF NOT EXISTS request_users (
                    request_id TEXT NOT NULL REFERENCES requests(id), user_id TEXT NOT NULL,
                    PRIMARY KEY(request_id, user_id)
                );
            """)
            # Existing portal databases can retain requests when adopting this release.
            columns = {row[1] for row in db.execute("PRAGMA table_info(requests)")}
            if "series_type" not in columns:
                db.execute("ALTER TABLE requests ADD COLUMN series_type TEXT NOT NULL DEFAULT 'standard'")
                if settings.anime_root not in (settings.tv_root, settings.cartoons_root):
                    db.execute("UPDATE requests SET series_type='anime' WHERE kind='series' AND folder=?", (settings.anime_root,))
        self.path.chmod(0o600)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def new_session(self, user_id, name, token, seconds):
        sid, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        with self.connection() as db:
            db.execute("DELETE FROM sessions WHERE expires < ?", (int(time.time()),))
            db.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?)", (sid, user_id, name, self.cipher.encrypt(token.encode()), csrf, int(time.time()) + seconds))
        return sid, csrf

    def session(self, sid):
        if not sid:
            return None
        with self.connection() as db:
            row = db.execute("SELECT * FROM sessions WHERE id=? AND expires>?", (sid, int(time.time()))).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["token"] = self.cipher.decrypt(result["token"]).decode()
        return result

    def logout(self, sid):
        with self.connection() as db:
            db.execute("DELETE FROM sessions WHERE id=?", (sid,))

    def add_request(self, kind, provider_id, title, year, folder, user_id, series_type="standard"):
        now = int(time.time())
        with self.connection() as db:
            db.execute("INSERT OR IGNORE INTO requests (id,kind,provider_id,title,year,folder,series_type,created,updated) VALUES (?,?,?,?,?,?,?,?,?)",
                       (secrets.token_urlsafe(18), kind, provider_id, title, year, folder, series_type, now, now))
            row = db.execute("SELECT * FROM requests WHERE kind=? AND provider_id=?", (kind, provider_id)).fetchone()
            db.execute("INSERT OR IGNORE INTO request_users VALUES (?,?)", (row["id"], user_id))
        return dict(row)

    def update(self, rid, **fields):
        allowed = {"upstream_id", "state", "detail", "progress", "snapshot"}
        if not fields or not set(fields) <= allowed:
            raise ValueError("Invalid request update")
        fields["updated"] = int(time.time())
        with self.connection() as db:
            db.execute("UPDATE requests SET " + ",".join(key + "=?" for key in fields) + " WHERE id=?", (*fields.values(), rid))

    def requests(self, user_id=None):
        with self.connection() as db:
            if user_id:
                rows = db.execute("SELECT r.* FROM requests r JOIN request_users u ON r.id=u.request_id WHERE u.user_id=? ORDER BY r.created DESC LIMIT 100", (user_id,)).fetchall()
            else:
                rows = db.execute("SELECT * FROM requests WHERE state != 'failed' AND (state != 'available' OR kind='series') ORDER BY created").fetchall()
        return [dict(row) for row in rows]

    def get(self, rid, user_id=None):
        with self.connection() as db:
            if user_id:
                row = db.execute("SELECT r.* FROM requests r JOIN request_users u ON r.id=u.request_id WHERE r.id=? AND u.user_id=?", (rid, user_id)).fetchone()
            else:
                row = db.execute("SELECT * FROM requests WHERE id=?", (rid,)).fetchone()
        return dict(row) if row else None

    def request_session(self, rid):
        with self.connection() as db:
            row = db.execute("SELECT s.id FROM sessions s JOIN request_users u ON s.user_id=u.user_id WHERE u.request_id=? AND s.expires>? ORDER BY s.expires DESC LIMIT 1", (rid, int(time.time()))).fetchone()
        return self.session(row["id"]) if row else None
