# Discord Login Bot

Uniwersalny bot Discord, przez którego użytkownicy logują się do **dowolnej aplikacji** – launchera, gry, programu, strony.
Bot ma wbudowane HTTP API, więc aplikacja może być napisana w czymkolwiek (Python, C#, C++, JS, Java…).

- wiele aplikacji na jednym bocie (każda ma swoje `App ID`)
- dostęp tylko dla członków serwera, opcjonalnie tylko z daną **rolą** (np. VIP / Premium)
- opcjonalna **blokada HWID** (konto przypisane do jednego komputera)
- **bany**, reset HWID, podgląd i kończenie sesji
- utrata roli / ban / wyjście z serwera = token od razu przestaje działać
- **kanał logów** – kto, kiedy, skąd się zalogował
- nie wymaga uprzywilejowanych intentów

## Jak działa logowanie

Są dwa tryby – aplikacja może obsługiwać jeden albo oba.

**Tryb 1 – kod z aplikacji (`/login`)**
1. Aplikacja wywołuje `POST /api/auth/start` i pokazuje kod, np. `ABCD-EFGH`.
2. Użytkownik wpisuje na Discordzie `/login ABCD-EFGH` i klika **Zatwierdź**.
3. Aplikacja co 2 s pyta `GET /api/auth/poll/{request_id}` i dostaje token.

**Tryb 2 – kod z Discorda (`/kod`)**
1. Użytkownik wpisuje na Discordzie `/kod aplikacja:Launcher` i dostaje jednorazowy kod.
2. Wpisuje ten kod w aplikacji, która wywołuje `POST /api/auth/redeem` i dostaje token.

Potem aplikacja zapisuje token i przy każdym starcie sprawdza go przez `GET /api/auth/me`.

## Uruchomienie

1. Wejdź na <https://discord.com/developers/applications> → **New Application** → zakładka **Bot** → **Reset Token** i skopiuj token.
2. Zaproś bota na serwer (podmień `CLIENT_ID` na Application ID z zakładki General Information):
   `https://discord.com/oauth2/authorize?client_id=CLIENT_ID&scope=bot+applications.commands&permissions=18432`
3. Skopiuj `.env.example` jako `.env` i wpisz `DISCORD_TOKEN` (oraz `GUILD_ID` swojego serwera, żeby komendy pojawiły się od razu).
4. Uruchom (potrzebny Python 3.10+):
   - Windows: dwuklik na `start.bat` (sam utworzy środowisko i zainstaluje paczki),
   - Linux / VPS: `chmod +x start.sh && ./start.sh`,
   - Docker: `docker compose up -d --build` (baza danych trafia do wolumenu `bot-data`),
   - ręcznie: `pip install -r requirements.txt`, potem `python bot.py`.
5. Na Discordzie: `/aplikacja utworz nazwa:MójLauncher` → dostaniesz **App ID**, który wpisujesz w swojej aplikacji.
6. Opcjonalnie: `/admin kanal-logow kanal:#logi`.

Bot i API działają w jednym procesie. API domyślnie słucha na porcie `8080` – żeby aplikacje u innych ludzi mogły się połączyć,
postaw bota na VPS-ie albo wystaw port (np. Cloudflare Tunnel / nginx z HTTPS, wtedy ustaw `TRUST_PROXY=true`).

## Komendy

| Komenda | Kto | Co robi |
|---|---|---|
| `/login kod` | każdy | zatwierdza logowanie kodem z aplikacji |
| `/kod aplikacja` | każdy | daje jednorazowy kod do wpisania w aplikacji |
| `/sesje` | każdy | pokazuje, gdzie jesteś zalogowany(-a), przycisk „wyloguj wszędzie” |
| `/aplikacja utworz nazwa [rola] [hwid] [dni_sesji]` | admin | dodaje aplikację |
| `/aplikacja lista` / `info` / `edytuj` / `usun` | admin | zarządzanie aplikacjami |
| `/admin ban uzytkownik [aplikacja] [powod]` | admin | blokuje logowanie (bez aplikacji = wszystkie) |
| `/admin unban uzytkownik [aplikacja]` | admin | odblokowuje |
| `/admin reset-hwid uzytkownik aplikacja` | admin | pozwala zalogować się z nowego komputera |
| `/admin sesje uzytkownik` | admin | sesje użytkownika (IP, HWID, ostatnia aktywność) |
| `/admin wyloguj uzytkownik [aplikacja]` | admin | kończy sesje użytkownika |
| `/admin kanal-logow [kanal]` | admin | kanał z logami logowań (puste = wyłącz) |

„Admin” = uprawnienie **Zarządzanie serwerem** (można to zmienić w Ustawienia serwera → Integracje).

## API

Wszystko w JSON. Błędy mają postać `{"error": "kod_bledu", "message": "komunikat po polsku"}` – `message` możesz od razu pokazać użytkownikowi.
HWID podajesz w polu `hwid` albo w nagłówku `X-HWID` (wymagany tylko, gdy aplikacja ma blokadę HWID).

| Metoda | Ścieżka | Opis |
|---|---|---|
| `POST` | `/api/auth/start` | `{"app_id", "hwid"?}` → `{"request_id", "code", "expires_in", "interval"}` |
| `GET` | `/api/auth/poll/{request_id}` | `{"status": "pending"}` → `{"status": "approved", "token", "user", ...}` albo `denied` / `expired` |
| `POST` | `/api/auth/redeem` | `{"app_id", "code", "hwid"?}` → `{"token", "expires_at", "user", "app"}` |
| `GET` | `/api/auth/me` | nagłówek `Authorization: Bearer TOKEN` → `{"valid": true, "user", "app", "expires_at"}` |
| `POST` | `/api/auth/logout` | nagłówek `Authorization: Bearer TOKEN` → `{"ok": true}` |
| `GET` | `/api/apps/{app_id}` | `{"id", "name", "hwid_required"}` |

Obiekt `user`:
```json
{
  "id": "123456789012345678",
  "username": "jan",
  "display_name": "Janek",
  "avatar_url": "https://cdn.discordapp.com/...",
  "roles": [{"id": "987654321", "name": "VIP"}]
}
```

Kody błędów: `app_not_found`, `not_member`, `missing_role`, `banned`, `hwid_required`, `hwid_mismatch`, `invalid_code`,
`expired`, `already_used`, `denied_by_user`, `invalid_token`, `rate_limited`.

### Szybki test z konsoli

```bash
curl -X POST http://localhost:8080/api/auth/start -H "Content-Type: application/json" -d "{\"app_id\": \"app_TWOJE_ID\"}"
```

### Przykłady

- [examples/client.py](examples/client.py) – gotowa aplikacja konsolowa w Pythonie (oba tryby, zapamiętuje token, HWID z rejestru).
- [examples/DiscordLogin.cs](examples/DiscordLogin.cs) – klasa do launchera w C# (.NET 6+).
- JavaScript (przeglądarka / Node 18+):

```js
const API = "http://localhost:8080", APP_ID = "app_TWOJE_ID";
const start = await (await fetch(`${API}/api/auth/start`, {
  method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({app_id: APP_ID}),
})).json();
console.log(`Wpisz na Discordzie: /login ${start.code}`);
let result;
do {
  await new Promise(r => setTimeout(r, start.interval * 1000));
  result = await (await fetch(`${API}/api/auth/poll/${start.request_id}`)).json();
} while (result.status === "pending");
console.log(result.status === "approved" ? `Cześć ${result.user.display_name}!` : result.message);
```

## Bezpieczeństwo – warto wiedzieć

- **App ID nie jest tajny** – siedzi w aplikacji. Ochronę dają serwer, rola, bany i HWID, a nie ukrycie ID.
- Jeśli masz własny backend (np. stronę), sprawdzaj token **po stronie serwera** przez `/api/auth/me` – nie ufaj temu, co mówi klient.
- HWID wysyła klient, więc da się go podrobić – to utrudnienie dzielenia się kontem, nie pełne zabezpieczenie.
- Wystawiając API do internetu używaj **HTTPS** (nginx / Caddy / Cloudflare), żeby tokeny nie leciały otwartym tekstem.
- Tokeny są przechowywane w bazie tylko jako hash SHA-256. Kody są jednorazowe, ważne 5 minut, a próby ich zgadywania są limitowane.
- Bot ostrzega przy `/login` i `/kod`, żeby nie wpisywać ani nie podawać kodów od/do innych osób (ochrona przed phishingiem).

## Pliki

| Plik | Zawartość |
|---|---|
| `bot.py` | bot Discord: komendy, przyciski, logi, start API |
| `api.py` | HTTP API (aiohttp), limity zapytań, CORS |
| `service.py` | logika logowania (kody, sesje, role, bany, HWID) |
| `db.py` | baza SQLite |
| `config.py` | ustawienia z `.env` |
| `tests/test_flow.py` | test całego przepływu bez Discorda: `python -m tests.test_flow` |
| `examples/` | przykładowi klienci (Python, C#) |
| `start.bat` / `start.sh` | szybki start na Windows / Linux |
| `Dockerfile`, `docker-compose.yml` | uruchomienie w Dockerze |

## Licencja

[MIT](LICENSE) – możesz używać, zmieniać i rozpowszechniać, także komercyjnie.
