"""Wire the engine from settings."""
from __future__ import annotations

from .config import Settings
from .engine import ConversationStore, SupportEngine
from .handoffs import HandoffStore
from .knowledge import KnowledgeBase
from .llm import ClaudeSupportLLM
from .prompt import build_system_prompt, reply_schema
from .stt import DisabledSTT, SpeechToText, WhisperHTTPSTT


def load_knowledge(settings: Settings, handoffs: HandoffStore | None = None) -> KnowledgeBase:
    """Knowledge base files plus the operator answers a person approved."""
    kb = KnowledgeBase.load(settings.kb_dir)
    return kb.with_learned(handoffs.learned()) if handoffs is not None else kb


def build_engine(settings: Settings, handoffs: HandoffStore | None = None) -> SupportEngine:
    kb = load_knowledge(settings, handoffs)
    llm = ClaudeSupportLLM(build_system_prompt(kb), reply_schema(kb), settings.claude_model, settings.claude_effort)
    stt: SpeechToText = (
        WhisperHTTPSTT(settings.stt_api_url, settings.stt_api_key, settings.stt_model,
                       language_hint=settings.stt_language_hint)
        if settings.stt_api_key else DisabledSTT()
    )
    return SupportEngine(llm, stt, ConversationStore(settings.history_turns), kb.videos)
