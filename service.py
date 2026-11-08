from __future__ import annotations

import hashlib
import re
import secrets
import sqlite3
from dataclasses import dataclass
from typing import Any, Protocol

from db import App, Database, LoginRequest, Session, now

CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
CODE_LENGTH = 8
MAX_HWID_LENGTH = 256

MESSAGES = {
    "app_not_found": "Nie znaleziono aplikacji.",
    "not_member": "Musisz być członkiem serwera Discord tej aplikacji.",
    "missing_role": "Nie masz wymaganej roli, aby korzystać z tej aplikacji.",
    "banned": "Masz zablokowany dostęp do tej aplikacji.",
    "hwid_required": "Ta aplikacja wymaga identyfikatora urządzenia (HWID).",
    "hwid_mismatch": "To konto jest przypisane do innego urządzenia. Poproś administratora o reset HWID.",
    "invalid_hwid": "Nieprawidłowy identyfikator urządzenia.",
    "invalid_code": "Nieprawidłowy lub wygasły kod.",
    "request_not_found": "Nie znaleziono prośby o logowanie.",
    "expired": "Kod wygasł. Spróbuj ponownie.",
    "already_used": "Ten kod został już użyty.",
    "denied_by_user": "Logowanie zostało odrzucone na Discordzie.",
    "invalid_token": "Sesja jest nieprawidłowa lub wygasła. Zaloguj się ponownie.",
}


class AuthError(Exception):
    def __init__(self, code: str, status: int = 400, message: str | None = None):
        self.code = code
        self.status = status
        self.message = message or MESSAGES.get(code, code)
        super().__init__(self.message)


@dataclass
class Access:
    ok: bool
    user: dict[str, Any] | None = None
    reason: str | None = None
    extra: str | None = None

    @property
    def message(self) -> str:
        msg = MESSAGES.get(self.reason or "", self.reason or "")
        return f"{msg} Powód: {self.extra}" if self.extra else msg


class Provider(Protocol):
    async def get_member_info(self, app: App, user_id: int, *, fresh: bool = False) -> dict[str, Any] | None:
        ...

    async def log_event(self, event: str, app: App, user_id: int | None, **details: Any) -> None:
        ...


def generate_code() -> str:
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))


def normalize_code(code: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (code or "").upper())


def format_code(code: str) -> str:
    half = len(code) // 2
    return f"{code[:half]}-{code[half:]}"


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def clean_hwid(hwid: Any) -> str | None:
    if hwid is None or hwid == "":
        return None
    if not isinstance(hwid, str) or len(hwid) > MAX_HWID_LENGTH:
        raise AuthError("invalid_hwid")
    return hwid.strip() or None


@dataclass
class IssuedSession:
    token: str
    session: Session
    app: App
    user: dict[str, Any]

    def to_json(self) -> dict[str, Any]:
        return {
            "token": self.token,
            "session_id": self.session.id,
            "expires_at": self.session.expires_at,
            "app": {"id": self.app.id, "name": self.app.name},
            "user": public_user(self.user),
        }


def public_user(info: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in info.items() if k != "role_ids"}


