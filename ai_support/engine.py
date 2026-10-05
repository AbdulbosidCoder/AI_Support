"""Channel-independent support engine: one incoming message in, one safe reply out."""
from __future__ import annotations

import logging
from collections import defaultdict, deque

from . import guardrails
from .images import MAX_IMAGES_PER_MESSAGE, prepare_image
from .language import detect_language
from .llm import LLMError, ModelAnswer, SupportLLM, Turn
from .models import BotReply, IncomingMessage, Lang
from .pii import mask_pii
from .stt import SpeechToText, STTError
from .templates import t

log = logging.getLogger(__name__)


class ConversationStore:
    """Short per-user text history (in memory). Replace with a DB for multi-instance deploys."""

    def __init__(self, max_turns: int = 6):
        self._turns: dict[str, deque[Turn]] = defaultdict(lambda: deque(maxlen=max_turns * 2))
        self._lang: dict[str, Lang] = {}

    def history(self, user_id: str) -> list[Turn]:
        turns = list(self._turns[user_id])
        while turns and turns[0].role != "user":  # the API expects the first turn to be the user's
            turns.pop(0)
        return turns

    def add(self, user_id: str, client_text: str, bot_text: str) -> None:
        self._turns[user_id].append(Turn("user", mask_pii(client_text)))
        self._turns[user_id].append(Turn("assistant", bot_text))

    def language(self, user_id: str) -> Lang | None:
        return self._lang.get(user_id)

    def set_language(self, user_id: str, lang: Lang) -> None:
        self._lang[user_id] = lang


class SupportEngine:
    def __init__(self, llm: SupportLLM, stt: SpeechToText, store: ConversationStore | None = None):
        self._llm = llm
        self._stt = stt
        self._store = store or ConversationStore()

    def welcome(self, user_id: str, hint: str = "") -> str:
        return t("welcome", self._store.language(user_id) or detect_language(hint))

    def handoff(self, user_id: str) -> BotReply:
        """Client explicitly asked for a human."""
        lang = self._store.language(user_id) or Lang.UZ_LATN
        return BotReply(t("handoff", lang), lang, escalate=True, escalation_reason="client asked for an operator")

    async def handle(self, msg: IncomingMessage) -> BotReply:
        known_lang = self._store.language(msg.user_id)
        text = msg.text or ""

        if msg.audio is not None:
            try:
                spoken = await self._stt.transcribe(msg.audio)
            except STTError as e:
                log.warning("stt failed: %s", e)
                lang = known_lang or detect_language(text)
                return BotReply(t("voice_unavailable", lang), lang)
            text = f"{text}\n{spoken}".strip()

        images = [img for img in (prepare_image(i.data) for i in msg.images[:MAX_IMAGES_PER_MESSAGE]) if img]
        if msg.images and not images and not text.strip():
            lang = known_lang or Lang.UZ_LATN
            return BotReply(t("image_unsupported", lang), lang)
        if not text.strip() and not images:
            lang = known_lang or Lang.UZ_LATN
            return BotReply(t("empty", lang), lang)

        # Language of the latest message wins; for an image without text keep the last known one.
        fallback_lang = detect_language(text, default=known_lang or Lang.UZ_LATN) if text.strip() else (known_lang or Lang.UZ_LATN)
        restricted = guardrails.restricted_request(text)

        # Image without a caption: the question comes from the image; keep replying in the client's language.
        note = "" if text.strip() else f"Язык ответа, если на изображении нет вопроса клиента: {fallback_lang.value}."
        try:
            ans = await self._llm.answer(self._store.history(msg.user_id), text, images, note)
        except LLMError as e:
            log.error("llm failed: %s", e)
            return BotReply(t("error", fallback_lang), fallback_lang, escalate=True,
                            escalation_reason=f"model unavailable: {e}", client_text=mask_pii(text))

        reply = self._postprocess(ans, fallback_lang, restricted, text)
        self._store.set_language(msg.user_id, reply.language)
        self._store.add(msg.user_id, _history_text(text, len(images), reply.screen_id), reply.text)
        return reply

    def _postprocess(self, ans: ModelAnswer, fallback_lang: Lang, restricted: str | None, text: str) -> BotReply:
        try:
            lang = Lang(ans.language)
        except ValueError:
            lang = fallback_lang

        answer = mask_pii(ans.answer.strip())
        escalate = ans.needs_escalation
        reason = ans.escalation_reason
        guard = False

        violations = guardrails.find_violations(answer)
        if violations or not answer:
            # Never send a forbidden promise/confirmation: replace the whole answer.
            log.warning("guardrail replaced answer: %s", [v.category for v in violations])
            answer = t("guardrail", lang)
            escalate, guard = True, True
            reason = "guardrail: " + ", ".join(v.category for v in violations) if violations else "empty answer"

        if restricted:
            escalate = True
            reason = reason or f"client asks: {restricted}"

        if escalate and t("handoff", lang) not in answer and not guard:
            answer = f"{answer}\n\n{t('handoff', lang)}"
        if ans.pii_visible:
            answer = f"{answer}\n\n{t('pii_reminder', lang)}"

        return BotReply(
            text=answer,
            language=lang,
            escalate=escalate,
            escalation_reason=reason,
            topic=ans.topic,
            screen_id=ans.screen_id,
            client_text=mask_pii(text),
            guardrail_triggered=guard,
        )


def _history_text(text: str, n_images: int, screen_id: str | None) -> str:
    """What the client sent, as text for later turns (images themselves are not resent)."""
    if not n_images:
        return text
    seen = f"экран {screen_id}" if screen_id else "экран не из базы"
    return f"{text}\n[клиент прислал изображение ({n_images} шт.), распознано: {seen}]".strip()
