from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass

SCHEMA = """
CREATE TABLE IF NOT EXISTS apps (
    id               TEXT PRIMARY KEY,
    guild_id         INTEGER NOT NULL,
    name             TEXT NOT NULL,
    required_role_id INTEGER,
    hwid_lock        INTEGER NOT NULL DEFAULT 0,
    session_days     INTEGER NOT NULL DEFAULT 7,
    created_by       INTEGER,
    created_at       INTEGER NOT NULL,
    UNIQUE (guild_id, name COLLATE NOCASE)
);

CREATE TABLE IF NOT EXISTS login_requests (
    id                 TEXT PRIMARY KEY,
    app_id             TEXT NOT NULL REFERENCES apps(id) ON DELETE CASCADE,
    code               TEXT NOT NULL UNIQUE,
    kind               TEXT NOT NULL,
    status             TEXT NOT NULL,
    reason             TEXT,
    user_id            INTEGER,
    hwid               TEXT,
    ip                 TEXT,
    created_at         INTEGER NOT NULL,
    expires_at         INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    id         TEXT PRIMARY KEY,
    token_hash TEXT NOT NULL UNIQUE,
    app_id     TEXT NOT NULL REFERENCES apps(id) ON DELETE CASCADE,
    user_id    INTEGER NOT NULL,
    hwid       TEXT,
    ip         TEXT,
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    last_seen  INTEGER
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions (user_id);

CREATE TABLE IF NOT EXISTS hwids (
    app_id   TEXT NOT NULL REFERENCES apps(id) ON DELETE CASCADE,
    user_id  INTEGER NOT NULL,
    hwid     TEXT NOT NULL,
    bound_at INTEGER NOT NULL,
    PRIMARY KEY (app_id, user_id)
);

CREATE TABLE IF NOT EXISTS bans (
    guild_id   INTEGER NOT NULL,
    app_id     TEXT NOT NULL,
    user_id    INTEGER NOT NULL,
    reason     TEXT,
    created_by INTEGER,
    created_at INTEGER NOT NULL,
    PRIMARY KEY (guild_id, app_id, user_id)
);

CREATE TABLE IF NOT EXISTS guild_settings (
    guild_id       INTEGER PRIMARY KEY,
    log_channel_id INTEGER
);
"""


def now() -> int:
    return int(time.time())


@dataclass
class App:
    id: str
    guild_id: int
    name: str
    required_role_id: int | None
    hwid_lock: bool
    session_days: int
    created_by: int | None
    created_at: int


@dataclass
class LoginRequest:
    id: str
    app_id: str
    code: str
    kind: str
    status: str
    reason: str | None
    user_id: int | None
    hwid: str | None
    ip: str | None
    created_at: int
    expires_at: int

    @property
    def expired(self) -> bool:
        return self.expires_at < now()


@dataclass
class Session:
    id: str
    token_hash: str
    app_id: str
    user_id: int
    hwid: str | None
    ip: str | None
    created_at: int
    expires_at: int
    last_seen: int | None


