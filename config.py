import os

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass


def _int(name: str, default: int | None = None) -> int | None:
    value = os.getenv(name, "").strip()
    return int(value) if value else default


def _bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name, "").strip().lower()
    if not value:
        return default
    return value in ("1", "true", "yes", "tak", "on")


DISCORD_TOKEN: str = os.getenv("DISCORD_TOKEN", "").strip()
GUILD_ID: int | None = _int("GUILD_ID")

API_HOST: str = os.getenv("API_HOST", "0.0.0.0")
API_PORT: int = _int("API_PORT", 8080)
PUBLIC_URL: str = os.getenv("PUBLIC_URL", "").strip()
TRUST_PROXY: bool = _bool("TRUST_PROXY", False)
CORS_ORIGINS: list[str] = [o.strip() for o in os.getenv("CORS_ORIGINS", "*").split(",") if o.strip()]

DATABASE_PATH: str = os.getenv("DATABASE_PATH", "data.db")

CODE_TTL: int = _int("CODE_TTL", 300)
MEMBER_CACHE_TTL: int = _int("MEMBER_CACHE_TTL", 60)
