"""Desktop platform session only. Never opens a provider vault or SQLite database."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import secrets

from cryptography.fernet import Fernet, InvalidToken

from .errors import AuthError


@contextmanager
def session_lock(root):
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (root / "session.lock").open("a+b") as handle:
        handle.seek(0)
        handle.write(b"0")
        handle.flush()
        if os.name == "nt":
            import msvcrt
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class SessionStore:
    def __init__(self, root):
        root = Path(root)
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name != "nt":
            root.chmod(0o700)
        self.path = root / "session.enc"
        # Keep v0.5's filename so existing encrypted platform sessions still open.
        # There is no SQLite creation or server vault key environment lookup.
        key_path = root / "vault.key"
        with session_lock(root):
            if not key_path.exists():
                if self.path.exists():
                    raise AuthError("cloud_session_key_missing", 401)
                fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "wb") as handle:
                    handle.write(Fernet.generate_key())
            self.cipher = Fernet(key_path.read_bytes())
            if os.name != "nt":
                key_path.chmod(0o600)

    def read(self, profile):
        if not self.path.exists():
            return None
        try:
            value = json.loads(self.cipher.decrypt(self.path.read_bytes()))
            return value["tokens"] if value["profile"] == profile else None
        except (InvalidToken, ValueError, KeyError, TypeError):
            raise AuthError("cloud_session_unreadable", 401) from None

    def save(self, profile, tokens):
        temporary = self.path.with_name("session-" + secrets.token_hex(8) + ".tmp")
        data = self.cipher.encrypt(json.dumps({"profile": profile, "tokens": tokens}).encode())
        try:
            fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(self.path)
        finally:
            temporary.unlink(missing_ok=True)
