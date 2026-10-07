"""Settings from environment variables."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Settings:
    telegram_token: str = ""
    # Telegram chat (group or user) where escalations are forwarded to human support.
    support_chat_id: int | None = None
    claude_model: str = "claude-opus-5-5"
    claude_effort: str = "medium"
    stt_api_url: str = "https://api.openai.com/v1/audio/transcriptions"
    stt_api_key: str = ""
    stt_model: str = "whisper-1"
    kb_dir: Path = ROOT / "data" / "knowledge_base"
    history_turns: int = 6
    # SQLite file with registered clients and their chosen language.
    db_path: Path = ROOT / "data" / "db" / "bot.sqlite3"
    # Admin bot and its web panel (Telegram mini app), served through a Cloudflare Tunnel.
    admin_bot_token: str = ""
    # Telegram user ids allowed into the admin bot and panel.
    admin_ids: frozenset[int] = frozenset()
    # Public hostname the tunnel routes to the panel, e.g. admin.example.com.
    admin_domain: str = ""
    admin_port: int = 8080

    @classmethod
    def from_env(cls) -> "Settings":
        chat = os.getenv("SUPPORT_CHAT_ID", "").strip()
        return cls(
            telegram_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
            support_chat_id=int(chat) if chat else None,
            claude_model=os.getenv("CLAUDE_MODEL", cls.claude_model),
            claude_effort=os.getenv("CLAUDE_EFFORT", cls.claude_effort),
            stt_api_url=os.getenv("STT_API_URL", cls.stt_api_url),
            stt_api_key=os.getenv("STT_API_KEY", ""),
            stt_model=os.getenv("STT_MODEL", cls.stt_model),
            kb_dir=Path(os.getenv("KB_DIR", str(cls.kb_dir))),
            history_turns=int(os.getenv("HISTORY_TURNS", cls.history_turns)),
            db_path=Path(os.getenv("DB_PATH", str(cls.db_path))),
            admin_bot_token=os.getenv("ADMIN_BOT_TOKEN", ""),
            admin_ids=parse_ids(os.getenv("ADMIN_IDS", "")),
            admin_domain=os.getenv("ADMIN_DOMAIN", "").strip(),
            admin_port=int(os.getenv("ADMIN_PORT", cls.admin_port)),
        )

    @property
    def admin_url(self) -> str:
        """HTTPS address of the admin panel; Telegram opens mini apps only over HTTPS."""
        domain = self.admin_domain.removeprefix("https://").removeprefix("http://").strip("/")
        return f"https://{domain}/" if domain else ""


def parse_ids(value: str) -> frozenset[int]:
    """'123, 456' -> {123, 456}; anything that is not a number is ignored."""
    return frozenset(int(p) for p in value.replace(";", ",").split(",") if p.strip().lstrip("-").isdigit())
