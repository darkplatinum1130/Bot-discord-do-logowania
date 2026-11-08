from __future__ import annotations

import asyncio
import logging
import secrets
import time
from typing import Any

import discord
from aiohttp import web
from discord import app_commands
from discord.ext import tasks

import config
from api import create_api
from db import App, Database, now
from service import MESSAGES, AuthError, AuthService, format_code

log = logging.getLogger("bot")

COLOR_OK = discord.Color.green()
COLOR_ERR = discord.Color.red()
COLOR_INFO = discord.Color.blurple()


def short(value: str | None, length: int = 16) -> str:
    if not value:
        return "—"
    return value if len(value) <= length else value[:length] + "…"


def member_to_info(member: discord.Member) -> dict[str, Any]:
    return {
        "id": str(member.id),
        "username": member.name,
        "display_name": member.display_name,
        "avatar_url": member.display_avatar.url,
        "roles": [{"id": str(r.id), "name": r.name} for r in member.roles if not r.is_default()],
        "role_ids": {r.id for r in member.roles},
    }


class LoginBot(discord.Client):
    def __init__(self) -> None:
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)
        self.db = Database(config.DATABASE_PATH)
        self.service = AuthService(self.db, self, code_ttl=config.CODE_TTL)
        self.runner: web.AppRunner | None = None
        self._member_cache: dict[tuple[int, int], tuple[float, dict[str, Any] | None]] = {}
        self._background: set[asyncio.Task] = set()

    async def setup_hook(self) -> None:
        register_commands(self)
        self.tree.on_error = on_command_error

        if config.GUILD_ID:
            guild = discord.Object(config.GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            log.info("Zsynchronizowano %d komend na serwerze %s", len(synced), config.GUILD_ID)
        else:
            synced = await self.tree.sync()
            log.info("Zsynchronizowano %d komend globalnie (mogą pojawić się po chwili)", len(synced))

        api = create_api(self.service, cors_origins=config.CORS_ORIGINS, trust_proxy=config.TRUST_PROXY)
        self.runner = web.AppRunner(api, access_log=None)
        await self.runner.setup()
        await web.TCPSite(self.runner, config.API_HOST, config.API_PORT).start()
        log.info("API działa na http://%s:%d", config.API_HOST, config.API_PORT)

        self.cleanup_loop.start()

    async def on_ready(self) -> None:
        log.info("Zalogowano jako %s (%s) – serwery: %d", self.user, self.user.id, len(self.guilds))

    async def close(self) -> None:
        if self.runner:
            await self.runner.cleanup()
        await super().close()
        self.db.close()

    @tasks.loop(minutes=5)
    async def cleanup_loop(self) -> None:
        self.db.cleanup()
        cutoff = time.monotonic() - config.MEMBER_CACHE_TTL
        self._member_cache = {k: v for k, v in self._member_cache.items() if v[0] > cutoff}

    async def get_member_info(self, app: App, user_id: int, *, fresh: bool = False) -> dict[str, Any] | None:
        key = (app.guild_id, user_id)
        cached = self._member_cache.get(key)
        if cached and not fresh and time.monotonic() - cached[0] < config.MEMBER_CACHE_TTL:
            return cached[1]
        guild = self.get_guild(app.guild_id)
        if guild is None:
            return None
        try:
            info = member_to_info(await guild.fetch_member(user_id))
        except discord.NotFound:
            info = None
        except discord.HTTPException:
            if cached:
                return cached[1]
            raise
        self._member_cache[key] = (time.monotonic(), info)
        return info

    def forget_member(self, guild_id: int, user_id: int) -> None:
        self._member_cache.pop((guild_id, user_id), None)

    async def log_event(self, event: str, app: App, user_id: int | None, **details: Any) -> None:
        log.info("[%s] %s user=%s %s", app.name, event, user_id, details)
        titles = {
            "login": ("✅ Zalogowano", COLOR_OK),
            "denied": ("⛔ Odmowa logowania", COLOR_ERR),
            "logout": ("👋 Wylogowano", discord.Color.light_grey()),
        }
        title, color = titles.get(event, (event, COLOR_INFO))
        embed = discord.Embed(title=title, color=color, timestamp=discord.utils.utcnow())
        if user_id:
            embed.add_field(name="Użytkownik", value=f"<@{user_id}> (`{user_id}`)")
        embed.add_field(name="Aplikacja", value=app.name)
        if details.get("reason"):
            embed.add_field(name="Powód", value=MESSAGES.get(details["reason"], details["reason"]), inline=False)
        if details.get("ip"):
            embed.add_field(name="IP", value=f"`{details['ip']}`")
        if details.get("hwid"):
            embed.add_field(name="HWID", value=f"`{short(details['hwid'])}`")
        if details.get("session_id"):
            embed.add_field(name="Sesja", value=f"`{details['session_id']}`")
        self.post_log(app.guild_id, embed)

    def post_log(self, guild_id: int, embed: discord.Embed) -> None:
        channel_id = self.db.get_log_channel(guild_id)
        channel = self.get_channel(channel_id) if channel_id else None
        if channel is None:
            return

        async def send() -> None:
            try:
                await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
            except discord.HTTPException as e:
                log.warning("Nie udało się wysłać logu: %s", e)

        task = asyncio.create_task(send())
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    def log_admin(self, interaction: discord.Interaction, text: str) -> None:
        embed = discord.Embed(title="🛠️ Akcja administratora", description=text, color=COLOR_INFO,
                              timestamp=discord.utils.utcnow())
        embed.set_footer(text=f"{interaction.user} ({interaction.user.id})")
        self.post_log(interaction.guild_id, embed)


class ConfirmLoginView(discord.ui.View):
    def __init__(self, bot: LoginBot, request_id: str, app: App, user_id: int, expires_at: int):
        super().__init__(timeout=max(5, expires_at - now()))
        self.bot = bot
        self.request_id = request_id
        self.app = app
        self.user_id = user_id
        self.interaction: discord.Interaction | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.user_id

    async def _finish(self, interaction: discord.Interaction, embed: discord.Embed) -> None:
        self.stop()
        await interaction.response.edit_message(embed=embed, view=None)

    @discord.ui.button(label="Zatwierdź", style=discord.ButtonStyle.success, emoji="✅")
    async def approve(self, interaction: discord.Interaction, _: discord.ui.Button):
        try:
            access = await self.bot.service.approve_device_login(self.request_id, interaction.user.id)
        except AuthError as e:
            return await self._finish(interaction, error_embed(e.message))
        if not access.ok:
            return await self._finish(interaction, error_embed(access.message))
        await self._finish(interaction, discord.Embed(
            title="✅ Zatwierdzono",
            description=f"Wróć do aplikacji **{self.app.name}** – za chwilę zostaniesz zalogowany(-a).",
            color=COLOR_OK,
        ))

    @discord.ui.button(label="Odrzuć", style=discord.ButtonStyle.danger, emoji="✖️")
    async def deny(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self.bot.service.reject_device_login(self.request_id, interaction.user.id)
        await self._finish(interaction, discord.Embed(
            title="✖️ Odrzucono", description="Logowanie zostało anulowane.", color=COLOR_ERR))

    async def on_timeout(self) -> None:
        if self.interaction:
            try:
                await self.interaction.edit_original_response(embed=error_embed("Kod wygasł."), view=None)
            except discord.HTTPException:
                pass


class LogoutAllView(discord.ui.View):
    def __init__(self, bot: LoginBot, user_id: int):
        super().__init__(timeout=300)
        self.bot = bot
        self.user_id = user_id

    @discord.ui.button(label="Wyloguj ze wszystkich urządzeń", style=discord.ButtonStyle.danger, emoji="🚪")
    async def logout_all(self, interaction: discord.Interaction, _: discord.ui.Button):
        if interaction.user.id != self.user_id:
            return
        count = self.bot.db.delete_user_sessions(self.user_id)
        self.stop()
        await interaction.response.edit_message(
            embed=discord.Embed(title="🚪 Wylogowano", description=f"Zakończono sesje: **{count}**.", color=COLOR_OK),
            view=None,
        )


def error_embed(text: str) -> discord.Embed:
    return discord.Embed(description=f"❌ {text}", color=COLOR_ERR)


def ok_embed(text: str, title: str | None = None) -> discord.Embed:
    return discord.Embed(title=title, description=text, color=COLOR_OK)


def app_embed(app: App) -> discord.Embed:
    role = f"<@&{app.required_role_id}>" if app.required_role_id else "brak (każdy z serwera)"
    embed = discord.Embed(title=f"📦 {app.name}", color=COLOR_INFO)
    embed.add_field(name="App ID", value=f"```{app.id}```", inline=False)
    embed.add_field(name="Wymagana rola", value=role)
    embed.add_field(name="Blokada HWID", value="✅ tak" if app.hwid_lock else "❌ nie")
    embed.add_field(name="Długość sesji", value=f"{app.session_days} dni")
    embed.add_field(name="Utworzona", value=f"<t:{app.created_at}:R>")
    if config.PUBLIC_URL:
        embed.add_field(name="Adres API", value=config.PUBLIC_URL, inline=False)
    return embed


async def send(interaction: discord.Interaction, **kwargs: Any) -> None:
    kwargs.setdefault("ephemeral", True)
    if interaction.response.is_done():
        await interaction.followup.send(**kwargs)
    else:
        await interaction.response.send_message(**kwargs)


async def on_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
    original = getattr(error, "original", error)
    if isinstance(original, AuthError):
        return await send(interaction, embed=error_embed(original.message))
    if isinstance(error, app_commands.CommandOnCooldown):
        return await send(interaction, embed=error_embed(f"Zwolnij! Spróbuj za {error.retry_after:.0f} s."))
    if isinstance(error, app_commands.MissingPermissions):
        return await send(interaction, embed=error_embed("Nie masz uprawnień do tej komendy."))
    log.exception("Błąd komendy", exc_info=original)
    await send(interaction, embed=error_embed("Wystąpił nieoczekiwany błąd."))


def register_commands(bot: LoginBot) -> None:
    tree = bot.tree

    def resolve_app(interaction: discord.Interaction, value: str) -> App:
        if interaction.guild_id:
            app = bot.db.find_app(interaction.guild_id, value)
        else:
            app = bot.db.get_app(value) or next(
                (a for a in bot.db.list_apps() if a.name.lower() == value.lower()), None)
        if app is None:
            raise AuthError("app_not_found")
        return app

    async def app_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        apps = bot.db.list_apps(interaction.guild_id) if interaction.guild_id else bot.db.list_apps()
        current = current.lower()
        return [app_commands.Choice(name=a.name, value=a.id)
                for a in apps if current in a.name.lower() or current in a.id.lower()][:25]

    @tree.command(name="login", description="Zatwierdź logowanie w aplikacji kodem, który ona wyświetliła")
    @app_commands.describe(kod="Kod z aplikacji, np. ABCD-EFGH")
    @app_commands.checks.cooldown(5, 60)
    async def login_cmd(interaction: discord.Interaction, kod: str):
        req, app = bot.service.find_device_request(kod)
        access = await bot.service.check_access(app, interaction.user.id, req.hwid, fresh=True)
        if not access.ok:
            await bot.service.reject_device_login(req.id, interaction.user.id, access.reason)
            return await send(interaction, embed=error_embed(access.message))

        embed = discord.Embed(
            title="🔐 Prośba o logowanie",
            description=(f"Aplikacja **{app.name}** chce zalogować się na Twoje konto.\n\n"
                         "⚠️ Zatwierdź **tylko**, jeśli to Ty przed chwilą logujesz się w tej aplikacji. "
                         "Nigdy nie wpisuj kodów, które ktoś Ci podesłał."),
            color=COLOR_INFO,
        )
        embed.add_field(name="Kod", value=f"`{format_code(req.code)}`")
        embed.add_field(name="IP", value=f"`{req.ip or '—'}`")
        embed.add_field(name="Urządzenie (HWID)", value=f"`{short(req.hwid)}`")
        embed.add_field(name="Wygasa", value=f"<t:{req.expires_at}:R>")
        view = ConfirmLoginView(bot, req.id, app, interaction.user.id, req.expires_at)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
        view.interaction = interaction

    @tree.command(name="kod", description="Wygeneruj jednorazowy kod do wpisania w aplikacji")
    @app_commands.describe(aplikacja="Do której aplikacji chcesz się zalogować")
    @app_commands.autocomplete(aplikacja=app_autocomplete)
    @app_commands.checks.cooldown(5, 60)
    async def code_cmd(interaction: discord.Interaction, aplikacja: str):
        app = resolve_app(interaction, aplikacja)
        req, access = await bot.service.create_user_code(app, interaction.user.id)
        if req is None:
            return await send(interaction, embed=error_embed(access.message))
        embed = discord.Embed(
            title=f"🔑 Kod do {app.name}",
            description=(f"# `{format_code(req.code)}`\n"
                         f"Wpisz go w aplikacji. Ważny do <t:{req.expires_at}:T> (<t:{req.expires_at}:R>), "
                         "działa tylko raz.\n\n⚠️ **Nie podawaj go nikomu** – kto ma ten kod, zaloguje się "
                         "na Twoje konto."),
            color=COLOR_INFO,
        )
        await send(interaction, embed=embed)

    @tree.command(name="sesje", description="Pokaż, gdzie jesteś zalogowany(-a), i wyloguj się")
    async def sessions_cmd(interaction: discord.Interaction):
        sessions = bot.db.list_sessions(interaction.user.id)
        if not sessions:
            return await send(interaction, embed=ok_embed("Nie masz aktywnych sesji.", "🗂️ Twoje sesje"))
        lines = [f"**{app.name}** · od <t:{s.created_at}:R> · IP `{s.ip or '—'}` · ID `{s.id}`"
                 for s, app in sessions[:20]]
        embed = discord.Embed(title="🗂️ Twoje sesje", description="\n".join(lines), color=COLOR_INFO)
        await send(interaction, embed=embed, view=LogoutAllView(bot, interaction.user.id))

    apps_group = app_commands.Group(
        name="aplikacja", description="Zarządzanie aplikacjami logującymi się przez bota",
        guild_only=True, default_permissions=discord.Permissions(manage_guild=True),
    )

    @apps_group.command(name="utworz", description="Dodaj nową aplikację")
    @app_commands.describe(nazwa="Nazwa widoczna dla użytkowników", rola="Rola wymagana do zalogowania (opcjonalnie)",
                           hwid="Przypisz konto do jednego urządzenia", dni_sesji="Ile dni ważne jest logowanie")
    async def app_create(interaction: discord.Interaction, nazwa: app_commands.Range[str, 1, 64],
                         rola: discord.Role | None = None, hwid: bool = False,
                         dni_sesji: app_commands.Range[int, 1, 365] = 7):
        if bot.db.find_app(interaction.guild_id, nazwa):
            return await send(interaction, embed=error_embed("Aplikacja o tej nazwie już istnieje."))
        app = App(id="app_" + secrets.token_hex(6), guild_id=interaction.guild_id, name=nazwa,
                  required_role_id=rola.id if rola else None, hwid_lock=hwid, session_days=dni_sesji,
                  created_by=interaction.user.id, created_at=now())
        bot.db.create_app(app)
        bot.log_admin(interaction, f"Utworzono aplikację **{app.name}** (`{app.id}`)")
        embed = app_embed(app)
        embed.title = f"✅ Utworzono: {app.name}"
        embed.set_footer(text="Wpisz App ID w swojej aplikacji (patrz README).")
        await send(interaction, embed=embed)

    @apps_group.command(name="lista", description="Lista aplikacji na tym serwerze")
    async def app_list(interaction: discord.Interaction):
        apps = bot.db.list_apps(interaction.guild_id)
        if not apps:
            empty = ok_embed("Brak aplikacji. Dodaj: `/aplikacja utworz`.", "📦 Aplikacje")
            return await send(interaction, embed=empty)
        lines = [f"**{a.name}** · `{a.id}`" + (f" · <@&{a.required_role_id}>" if a.required_role_id else "")
                 + (" · 🔒 HWID" if a.hwid_lock else "") for a in apps]
        embed = discord.Embed(title="📦 Aplikacje", description="\n".join(lines), color=COLOR_INFO)
        await send(interaction, embed=embed)

    @apps_group.command(name="info", description="Szczegóły aplikacji")
    @app_commands.autocomplete(aplikacja=app_autocomplete)
    async def app_info(interaction: discord.Interaction, aplikacja: str):
        await send(interaction, embed=app_embed(resolve_app(interaction, aplikacja)))

    @apps_group.command(name="edytuj", description="Zmień ustawienia aplikacji")
    @app_commands.autocomplete(aplikacja=app_autocomplete)
    @app_commands.describe(usun_role="Usuń wymóg roli")
    async def app_edit(interaction: discord.Interaction, aplikacja: str,
                       nazwa: app_commands.Range[str, 1, 64] | None = None, rola: discord.Role | None = None,
                       usun_role: bool = False, hwid: bool | None = None,
                       dni_sesji: app_commands.Range[int, 1, 365] | None = None):
        app = resolve_app(interaction, aplikacja)
        changes: dict[str, Any] = {}
        if nazwa and nazwa != app.name:
            other = bot.db.find_app(interaction.guild_id, nazwa)
            if other and other.id != app.id:
                return await send(interaction, embed=error_embed("Aplikacja o tej nazwie już istnieje."))
            changes["name"] = nazwa
        if rola:
            changes["required_role_id"] = rola.id
        elif usun_role:
            changes["required_role_id"] = None
        if hwid is not None:
            changes["hwid_lock"] = hwid
        if dni_sesji is not None:
            changes["session_days"] = dni_sesji
        if not changes:
            return await send(interaction, embed=error_embed("Nie podano żadnych zmian."))
        bot.db.update_app(app.id, **changes)
        app = bot.db.get_app(app.id)
        bot.log_admin(interaction, f"Zmieniono ustawienia **{app.name}**: {', '.join(changes)}")
        embed = app_embed(app)
        embed.title = f"✏️ Zapisano: {app.name}"
        await send(interaction, embed=embed)

    @apps_group.command(name="usun", description="Usuń aplikację (wylogowuje wszystkich jej użytkowników)")
    @app_commands.autocomplete(aplikacja=app_autocomplete)
    @app_commands.describe(potwierdz="Ustaw na True, aby potwierdzić")
    async def app_delete(interaction: discord.Interaction, aplikacja: str, potwierdz: bool = False):
        app = resolve_app(interaction, aplikacja)
        if not potwierdz:
            return await send(interaction, embed=error_embed(
                f"To usunie **{app.name}** i wszystkie jej sesje. Powtórz komendę z `potwierdz: True`."))
        bot.db.delete_app(app.id)
        bot.log_admin(interaction, f"Usunięto aplikację **{app.name}** (`{app.id}`)")
        await send(interaction, embed=ok_embed(f"Usunięto **{app.name}**."))

    admin_group = app_commands.Group(
        name="admin", description="Zarządzanie użytkownikami i logami",
        guild_only=True, default_permissions=discord.Permissions(manage_guild=True),
    )

    def app_or_all(interaction: discord.Interaction, aplikacja: str | None) -> tuple[str, str]:
        if not aplikacja:
            return "*", "wszystkich aplikacji"
        app = resolve_app(interaction, aplikacja)
        return app.id, f"**{app.name}**"

    @admin_group.command(name="ban", description="Zablokuj użytkownikowi logowanie")
    @app_commands.autocomplete(aplikacja=app_autocomplete)
    @app_commands.describe(aplikacja="Puste = wszystkie aplikacje serwera")
    async def admin_ban(interaction: discord.Interaction, uzytkownik: discord.User, aplikacja: str | None = None,
                        powod: app_commands.Range[str, 1, 200] | None = None):
        app_id, label = app_or_all(interaction, aplikacja)
        bot.db.ban(interaction.guild_id, app_id, uzytkownik.id, powod, interaction.user.id)
        removed = bot.db.delete_user_sessions(uzytkownik.id, app_id=None if app_id == "*" else app_id,
                                              guild_id=interaction.guild_id)
        bot.log_admin(interaction, f"Ban dla {uzytkownik.mention} na {label}. Powód: {powod or '—'}")
        await send(interaction, embed=ok_embed(
            f"Zablokowano {uzytkownik.mention} na {label}. Zakończone sesje: **{removed}**."))

    @admin_group.command(name="unban", description="Odblokuj użytkownika")
    @app_commands.autocomplete(aplikacja=app_autocomplete)
    @app_commands.describe(aplikacja="Puste = ban na wszystkie aplikacje")
    async def admin_unban(interaction: discord.Interaction, uzytkownik: discord.User, aplikacja: str | None = None):
        app_id, label = app_or_all(interaction, aplikacja)
        if not bot.db.unban(interaction.guild_id, app_id, uzytkownik.id):
            return await send(interaction, embed=error_embed(f"{uzytkownik.mention} nie ma bana na {label}."))
        bot.log_admin(interaction, f"Unban dla {uzytkownik.mention} na {label}")
        await send(interaction, embed=ok_embed(f"Odblokowano {uzytkownik.mention} na {label}."))

    @admin_group.command(name="reset-hwid", description="Pozwól użytkownikowi zalogować się z nowego urządzenia")
    @app_commands.autocomplete(aplikacja=app_autocomplete)
    async def admin_reset_hwid(interaction: discord.Interaction, uzytkownik: discord.User, aplikacja: str):
        app = resolve_app(interaction, aplikacja)
        if not bot.db.reset_hwid(app.id, uzytkownik.id):
            return await send(interaction, embed=error_embed(f"{uzytkownik.mention} nie ma przypisanego HWID."))
        bot.log_admin(interaction, f"Reset HWID dla {uzytkownik.mention} w **{app.name}**")
        await send(interaction, embed=ok_embed(f"Zresetowano HWID {uzytkownik.mention} w **{app.name}**."))

    @admin_group.command(name="sesje", description="Pokaż sesje użytkownika")
    async def admin_sessions(interaction: discord.Interaction, uzytkownik: discord.User):
        sessions = bot.db.list_sessions(uzytkownik.id, interaction.guild_id)
        if not sessions:
            return await send(interaction, embed=ok_embed(f"{uzytkownik.mention} nie ma aktywnych sesji."))
        lines = [f"**{app.name}** · od <t:{s.created_at}:R> · ostatnio <t:{s.last_seen or s.created_at}:R>"
                 f" · IP `{s.ip or '—'}` · HWID `{short(s.hwid)}`" for s, app in sessions[:20]]
        await send(interaction, embed=discord.Embed(
            title=f"🗂️ Sesje: {uzytkownik}", description="\n".join(lines), color=COLOR_INFO))

    @admin_group.command(name="wyloguj", description="Zakończ sesje użytkownika")
    @app_commands.autocomplete(aplikacja=app_autocomplete)
    @app_commands.describe(aplikacja="Puste = wszystkie aplikacje serwera")
    async def admin_logout(interaction: discord.Interaction, uzytkownik: discord.User, aplikacja: str | None = None):
        app_id, label = app_or_all(interaction, aplikacja)
        removed = bot.db.delete_user_sessions(uzytkownik.id, app_id=None if app_id == "*" else app_id,
                                              guild_id=interaction.guild_id)
        bot.log_admin(interaction, f"Wylogowano {uzytkownik.mention} z {label} ({removed} sesji)")
        await send(interaction, embed=ok_embed(f"Wylogowano {uzytkownik.mention} z {label}. Sesje: **{removed}**."))

    @admin_group.command(name="kanal-logow", description="Ustaw kanał, na który bot wysyła logi logowań")
    @app_commands.describe(kanal="Puste = wyłącz logi")
    async def admin_log_channel(interaction: discord.Interaction, kanal: discord.TextChannel | None = None):
        bot.db.set_log_channel(interaction.guild_id, kanal.id if kanal else None)
        if kanal:
            bot.log_admin(interaction, f"Ustawiono kanał logów na {kanal.mention}")
            await send(interaction, embed=ok_embed(f"Logi będą wysyłane na {kanal.mention}."))
        else:
            await send(interaction, embed=ok_embed("Wyłączono logi."))

    tree.add_command(apps_group)
    tree.add_command(admin_group)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not config.DISCORD_TOKEN:
        raise SystemExit("Brak DISCORD_TOKEN – skopiuj .env.example do .env i uzupełnij token bota.")
    LoginBot().run(config.DISCORD_TOKEN, log_handler=None)


if __name__ == "__main__":
    main()
