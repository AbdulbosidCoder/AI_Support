"""Wire the engine from settings."""
from __future__ import annotations

from .config import Settings
from .engine import ConversationStore, SupportEngine
from .knowledge import KnowledgeBase
from .llm import ClaudeSupportLLM
from .prompt import build_system_prompt, reply_schema
from .stt import DisabledSTT, SpeechToText, WhisperHTTPSTT


def build_engine(settings: Settings) -> SupportEngine:
    kb = KnowledgeBase.load(settings.kb_dir)
    llm = ClaudeSupportLLM(build_system_prompt(kb), reply_schema(kb), settings.claude_model, settings.claude_effort)
    stt: SpeechToText = (
        WhisperHTTPSTT(settings.stt_api_url, settings.stt_api_key, settings.stt_model)
        if settings.stt_api_key else DisabledSTT()
    )
    return SupportEngine(llm, stt, ConversationStore(settings.history_turns))
