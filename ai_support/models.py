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
class VideoAttachment:
    """An instruction video to send after the answer; the channel picks the source it can use."""

    id: str
    title: str
    path: str | None = None
    url: str = ""
    file_ids: dict[str, str] = field(default_factory=dict)


@dataclass
class IncomingMessage:
    """One client message, whatever channel it came from."""

    user_id: str
    text: str = ""
    images: list[Image] = field(default_factory=list)
    audio: Audio | None = None
    # Language the client chose in the channel, if known (a hint for voice recognition).
    language: Lang | None = None


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
    # Offer the quick-question menu with this reply (greeting or a message without a problem).
    show_menu: bool = False
    # Instruction videos for the answer's topic (see ai_support/videos.py), sent after the text.
    videos: list[VideoAttachment] = field(default_factory=list)
    # For the support team's log only, never shown to the client (e.g. why a voice message was not recognised).
    note: str = ""
