import asyncio
import os
import tempfile

from aiohttp.test_utils import TestClient, TestServer

from api import create_api
from db import App, Database, now
from service import AuthService

GUILD = 1
VIP_ROLE = 500
ALICE, BOB, STRANGER = 10, 20, 30


class FakeDiscord:
    def __init__(self):
        self.members = {ALICE: {VIP_ROLE}, BOB: set()}
        self.events = []

    async def get_member_info(self, app, user_id, *, fresh=False):
        if user_id not in self.members:
            return None
        roles = self.members[user_id]
        return {"id": str(user_id), "username": f"user{user_id}", "display_name": f"User {user_id}",
                "avatar_url": "", "roles": [{"id": str(r), "name": "VIP"} for r in roles], "role_ids": roles}

    async def log_event(self, event, app, user_id, **details):
        self.events.append((event, user_id, details.get("reason")))


def make_app(app_id, **kw):
    defaults = {"guild_id": GUILD, "name": app_id, "required_role_id": None, "hwid_lock": False,
                "session_days": 7, "created_by": 1, "created_at": now()}
    defaults.update(kw)
    return App(id=app_id, **defaults)


async def main():
    tmp = tempfile.mkdtemp()
    db = Database(os.path.join(tmp, "test.db"))
    fake = FakeDiscord()
    service = AuthService(db, fake, code_ttl=300)
    db.create_app(make_app("app_open"))
    db.create_app(make_app("app_vip", required_role_id=VIP_ROLE))
    db.create_app(make_app("app_hwid", hwid_lock=True))

    client = TestClient(TestServer(create_api(service, cors_origins=["*"])))
    await client.start_server()
    ok = 0

    def check(cond, label):
        nonlocal ok
        assert cond, label
        ok += 1
        print("  ✔", label)

    async def me(token, hwid=None):
        headers = {"Authorization": f"Bearer {token}"}
        if hwid:
            headers["X-HWID"] = hwid
        r = await client.get("/api/auth/me", headers=headers)
        return r.status, await r.json()

    print("Tryb 1: kod z aplikacji -> /login")
    r = await client.post("/api/auth/start", json={"app_id": "app_open"})
    start = await r.json()
    check(r.status == 200 and len(start["code"]) == 9, "start zwraca kod XXXX-XXXX")
    r = await client.get(f"/api/auth/poll/{start['request_id']}")
    check((await r.json())["status"] == "pending", "poll: pending przed zatwierdzeniem")
    req, _ = service.find_device_request(start["code"].lower())
    access = await service.approve_device_login(req.id, BOB)
    check(access.ok, "BOB zatwierdza na Discordzie")
    r = await client.get(f"/api/auth/poll/{start['request_id']}")
    data = await r.json()
    check(data["status"] == "approved" and data["token"] and data["user"]["id"] == str(BOB),
          "poll: approved + token + dane użytkownika")
    check("role_ids" not in data["user"], "wewnętrzne pole role_ids nie wycieka")
    token = data["token"]
    r = await client.get(f"/api/auth/poll/{start['request_id']}")
    check(r.status == 410, "token można odebrać tylko raz")
    status, body = await me(token)
    check(status == 200 and body["valid"] and body["app"]["id"] == "app_open", "/me: token ważny")
    r = await client.post("/api/auth/logout", headers={"Authorization": f"Bearer {token}"})
    check((await r.json())["ok"], "logout")
    status, body = await me(token)
    check(status == 401 and body["error"] == "invalid_token", "/me po wylogowaniu: 401")

    print("Odrzucenie i brak roli")
    r = await client.post("/api/auth/start", json={"app_id": "app_open"})
    start = await r.json()
    req, _ = service.find_device_request(start["code"])
    await service.reject_device_login(req.id, BOB)
    data = await (await client.get(f"/api/auth/poll/{start['request_id']}")).json()
    check(data["status"] == "denied" and data["reason"] == "denied_by_user", "odrzucone przez użytkownika")
    r = await client.post("/api/auth/start", json={"app_id": "app_vip"})
    start = await r.json()
    req, _ = service.find_device_request(start["code"])
    access = await service.approve_device_login(req.id, BOB)
    check(not access.ok and access.reason == "missing_role", "BOB bez roli VIP nie może zatwierdzić")
    data = await (await client.get(f"/api/auth/poll/{start['request_id']}")).json()
    check(data["status"] == "denied" and data["reason"] == "missing_role", "aplikacja dostaje powód missing_role")
    r = await client.post("/api/auth/start", json={"app_id": "app_vip"})
    req, _ = service.find_device_request((await r.json())["code"])
    access = await service.approve_device_login(req.id, STRANGER)
    check(access.reason == "not_member", "osoba spoza serwera: not_member")

    print("Tryb 2: /kod na Discordzie -> aplikacja")
    req, access = await service.create_user_code(db.get_app("app_vip"), ALICE)
    check(req is not None, "ALICE dostaje kod")
    r = await client.post("/api/auth/redeem", json={"app_id": "app_open", "code": req.code})
    check(r.status == 401, "kod nie działa w innej aplikacji")
    r = await client.post("/api/auth/redeem", json={"app_id": "app_vip", "code": req.code})
    data = await r.json()
    check(r.status == 200 and data["user"]["id"] == str(ALICE), "redeem zwraca token ALICE")
    alice_token = data["token"]
    r = await client.post("/api/auth/redeem", json={"app_id": "app_vip", "code": req.code})
    check(r.status == 401, "kodu nie da się użyć drugi raz")
    fake.members[ALICE] = set()
    status, body = await me(alice_token)
    check(status == 403 and body["error"] == "missing_role", "utrata roli = token przestaje działać")
    fake.members[ALICE] = {VIP_ROLE}
    status, _ = await me(alice_token)
    check(status == 200, "po odzyskaniu roli token znów działa")
    db.ban(GUILD, "*", ALICE, "cheaty", 1)
    status, body = await me(alice_token)
    check(status == 403 and body["error"] == "banned", "ban blokuje istniejący token")
    db.unban(GUILD, "*", ALICE)

    print("Blokada HWID")
    r = await client.post("/api/auth/start", json={"app_id": "app_hwid"})
    check(r.status == 400 and (await r.json())["error"] == "hwid_required", "bez HWID: hwid_required")
    req, _ = await service.create_user_code(db.get_app("app_hwid"), BOB)
    r = await client.post("/api/auth/redeem", json={"app_id": "app_hwid", "code": req.code, "hwid": "PC-1"})
    hwid_token = (await r.json())["token"]
    check(r.status == 200 and db.get_hwid("app_hwid", BOB) == "PC-1", "pierwsze logowanie przypisuje HWID")
    status, body = await me(hwid_token, "PC-2")
    check(status == 403 and body["error"] == "hwid_mismatch", "/me z innym HWID: 403")
    status, _ = await me(hwid_token, "PC-1")
    check(status == 200, "/me z właściwym HWID: 200")
    req, _ = await service.create_user_code(db.get_app("app_hwid"), BOB)
    r = await client.post("/api/auth/redeem", json={"app_id": "app_hwid", "code": req.code, "hwid": "PC-2"})
    check(r.status == 403 and (await r.json())["error"] == "hwid_mismatch", "logowanie z innego PC zablokowane")
    db.reset_hwid("app_hwid", BOB)
    req, _ = await service.create_user_code(db.get_app("app_hwid"), BOB)
    r = await client.post("/api/auth/redeem", json={"app_id": "app_hwid", "code": req.code, "hwid": "PC-2"})
    check(r.status == 200, "po resecie HWID można zalogować się z nowego PC")

    print("Wygasanie, błędne dane, limity, CORS")
    r = await client.post("/api/auth/start", json={"app_id": "app_open"})
    start = await r.json()
    db.conn.execute("UPDATE login_requests SET expires_at = ? WHERE id = ?", (now() - 1, start["request_id"]))
    data = await (await client.get(f"/api/auth/poll/{start['request_id']}")).json()
    check(data["status"] == "expired", "wygasły kod: expired")
    r = await client.post("/api/auth/start", json={"app_id": "nie_ma"})
    check(r.status == 404, "nieznana aplikacja: 404")
    r = await client.post("/api/auth/start", data="to nie json")
    check(r.status == 400, "zły JSON: 400")
    r = await client.get("/api/apps/app_hwid")
    check((await r.json())["hwid_required"] is True, "/api/apps/{id} pokazuje wymóg HWID")
    r = await client.options("/api/auth/me", headers={"Origin": "https://x.pl"})
    check(r.status == 204 and r.headers.get("Access-Control-Allow-Origin") == "*", "CORS preflight")
    statuses = [(await client.post("/api/auth/redeem", json={"app_id": "app_open", "code": "AAAA-AAAA"})).status
                for _ in range(12)]
    check(429 in statuses, "limit prób wpisywania kodu (ochrona przed zgadywaniem)")

    await client.close()
    db.close()
    print(f"\nWszystkie testy OK ({ok}). Zdarzenia w logach: {len(fake.events)}")


if __name__ == "__main__":
    asyncio.run(main())
