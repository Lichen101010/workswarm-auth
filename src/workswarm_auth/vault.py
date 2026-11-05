"""Encrypted SQLite vault: connection credentials, account bindings, one-use flow capabilities and audit.

Connection revisions fence late callbacks and refreshes. A pending authorization flow is
tracked separately (``flow``) so that starting a reconnection never removes credentials
that are still in use; they are replaced only when the new grant has been verified.
"""
import hashlib
import json
import os
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from cryptography.fernet import Fernet

FLOW_TTL = 600


class Vault:
    def __init__(self, root):
        root = Path(root)
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name != "nt":
            root.chmod(0o700)
        self.path = root / "vault.sqlite3"
        configured = os.environ.get("WORKSWARM_VAULT_KEY_FILE")
        key_path = Path(configured) if configured else root / "vault.key"
        # Never silently regenerate an encryption key for an existing database.
        if configured and not key_path.exists():
            raise RuntimeError("Configured vault key file is missing.")
        if not key_path.exists():
            if self.path.exists():
                raise RuntimeError("Vault key is missing; restore it or reset the disconnected vault.")
            fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(Fernet.generate_key())
        self.cipher = Fernet(key_path.read_bytes())
        with self.db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS flows (
                    digest TEXT PRIMARY KEY, kind TEXT NOT NULL,
                    expires REAL NOT NULL, payload BLOB NOT NULL);
                CREATE TABLE IF NOT EXISTS connections (
                    owner TEXT NOT NULL, cid TEXT NOT NULL, provider TEXT NOT NULL,
                    label TEXT NOT NULL, revision INTEGER NOT NULL, tokens BLOB,
                    authorization TEXT NOT NULL, validation TEXT NOT NULL,
                    checked_at REAL, last_error TEXT, flow TEXT, flow_expires REAL, completed_flow TEXT,
                    PRIMARY KEY(owner,cid));
                CREATE TABLE IF NOT EXISTS accounts (owner TEXT, cid TEXT, opaque_id TEXT NOT NULL, identity BLOB NOT NULL, PRIMARY KEY(owner,cid));
                CREATE TABLE IF NOT EXISTS audit (id INTEGER PRIMARY KEY, at REAL NOT NULL, owner TEXT, cid TEXT, event TEXT NOT NULL, result TEXT NOT NULL);
            """)
            # Vaults created by v0.3 have no pending-flow columns.
            columns = {row[1] for row in db.execute("PRAGMA table_info(connections)")}
            for column, kind in (("flow", "TEXT"), ("flow_expires", "REAL"), ("completed_flow", "TEXT")):
                if column not in columns:
                    db.execute(f"ALTER TABLE connections ADD COLUMN {column} {kind}")
        if os.name != "nt":
            if not configured:
                key_path.chmod(0o600)
            self.path.chmod(0o600)

    @contextmanager
    def db(self):
        connection = sqlite3.connect(self.path, timeout=10)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def encode(self, value):
        return self.cipher.encrypt(json.dumps(value).encode())

    def decode(self, value):
        return json.loads(self.cipher.decrypt(value))

    # One-use, expiring capabilities (connect tickets, OAuth states). Only digests are stored.
    def issue(self, kind, payload, ttl=FLOW_TTL):
        token = secrets.token_urlsafe(32)
        with self.db() as db:
            db.execute("DELETE FROM flows WHERE expires < ?", (time.time(),))
            db.execute("INSERT INTO flows VALUES (?, ?, ?, ?)", (
                hashlib.sha256(token.encode()).hexdigest(), kind, time.time() + ttl, self.encode(payload)))
        return token

    def consume(self, token, kind):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            digest = hashlib.sha256(token.encode()).hexdigest()
            row = db.execute("SELECT expires, payload FROM flows WHERE digest=? AND kind=?", (digest, kind)).fetchone()
            db.execute("DELETE FROM flows WHERE digest=? AND kind=?", (digest, kind))
        if not row or row[0] < time.time():
            return None
        return self.decode(row[1])

    def account(self, owner, cid, private=False):
        with self.db() as db:
            row = db.execute("SELECT opaque_id,identity FROM accounts WHERE owner=? AND cid=?", (owner, cid)).fetchone()
        if not row:
            return None
        identity = self.decode(row[1])
        if private:
            return identity
        return {"account_id": row[0], "family": identity["family"], "label": identity["label"]}

    def _bind_account(self, db, owner, cid, identity):
        """Pin the connection to an account inside the caller's transaction; a different account raises."""
        row = db.execute("SELECT opaque_id,identity FROM accounts WHERE owner=? AND cid=?", (owner, cid)).fetchone()
        if row:
            old = self.decode(row[1])
            if (old["family"], old["subject"]) != (identity["family"], identity["subject"]):
                raise ValueError("account_mismatch_use_new_connection")
            opaque = row[0]
        else:
            # Opaque IDs are owner-scoped; service subjects never appear in logs or reports.
            other = db.execute("SELECT opaque_id,identity FROM accounts WHERE owner=?", (owner,)).fetchall()
            opaque = "acct_" + secrets.token_hex(16)
            for candidate_id, encrypted in other:
                candidate = self.decode(encrypted)
                if (candidate["family"], candidate["subject"]) == (identity["family"], identity["subject"]):
                    opaque = candidate_id
                    break
        db.execute("INSERT OR REPLACE INTO accounts VALUES (?,?,?,?)", (owner, cid, opaque, self.encode(identity)))

    def bind(self, owner, cid, revision, identity):
        """Re-check (or first pin) the account behind credentials that are already stored."""
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute("SELECT revision FROM connections WHERE owner=? AND cid=?", (owner, cid)).fetchone()
            if not current or current[0] != revision:
                return False
            self._bind_account(db, owner, cid, identity)
        return True

    def audit(self, owner, cid, event, result):
        # Callers pass fixed event/result codes, never API data, URLs or tokens.
        with self.db() as db:
            db.execute("INSERT INTO audit(at,owner,cid,event,result) VALUES (?,?,?,?,?)", (time.time(), owner, cid, event, result))

    def events(self, owner, limit=100):
        with self.db() as db:
            rows = db.execute("SELECT at,cid,event,result FROM audit WHERE owner=? ORDER BY id DESC LIMIT ?", (owner, limit)).fetchall()
        return [dict(zip(("at", "connection_id", "event", "result"), row)) for row in rows]

    _COLUMNS = "owner,cid,provider,label,revision,authorization,validation,checked_at,last_error,flow,flow_expires,completed_flow"

    @staticmethod
    def _public(row):
        if not row:
            return None
        result = dict(zip(("owner", "connection_id", "provider", "label", "revision",
                           "authorization", "validation", "checked_at", "last_error"), row[:9]))
        flow, expires = row[9], row[10]
        result["pending"] = bool(flow and expires and expires > time.time())
        result["flow"] = flow if result["pending"] else None
        result["completed_attempt_id"] = row[11]
        return result

    def list(self, owner):
        with self.db() as db:
            rows = db.execute(f"SELECT {self._COLUMNS} FROM connections WHERE owner=? ORDER BY cid", (owner,)).fetchall()
        return [self._public(row) for row in rows]

    def info(self, owner, cid):
        with self.db() as db:
            row = db.execute(f"SELECT {self._COLUMNS} FROM connections WHERE owner=? AND cid=?", (owner, cid)).fetchone()
        return self._public(row)

    def begin(self, owner, cid, provider, label):
        """Open an authorization flow. Existing credentials stay usable until it completes."""
        flow = secrets.token_hex(8)
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT provider,revision,label FROM connections WHERE owner=? AND cid=?", (owner, cid)).fetchone()
            if old and old[0] != provider:
                raise ValueError("connection_provider_cannot_change")
            if old:
                db.execute("UPDATE connections SET label=?,last_error=NULL,flow=?,flow_expires=? WHERE owner=? AND cid=?",
                           (label or old[2], flow, time.time() + FLOW_TTL, owner, cid))
                revision = old[1]
            else:
                revision = 1
                db.execute("""INSERT INTO connections
                    (owner,cid,provider,label,revision,tokens,authorization,validation,checked_at,last_error,flow,flow_expires)
                    VALUES (?,?,?,?,?,NULL,'pending','not_checked',NULL,NULL,?,?)""",
                           (owner, cid, provider, label, revision, flow, time.time() + FLOW_TTL))
        return revision, flow

    def complete(self, owner, cid, revision, flow, tokens, identity=None):
        """Atomically store a new grant and its account pin; the revision bump fences old refreshes.

        Nothing is written if the flow is no longer current or the account differs, so a failed
        or cancelled first authorization never leaves an account binding behind.
        """
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            cursor = db.execute("""UPDATE connections SET tokens=?,authorization='authorized',validation='not_checked',
                checked_at=NULL,last_error=NULL,flow=NULL,flow_expires=NULL,completed_flow=?,revision=revision+1
                WHERE owner=? AND cid=? AND revision=? AND flow=?""", (self.encode(tokens), flow, owner, cid, revision, flow))
            if cursor.rowcount != 1:
                return False
            if identity is not None:
                self._bind_account(db, owner, cid, identity)  # ValueError rolls back the update
        return True

    def fail_flow(self, owner, cid, revision, flow, error):
        """End a flow without touching existing credentials; only a connection without any is marked failed."""
        with self.db() as db:
            cursor = db.execute("""UPDATE connections SET flow=NULL,flow_expires=NULL,last_error=?,
                authorization=CASE WHEN tokens IS NULL THEN 'failed' ELSE authorization END
                WHERE owner=? AND cid=? AND revision=? AND flow=?""", (error, owner, cid, revision, flow))
        return cursor.rowcount == 1

    def credentials(self, owner, cid):
        with self.db() as db:
            row = db.execute("SELECT tokens FROM connections WHERE owner=? AND cid=?", (owner, cid)).fetchone()
        return self.decode(row[0]) if row and row[0] else None

    def save(self, owner, cid, revision, tokens):
        """Store refreshed credentials for an already authorized connection."""
        with self.db() as db:
            cursor = db.execute("UPDATE connections SET tokens=? WHERE owner=? AND cid=? AND revision=? AND authorization='authorized'",
                                (self.encode(tokens), owner, cid, revision))
        return cursor.rowcount == 1

    def fail_validation(self, owner, cid, revision, error):
        with self.db() as db:
            db.execute("UPDATE connections SET validation='failed',checked_at=?,last_error=? WHERE owner=? AND cid=? AND revision=?",
                       (time.time(), error, owner, cid, revision))

    def verified(self, owner, cid, revision):
        with self.db() as db:
            cursor = db.execute("UPDATE connections SET validation='passed',checked_at=?,last_error=NULL WHERE owner=? AND cid=? AND revision=? AND authorization='authorized'",
                                (time.time(), owner, cid, revision))
        return cursor.rowcount == 1

    def disconnect(self, owner, cid, error=None, expected_revision=None):
        with self.db() as db:
            query = ("UPDATE connections SET revision=revision+1,tokens=NULL,authorization='disconnected',validation='not_checked',"
                     "checked_at=NULL,last_error=?,flow=NULL,flow_expires=NULL WHERE owner=? AND cid=?")
            values = (error, owner, cid)
            if expected_revision is not None:
                query += " AND revision=?"
                values += (expected_revision,)
            cursor = db.execute(query, values)
        return cursor.rowcount == 1
