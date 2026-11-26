import hashlib
import hmac
import json
import os
import secrets
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable

BASE_DIR = Path(__file__).resolve().parent
SESSIONS_PATH = BASE_DIR / "auth_sessions.json"

SESSION_COOKIE_NAME = "scanner_session"
SESSION_TTL_SECONDS = 30 * 24 * 60 * 60  # 30 days
QR_TOKEN_TTL_SECONDS = 5 * 60  # 5 minutes
MAX_SESSIONS = 30
QR_TOKEN_BYTES = 9  # 72 bits -> 12 URL-safe base64 chars
SESSION_TOKEN_BYTES = 32  # 256 bits


def is_auth_enabled() -> bool:
    raw = os.environ.get("SCANNER_AUTH_ENABLED", "1").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def _hash_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    tmp_path: str | None = None
    try:
        fd, tmp_path = tempfile.mkstemp(
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
        )
        os.chmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(data, ensure_ascii=False, indent=2))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
        os.chmod(path, 0o600)
        tmp_path = None
    finally:
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


class AuthManager:
    """QR ワンタイムトークンと 30 日セッション Cookie を管理する。"""

    def __init__(
        self,
        sessions_path: Path = SESSIONS_PATH,
        *,
        session_ttl_seconds: int = SESSION_TTL_SECONDS,
        qr_token_ttl_seconds: int = QR_TOKEN_TTL_SECONDS,
        now_fn: Callable[[], float] | None = None,
    ) -> None:
        self.sessions_path = Path(sessions_path)
        self.session_ttl_seconds = session_ttl_seconds
        self.qr_token_ttl_seconds = qr_token_ttl_seconds
        self._now = now_fn or time.time
        self._lock = threading.Lock()
        self._qr_token: str = ""
        self._qr_expires_at: float = 0.0

    def _load_sessions_unlocked(self) -> list[dict[str, Any]]:
        if not self.sessions_path.exists():
            return []
        try:
            data = json.loads(self.sessions_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        raw_list = data.get("sessions", []) if isinstance(data, dict) else []
        now = self._now()
        valid: list[dict[str, Any]] = []
        for item in raw_list:
            if not isinstance(item, dict):
                continue
            token_hash = item.get("hash")
            expires_at = item.get("expires_at")
            created_at = item.get("created_at", now)
            if (
                isinstance(token_hash, str)
                and len(token_hash) == 64
                and isinstance(expires_at, (int, float))
                and expires_at > now
            ):
                valid.append(
                    {
                        "hash": token_hash,
                        "created_at": float(created_at),
                        "expires_at": float(expires_at),
                    }
                )
        return valid

    def _save_sessions_unlocked(self, sessions: list[dict[str, Any]]) -> None:
        trimmed = sorted(sessions, key=lambda s: s["expires_at"], reverse=True)[:MAX_SESSIONS]
        _atomic_write_json(self.sessions_path, {"sessions": trimmed})

    def _rotate_qr_unlocked(self) -> tuple[str, float]:
        self._qr_token = secrets.token_urlsafe(QR_TOKEN_BYTES)
        self._qr_expires_at = self._now() + self.qr_token_ttl_seconds
        return self._qr_token, self._qr_expires_at

    def get_qr_token(self, *, force_rotate: bool = False) -> tuple[str, float]:
        with self._lock:
            now = self._now()
            if force_rotate or not self._qr_token or now >= self._qr_expires_at:
                return self._rotate_qr_unlocked()
            return self._qr_token, self._qr_expires_at

    def consume_qr_token(self, token: str) -> dict[str, Any] | None:
        if not token or not isinstance(token, str):
            return None
        with self._lock:
            now = self._now()
            if not self._qr_token or now >= self._qr_expires_at:
                return None
            if not hmac.compare_digest(self._qr_token, token.strip()):
                return None

            # ワンタイム使用後すぐに新しい QR トークンへ更新する
            self._rotate_qr_unlocked()

            raw_session = secrets.token_urlsafe(SESSION_TOKEN_BYTES)
            expires_at = now + self.session_ttl_seconds
            sessions = self._load_sessions_unlocked()
            sessions.append(
                {
                    "hash": _hash_token(raw_session),
                    "created_at": now,
                    "expires_at": expires_at,
                }
            )
            self._save_sessions_unlocked(sessions)
            return {
                "token": raw_session,
                "created_at": now,
                "expires_at": expires_at,
                "max_age": self.session_ttl_seconds,
            }

    def verify_session(self, raw_token: str | None) -> dict[str, Any] | None:
        if not raw_token or not isinstance(raw_token, str):
            return None
        target_hash = _hash_token(raw_token)
        with self._lock:
            now = self._now()
            sessions = self._load_sessions_unlocked()
            for item in sessions:
                if hmac.compare_digest(item["hash"], target_hash):
                    remaining = max(0.0, item["expires_at"] - now)
                    return {
                        "created_at": item["created_at"],
                        "expires_at": item["expires_at"],
                        "remaining_seconds": int(remaining),
                        "active_sessions": len(sessions),
                    }
            return None

    def refresh_session(self, raw_token: str | None) -> dict[str, Any] | None:
        """既存セッションを検証し、トークンをローテーションしつつ有効期限を 30 日延長する。"""
        if not raw_token or not isinstance(raw_token, str):
            return None
        target_hash = _hash_token(raw_token)
        with self._lock:
            now = self._now()
            sessions = self._load_sessions_unlocked()
            matched_idx: int | None = None
            for idx, item in enumerate(sessions):
                if hmac.compare_digest(item["hash"], target_hash):
                    matched_idx = idx
                    break
            if matched_idx is None:
                return None

            new_raw = secrets.token_urlsafe(SESSION_TOKEN_BYTES)
            new_expires = now + self.session_ttl_seconds
            sessions[matched_idx] = {
                "hash": _hash_token(new_raw),
                "created_at": now,
                "expires_at": new_expires,
            }
            self._save_sessions_unlocked(sessions)
            return {
                "token": new_raw,
                "created_at": now,
                "expires_at": new_expires,
                "remaining_seconds": int(self.session_ttl_seconds),
                "max_age": self.session_ttl_seconds,
                "active_sessions": len(sessions),
            }

    def revoke_session(self, raw_token: str | None) -> bool:
        if not raw_token or not isinstance(raw_token, str):
            return False
        target_hash = _hash_token(raw_token)
        with self._lock:
            sessions = self._load_sessions_unlocked()
            kept = [s for s in sessions if not hmac.compare_digest(s["hash"], target_hash)]
            if len(kept) == len(sessions):
                return False
            self._save_sessions_unlocked(kept)
            return True

    def revoke_all_sessions(self) -> int:
        with self._lock:
            sessions = self._load_sessions_unlocked()
            count = len(sessions)
            self._save_sessions_unlocked([])
            return count


auth_manager = AuthManager()
