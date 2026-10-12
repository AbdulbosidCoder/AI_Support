"""Wire the engine from settings."""
from __future__ import annotations

from .cases import CaseStore
from .config import Settings
from .engine import ConversationStore, SupportEngine
from .handoffs import HandoffStore
from .knowledge import KnowledgeBase
from .llm import ClaudeSupportLLM
from .prompt import build_system_prompt, reply_schema
from .stt import DisabledSTT, SpeechToText, WhisperHTTPSTT


def load_knowledge(settings: Settings, handoffs: HandoffStore | None = None,
                   cases: CaseStore | None = None) -> KnowledgeBase:
    """Knowledge base files plus the cases admins added and the operator answers a person approved."""
    kb = KnowledgeBase.load(settings.kb_dir)
    if cases is not None:
        kb = kb.with_cases(cases.topics())
    return kb.with_learned(handoffs.learned()) if handoffs is not None else kb


def build_engine(settings: Settings, handoffs: HandoffStore | None = None,
                 cases: CaseStore | None = None) -> SupportEngine:
    kb = load_knowledge(settings, handoffs, cases)
    llm = ClaudeSupportLLM(build_system_prompt(kb), reply_schema(kb), settings.claude_model, settings.claude_effort)
    stt: SpeechToText = (
        WhisperHTTPSTT(settings.stt_api_url, settings.stt_api_key, settings.stt_model,
                       language_hint=settings.stt_language_hint)
        if settings.stt_api_key else DisabledSTT()
    )
    engine = SupportEngine(llm, stt, ConversationStore(settings.history_turns),
                           kb.videos, settings.history_images)
    engine.set_always_escalate(always_escalate(kb))
    return engine


def always_escalate(kb: KnowledgeBase) -> set[str]:
    """Topics whose answer always goes to a person ("Эскалация, если: Всегда")."""
    return {f'{t["section"]}/{t["id"]}' for t in kb.topics + kb.cases
            if (t.get("escalate_if") or "").startswith("Всегда")}