class Database:
    def __init__(self, path: str):
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    @staticmethod
    def _app(row) -> App | None:
        if row is None:
            return None
        d = dict(row)
        d["hwid_lock"] = bool(d["hwid_lock"])
        return App(**d)

    def create_app(self, app: App) -> None:
        self.conn.execute(
            "INSERT INTO apps (id, guild_id, name, required_role_id, hwid_lock, session_days, created_by, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (app.id, app.guild_id, app.name, app.required_role_id, int(app.hwid_lock),
             app.session_days, app.created_by, app.created_at),
        )

    def get_app(self, app_id: str) -> App | None:
        return self._app(self.conn.execute("SELECT * FROM apps WHERE id = ?", (app_id,)).fetchone())

    def find_app(self, guild_id: int, id_or_name: str) -> App | None:
        row = self.conn.execute(
            "SELECT * FROM apps WHERE guild_id = ? AND (id = ? OR name = ? COLLATE NOCASE)",
            (guild_id, id_or_name, id_or_name),
        ).fetchone()
        return self._app(row)

    def list_apps(self, guild_id: int | None = None) -> list[App]:
        if guild_id is None:
            rows = self.conn.execute("SELECT * FROM apps ORDER BY name").fetchall()
        else:
            rows = self.conn.execute("SELECT * FROM apps WHERE guild_id = ? ORDER BY name", (guild_id,)).fetchall()
        return [self._app(r) for r in rows]

    def update_app(self, app_id: str, **fields) -> None:
        if not fields:
            return
        allowed = {"name", "required_role_id", "hwid_lock", "session_days"}
        sets, values = [], []
        for key, value in fields.items():
            if key not in allowed:
                raise ValueError(key)
            sets.append(f"{key} = ?")
            values.append(int(value) if isinstance(value, bool) else value)
        values.append(app_id)
        self.conn.execute(f"UPDATE apps SET {', '.join(sets)} WHERE id = ?", values)

    def delete_app(self, app_id: str) -> None:
        self.conn.execute("DELETE FROM apps WHERE id = ?", (app_id,))
        self.conn.execute("DELETE FROM bans WHERE app_id = ?", (app_id,))

    def create_request(self, req: LoginRequest) -> None:
        self.conn.execute(
            "INSERT INTO login_requests (id, app_id, code, kind, status, reason, user_id, hwid, ip,"
            " created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (req.id, req.app_id, req.code, req.kind, req.status, req.reason, req.user_id, req.hwid, req.ip,
             req.created_at, req.expires_at),
        )

    def get_request(self, request_id: str) -> LoginRequest | None:
        row = self.conn.execute("SELECT * FROM login_requests WHERE id = ?", (request_id,)).fetchone()
        return LoginRequest(**dict(row)) if row else None

    def get_request_by_code(self, code: str) -> LoginRequest | None:
        row = self.conn.execute("SELECT * FROM login_requests WHERE code = ?", (code,)).fetchone()
        return LoginRequest(**dict(row)) if row else None

    def update_request(self, request_id: str, *, expect_status: str | None = None, **fields) -> bool:
        sets = ", ".join(f"{k} = ?" for k in fields)
        values = list(fields.values()) + [request_id]
        sql = f"UPDATE login_requests SET {sets} WHERE id = ?"
        if expect_status is not None:
            sql += " AND status = ?"
            values.append(expect_status)
        return self.conn.execute(sql, values).rowcount > 0

    def create_session(self, s: Session) -> None:
        self.conn.execute(
            "INSERT INTO sessions (id, token_hash, app_id, user_id, hwid, ip, created_at, expires_at, last_seen)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (s.id, s.token_hash, s.app_id, s.user_id, s.hwid, s.ip, s.created_at, s.expires_at, s.last_seen),
        )

    def get_session_by_hash(self, token_hash: str) -> Session | None:
        row = self.conn.execute("SELECT * FROM sessions WHERE token_hash = ?", (token_hash,)).fetchone()
        return Session(**dict(row)) if row else None

    def touch_session(self, session_id: str) -> None:
        self.conn.execute("UPDATE sessions SET last_seen = ? WHERE id = ?", (now(), session_id))

    def list_sessions(self, user_id: int, guild_id: int | None = None) -> list[tuple[Session, App]]:
        sql = ("SELECT s.* FROM sessions s JOIN apps a ON a.id = s.app_id"
               " WHERE s.user_id = ? AND s.expires_at > ?")
        params: list = [user_id, now()]
        if guild_id is not None:
            sql += " AND a.guild_id = ?"
            params.append(guild_id)
        result = []
        for row in self.conn.execute(sql + " ORDER BY s.created_at DESC", params).fetchall():
            session = Session(**dict(row))
            result.append((session, self.get_app(session.app_id)))
        return result

    def delete_session(self, session_id: str) -> bool:
        return self.conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,)).rowcount > 0

    def delete_user_sessions(self, user_id: int, *, app_id: str | None = None, guild_id: int | None = None) -> int:
        sql = "DELETE FROM sessions WHERE user_id = ?"
        params: list = [user_id]
        if app_id is not None:
            sql += " AND app_id = ?"
            params.append(app_id)
        if guild_id is not None:
            sql += " AND app_id IN (SELECT id FROM apps WHERE guild_id = ?)"
            params.append(guild_id)
        return self.conn.execute(sql, params).rowcount

    def get_hwid(self, app_id: str, user_id: int) -> str | None:
        row = self.conn.execute("SELECT hwid FROM hwids WHERE app_id = ? AND user_id = ?", (app_id, user_id)).fetchone()
        return row["hwid"] if row else None

    def bind_hwid(self, app_id: str, user_id: int, hwid: str) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO hwids (app_id, user_id, hwid, bound_at) VALUES (?, ?, ?, ?)",
            (app_id, user_id, hwid, now()),
        )

    def reset_hwid(self, app_id: str, user_id: int) -> bool:
        return self.conn.execute("DELETE FROM hwids WHERE app_id = ? AND user_id = ?", (app_id, user_id)).rowcount > 0

    def ban(self, guild_id: int, app_id: str, user_id: int, reason: str | None, by: int) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO bans (guild_id, app_id, user_id, reason, created_by, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (guild_id, app_id, user_id, reason, by, now()),
        )

    def unban(self, guild_id: int, app_id: str, user_id: int) -> bool:
        return self.conn.execute(
            "DELETE FROM bans WHERE guild_id = ? AND app_id = ? AND user_id = ?", (guild_id, app_id, user_id)
        ).rowcount > 0

    def get_ban(self, app: App, user_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM bans WHERE guild_id = ? AND user_id = ? AND app_id IN (?, '*')",
            (app.guild_id, user_id, app.id),
        ).fetchone()

    def get_log_channel(self, guild_id: int) -> int | None:
        row = self.conn.execute("SELECT log_channel_id FROM guild_settings WHERE guild_id = ?", (guild_id,)).fetchone()
        return row["log_channel_id"] if row else None

    def set_log_channel(self, guild_id: int, channel_id: int | None) -> None:
        self.conn.execute(
            "INSERT INTO guild_settings (guild_id, log_channel_id) VALUES (?, ?)"
            " ON CONFLICT(guild_id) DO UPDATE SET log_channel_id = excluded.log_channel_id",
            (guild_id, channel_id),
        )

    def cleanup(self) -> None:
        t = now()
        self.conn.execute("DELETE FROM login_requests WHERE expires_at < ?", (t - 3600,))
        self.conn.execute("DELETE FROM sessions WHERE expires_at < ?", (t,))
