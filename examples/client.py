"""Przykładowa aplikacja logująca się przez bota Discord (tylko biblioteka standardowa).

Uruchom:  python examples/client.py
Ustaw API_URL i APP_ID (ID dostaniesz z /aplikacja utworz).
"""
import hashlib
import json
import os
import platform
import time
import urllib.error
import urllib.request
import uuid

API_URL = os.getenv("LOGIN_API", "http://localhost:8080")
APP_ID = os.getenv("LOGIN_APP_ID", "app_WPISZ_TUTAJ")
TOKEN_FILE = os.path.join(os.path.expanduser("~"), ".moja_aplikacja_token")


def get_hwid() -> str:
    """Stały identyfikator komputera (hash, żeby nie wysyłać surowych danych)."""
    raw = ""
    if platform.system() == "Windows":
        try:
            import winreg

            key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography",
                                 0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY)
            raw = winreg.QueryValueEx(key, "MachineGuid")[0]
        except OSError:
            pass
    if not raw:
        raw = str(uuid.getnode())
    return hashlib.sha256(f"{APP_ID}:{raw}".encode()).hexdigest()


def call(method: str, path: str, body: dict | None = None, token: str | None = None) -> tuple[int, dict]:
    headers = {"Content-Type": "application/json", "X-HWID": get_hwid()}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(API_URL + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as e:
        return e.code, json.load(e)


def login_with_discord_command() -> str | None:
    """Tryb 1: aplikacja pokazuje kod, użytkownik wpisuje /login KOD na Discordzie."""
    status, data = call("POST", "/api/auth/start", {"app_id": APP_ID, "hwid": get_hwid()})
    if status != 200:
        print("Błąd:", data.get("message"))
        return None
    print(f"\n  Wpisz na Discordzie:  /login {data['code']}\n  (masz {data['expires_in'] // 60} min)\n")
    while True:
        time.sleep(data.get("interval", 2))
        _, result = call("GET", f"/api/auth/poll/{data['request_id']}")
        if result.get("status") == "pending":
            continue
        if result.get("status") == "approved":
            return result["token"]
        print("Nie zalogowano:", result.get("message", result))
        return None


def login_with_code() -> str | None:
    """Tryb 2: użytkownik wpisuje /kod na Discordzie i przepisuje kod tutaj."""
    code = input("Wpisz kod z komendy /kod: ").strip()
    status, data = call("POST", "/api/auth/redeem", {"app_id": APP_ID, "code": code, "hwid": get_hwid()})
    if status != 200:
        print("Błąd:", data.get("message"))
        return None
    return data["token"]


def main() -> None:
    token = None
    if os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE) as f:
            token = f.read().strip()
    if token:
        status, data = call("GET", "/api/auth/me", token=token)
        if status != 200:
            print("Zapisana sesja nie działa:", data.get("message"))
            token = None

    if not token:
        choice = input("Logowanie przez Discord:\n 1) wpiszę /login na Discordzie\n 2) mam kod z /kod\n> ")
        token = login_with_code() if choice.strip() == "2" else login_with_discord_command()
        if not token:
            return
        with open(TOKEN_FILE, "w") as f:
            f.write(token)

    status, data = call("GET", "/api/auth/me", token=token)
    user = data["user"]
    print(f"\n✅ Zalogowano do {data['app']['name']} jako {user['display_name']} (@{user['username']})")
    print("Role:", ", ".join(r["name"] for r in user["roles"]) or "brak")
    # tu zaczyna się Twoja aplikacja – np. odblokuj funkcje zależnie od ról:
    # if any(r["name"] == "VIP" for r in user["roles"]): ...

    if input("\nWylogować? (t/n) ").strip().lower() == "t":
        call("POST", "/api/auth/logout", token=token)
        os.remove(TOKEN_FILE)
        print("Wylogowano.")


if __name__ == "__main__":
    main()