@dataclass
class AuthService:
    db: Database
    provider: Provider
    code_ttl: int = 300

    async def check_access(self, app: App, user_id: int, hwid: str | None = None, *,
                           check_hwid: bool = True, fresh: bool = False) -> Access:
        ban = self.db.get_ban(app, user_id)
        if ban:
            return Access(False, reason="banned", extra=ban["reason"])
        info = await self.provider.get_member_info(app, user_id, fresh=fresh)
        if info is None:
            return Access(False, reason="not_member")
        if app.required_role_id and app.required_role_id not in info["role_ids"]:
            return Access(False, user=info, reason="missing_role")
        if check_hwid and app.hwid_lock:
            if not hwid:
                return Access(False, user=info, reason="hwid_required")
            bound = self.db.get_hwid(app.id, user_id)
            if bound and bound != hwid:
                return Access(False, user=info, reason="hwid_mismatch")
        return Access(True, user=info)

    def require_app(self, app_id: Any) -> App:
        app = self.db.get_app(app_id) if isinstance(app_id, str) else None
        if app is None:
            raise AuthError("app_not_found", 404)
        return app

    def _new_request(self, app: App, kind: str, *, user_id: int | None = None,
                     hwid: str | None = None, ip: str | None = None) -> LoginRequest:
        t = now()
        for _ in range(10):
            req = LoginRequest(
                id=secrets.token_urlsafe(24), app_id=app.id, code=generate_code(), kind=kind,
                status="pending", reason=None, user_id=user_id, hwid=hwid, ip=ip,
                created_at=t, expires_at=t + self.code_ttl,
            )
            try:
                self.db.create_request(req)
                return req
            except sqlite3.IntegrityError:
                continue
        raise RuntimeError("Nie udało się wygenerować unikalnego kodu")

    async def _issue_session(self, app: App, user_id: int, hwid: str | None, ip: str | None,
                             user: dict[str, Any]) -> IssuedSession:
        if app.hwid_lock and hwid:
            self.db.bind_hwid(app.id, user_id, hwid)
        token = secrets.token_urlsafe(32)
        t = now()
        session = Session(
            id=secrets.token_hex(6), token_hash=hash_token(token), app_id=app.id, user_id=user_id,
            hwid=hwid, ip=ip, created_at=t, expires_at=t + app.session_days * 86400, last_seen=t,
        )
        self.db.create_session(session)
        await self.provider.log_event("login", app, user_id, ip=ip, hwid=hwid, session_id=session.id)
        return IssuedSession(token, session, app, user)

    def start_device_login(self, app_id: Any, hwid: Any = None, ip: str | None = None) -> LoginRequest:
        app = self.require_app(app_id)
        hwid = clean_hwid(hwid)
        if app.hwid_lock and not hwid:
            raise AuthError("hwid_required")
        return self._new_request(app, "device", hwid=hwid, ip=ip)

    def find_device_request(self, code: str) -> tuple[LoginRequest, App]:
        req = self.db.get_request_by_code(normalize_code(code))
        if req is None or req.kind != "device":
            raise AuthError("invalid_code")
        if req.status != "pending":
            raise AuthError("already_used")
        if req.expired:
            raise AuthError("expired")
        app = self.require_app(req.app_id)
        return req, app

    async def approve_device_login(self, request_id: str, user_id: int) -> Access:
        req = self.db.get_request(request_id)
        if req is None:
            raise AuthError("request_not_found", 404)
        if req.expired:
            raise AuthError("expired")
        app = self.require_app(req.app_id)
        access = await self.check_access(app, user_id, req.hwid, fresh=True)
        if not access.ok:
            self.db.update_request(req.id, expect_status="pending", status="denied", reason=access.reason,
                                   user_id=user_id)
            await self.provider.log_event("denied", app, user_id, reason=access.reason, ip=req.ip)
            return access
        if not self.db.update_request(req.id, expect_status="pending", status="approved", user_id=user_id):
            raise AuthError("already_used")
        return access

    async def reject_device_login(self, request_id: str, user_id: int, reason: str = "denied_by_user") -> None:
        req = self.db.get_request(request_id)
        if req and self.db.update_request(req.id, expect_status="pending", status="denied",
                                          reason=reason, user_id=user_id):
            app = self.db.get_app(req.app_id)
            if app:
                await self.provider.log_event("denied", app, user_id, reason=reason, ip=req.ip)

    async def poll_device_login(self, request_id: Any) -> dict[str, Any]:
        req = self.db.get_request(request_id) if isinstance(request_id, str) else None
        if req is None or req.kind != "device":
            raise AuthError("request_not_found", 404)
        if req.status == "denied":
            return {"status": "denied", "reason": req.reason, "message": MESSAGES.get(req.reason or "", req.reason)}
        if req.status == "consumed":
            raise AuthError("already_used", 410)
        if req.expired:
            return {"status": "expired", "message": MESSAGES["expired"]}
        if req.status == "pending":
            return {"status": "pending", "expires_at": req.expires_at}
        if not self.db.update_request(req.id, expect_status="approved", status="consumed"):
            raise AuthError("already_used", 410)
        app = self.require_app(req.app_id)
        access = await self.check_access(app, req.user_id, req.hwid)
        if not access.ok:
            return {"status": "denied", "reason": access.reason, "message": access.message}
        issued = await self._issue_session(app, req.user_id, req.hwid, req.ip, access.user)
        return {"status": "approved", **issued.to_json()}

    async def create_user_code(self, app: App, user_id: int) -> tuple[LoginRequest | None, Access]:
        access = await self.check_access(app, user_id, check_hwid=False, fresh=True)
        if not access.ok:
            await self.provider.log_event("denied", app, user_id, reason=access.reason)
            return None, access
        return self._new_request(app, "code", user_id=user_id), access

    async def redeem_code(self, app_id: Any, code: Any, hwid: Any = None, ip: str | None = None) -> IssuedSession:
        app = self.require_app(app_id)
        hwid = clean_hwid(hwid)
        req = self.db.get_request_by_code(normalize_code(code if isinstance(code, str) else ""))
        if req is None or req.kind != "code" or req.app_id != app.id or req.status != "pending" or req.expired:
            raise AuthError("invalid_code", 401)
        if not self.db.update_request(req.id, expect_status="pending", status="consumed", hwid=hwid, ip=ip):
            raise AuthError("invalid_code", 401)
        access = await self.check_access(app, req.user_id, hwid)
        if not access.ok:
            await self.provider.log_event("denied", app, req.user_id, reason=access.reason, ip=ip, hwid=hwid)
            raise AuthError(access.reason, 403, access.message)
        return await self._issue_session(app, req.user_id, hwid, ip, access.user)

    async def validate_token(self, token: Any, hwid: Any = None) -> tuple[Session, App, dict[str, Any]]:
        if not isinstance(token, str) or not token:
            raise AuthError("invalid_token", 401)
        session = self.db.get_session_by_hash(hash_token(token))
        if session is None or session.expires_at < now():
            raise AuthError("invalid_token", 401)
        app = self.db.get_app(session.app_id)
        if app is None:
            raise AuthError("invalid_token", 401)
        if app.hwid_lock and session.hwid and clean_hwid(hwid) != session.hwid:
            raise AuthError("hwid_mismatch", 403)
        access = await self.check_access(app, session.user_id, check_hwid=False)
        if not access.ok:
            raise AuthError(access.reason, 403, access.message)
        self.db.touch_session(session.id)
        return session, app, access.user

    async def logout(self, token: Any) -> bool:
        if not isinstance(token, str) or not token:
            return False
        session = self.db.get_session_by_hash(hash_token(token))
        if session is None:
            return False
        self.db.delete_session(session.id)
        app = self.db.get_app(session.app_id)
        if app:
            await self.provider.log_event("logout", app, session.user_id, session_id=session.id)
        return True
