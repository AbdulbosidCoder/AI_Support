"""Channel-independent message and reply types."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Lang(str, Enum):
    UZ_LATN = "uz_latn"
    UZ_CYRL = "uz_cyrl"
    RU = "ru"
    EN = "en"


@dataclass
class Image:
    data: bytes
    media_type: str = "image/jpeg"


@dataclass
class Audio:
    data: bytes
    media_type: str = "audio/ogg"
    filename: str = "voice.ogg"


@dataclass
class IncomingMessage:
    """One client message, whatever channel it came from."""

    user_id: str
    text: str = ""
    images: list[Image] = field(default_factory=list)
    audio: Audio | None = None


@dataclass
class BotReply:
    text: str
    language: Lang
    escalate: bool = False
    escalation_reason: str = ""
    topic: str = ""
    screen_id: str | None = None
    # Text the client said (typed or transcribed), for the operator hand-off.
    client_text: str = ""
    guardrail_triggered: bool = False
