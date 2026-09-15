from __future__ import annotations

import hashlib
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

PAIRING_TTL_SECONDS = 300
PAIRING_MAX_ATTEMPTS = 5


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class PairingError(ValueError):
    pass


class DeviceStore:
    """Computer-owned authorization records. Raw bearer secrets are never stored."""

    def __init__(self, directory: Path) -> None:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        directory.chmod(0o700)
        self.path = directory / "devices.sqlite3"
        # Restrict the file before sqlite opens it (journals inherit its mode).
        self.path.touch(mode=0o600, exist_ok=True)
        self.path.chmod(0o600)
        with self._connection() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS invitation (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    secret_hash TEXT NOT NULL,
                    expires_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS devices (
                    device_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    secret_hash TEXT NOT NULL UNIQUE,
                    verification_code TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('pending', 'approved', 'revoked')),
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    approved_at REAL,
                    revoked_at REAL
                );
                CREATE TABLE IF NOT EXISTS pairing_attempts (
                    device_id TEXT PRIMARY KEY,
                    attempts INTEGER NOT NULL DEFAULT 0
                );
            """)

    @contextmanager
    def _connection(self):
        conn = sqlite3.connect(self.path, timeout=5)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def create_invitation(self) -> dict:
        secret = secrets.token_urlsafe(32)
        now = time.time()
        expires_at = now + PAIRING_TTL_SECONDS
        with self._connection() as conn:
            conn.execute("DELETE FROM devices WHERE status = 'pending' AND expires_at <= ?", (now,))
            conn.execute("DELETE FROM pairing_attempts WHERE device_id NOT IN (SELECT device_id FROM devices)")
            conn.execute(
                "INSERT OR REPLACE INTO invitation VALUES (1, ?, ?)",
                (_digest(secret), expires_at),
            )
        return {"invitation": secret, "expires_at": expires_at}

    def claim(self, invitation: str, name: str, device_secret: str) -> dict:
        now = time.time()
        device_id = uuid4().hex
        verification_code = f"{secrets.randbelow(1_000_000):06d}"
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            deleted = conn.execute(
                "DELETE FROM invitation WHERE secret_hash = ? AND expires_at > ?",
                (_digest(invitation), now),
            ).rowcount
            if deleted != 1:
                raise PairingError("Invitation is invalid, expired, or already used")
            try:
                conn.execute(
                    "INSERT INTO devices VALUES (?, ?, ?, ?, 'pending', ?, ?, NULL, NULL)",
                    (device_id, name, _digest(device_secret), verification_code,
                     now, now + PAIRING_TTL_SECONDS),
                )
                conn.execute("INSERT INTO pairing_attempts (device_id) VALUES (?)", (device_id,))
            except sqlite3.IntegrityError as exc:
                raise PairingError("Use a new device credential") from exc
        return {"device_id": device_id, "status": "pending",
                "verification_code": verification_code, "expires_at": now + PAIRING_TTL_SECONDS}

    def lookup(self, secret: str) -> dict | None:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM devices WHERE secret_hash = ?", (_digest(secret),)
            ).fetchone()
        if row is None:
            return None
        result = self._public(row)
        if result["status"] == "pending" and result["expires_at"] <= time.time():
            result["status"] = "expired"
        return result

    def list_devices(self) -> list[dict]:
        with self._connection() as conn:
            rows = conn.execute("SELECT * FROM devices ORDER BY created_at DESC").fetchall()
        return [self._public(row) for row in rows
                if row["status"] != "pending" or row["expires_at"] > time.time()]

    @staticmethod
    def _public(row: sqlite3.Row) -> dict:
        return {key: row[key] for key in (
            "device_id", "name", "verification_code", "status", "created_at",
            "expires_at", "approved_at", "revoked_at",
        )}

    def approve(self, device_id: str, verification_code: str) -> None:
        with self._connection() as conn:
            changed = conn.execute(
                "UPDATE devices SET status = 'approved', approved_at = ? "
                "WHERE device_id = ? AND verification_code = ? AND status = 'pending' "
                "AND expires_at > ?",
                (time.time(), device_id, verification_code, time.time()),
            ).rowcount
        if changed != 1:
            raise PairingError("Pairing is expired, unavailable, or the verification code differs")

    def verify(self, device_id: str, verification_code: str) -> None:
        """A credential holder proves possession of the code shown only on the computer.

        Attempts and approval serialize together and survive process restarts. Pending
        records created before this protocol are ineligible: their code was public.
        """
        error = None
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT d.*, a.attempts FROM devices d LEFT JOIN pairing_attempts a "
                "ON d.device_id = a.device_id WHERE d.device_id = ?", (device_id,),
            ).fetchone()
            if row is None or row["status"] in {"revoked"}:
                error = "连接申请已失效，请在电脑上重新生成二维码"
            elif row["status"] == "approved":
                return  # An already-approved credential may retry a lost response.
            elif row["expires_at"] <= time.time() or row["attempts"] is None:
                error = "连接申请已过期，请在电脑上重新生成二维码"
            elif secrets.compare_digest(row["verification_code"], verification_code):
                conn.execute("UPDATE devices SET status = 'approved', approved_at = ? WHERE device_id = ?",
                             (time.time(), device_id))
            else:
                attempts = row["attempts"] + 1
                conn.execute("UPDATE pairing_attempts SET attempts = ? WHERE device_id = ?", (attempts, device_id))
                if attempts >= PAIRING_MAX_ATTEMPTS:
                    conn.execute("UPDATE devices SET status = 'revoked', revoked_at = ? WHERE device_id = ?",
                                 (time.time(), device_id))
                    error = "输入错误次数过多，请在电脑上重新生成二维码"
                else:
                    error = f"配对码不正确，还可尝试 {PAIRING_MAX_ATTEMPTS - attempts} 次"
        # Raise after commit so rejected attempts cannot reset the counter.
        if error:
            raise PairingError(error)

    def revoke(self, device_id: str) -> None:
        with self._connection() as conn:
            changed = conn.execute(
                "UPDATE devices SET status = 'revoked', revoked_at = ? WHERE device_id = ?",
                (time.time(), device_id),
            ).rowcount
        if changed != 1:
            raise PairingError("Device not found")
