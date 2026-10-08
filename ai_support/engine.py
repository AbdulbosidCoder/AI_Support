"""Channel-independent support engine: one incoming message in, one safe reply out."""
from __future__ import annotations

import logging
from collections import defaultdict, deque

from . import guardrails
from .feedback import Assessment
from .images import MAX_IMAGES_PER_MESSAGE, prepare_image
from .language import detect_language, is_small_talk
from .llm import LLMError, ModelAnswer, SupportLLM, Turn
from .models import BotReply, IncomingMessage, Lang
from .pii import mask_pii
from .stt import SpeechToText, STTError
from .templates import t
from .videos import VideoLibrary

log = logging.getLogger(__name__)


class ConversationStore:
    """Short per-user text history (in memory). Replace with a DB for multi-instance deploys."""

    def __init__(self, max_turns: int = 6):
        self._turns: dict[str, deque[Turn]] = defaultdict(lambda: deque(maxlen=max_turns * 2))
        self._lang: dict[str, Lang] = {}
        self._videos_sent: dict[str, set[str]] = defaultdict(set)

    def history(self, user_id: str) -> list[Turn]:
        turns = list(self._turns[user_id])
        while turns and turns[0].role != "user":  # the API expects the first turn to be the user's
            turns.pop(0)
        return turns

    def add(self, user_id: str, client_text: str, bot_text: str) -> None:
        self._turns[user_id].append(Turn("user", mask_pii(client_text)))
        self._turns[user_id].append(Turn("assistant", bot_text))

    def clear(self, user_id: str) -> None:
        self._turns.pop(user_id, None)
        self._videos_sent.pop(user_id, None)

    def video_sent(self, user_id: str, video_id: str) -> bool:
        return video_id in self._videos_sent[user_id]

    def mark_video_sent(self, user_id: str, video_id: str) -> None:
        self._videos_sent[user_id].add(video_id)

    def language(self, user_id: str) -> Lang | None:
        return self._lang.get(user_id)

    def set_language(self, user_id: str, lang: Lang) -> None:
        self._lang[user_id] = lang


class SupportEngine:
    def __init__(self, llm: SupportLLM, stt: SpeechToText, store: ConversationStore | None = None,
                 videos: VideoLibrary | None = None):
        self._llm = llm
        self._stt = stt
        self._store = store or ConversationStore()
        self._videos = videos or VideoLibrary()

    def welcome(self, user_id: str, hint: str = "") -> str:
        return t("welcome", self._store.language(user_id) or detect_language(hint))

    def remember_language(self, user_id: str, lang: Lang) -> None:
        """The client chose a language: use it for fixed replies until their messages say otherwise."""
        self._store.set_language(user_id, lang)

    def set_system_prompt(self, system_prompt: str) -> None:
        self._llm.set_system_prompt(system_prompt)

    def recent_turns(self, user_id: str) -> list[Turn]:
        """The client's recent conversation (PII already masked), saved with a hand-off."""
        return self._store.history(user_id)

    def end_conversation(self, user_id: str) -> None:
        """The conversation ended: the next question starts without the old context (language is kept)."""
        self._store.clear(user_id)

    async def assess_client(self, user_id: str, history: list[Turn] | None = None) -> Assessment | None:
        """Internal AI assessment of the client from the recent conversation (or `history`, e.g. the whole
        ended conversation with an operator); None if there is nothing to judge.

        Only for the support team: it never changes what the bot answers.
        """
        history = history or self._store.history(user_id)
        if not any(turn.role == "user" and turn.text.strip() for turn in history):
            return None
        try:
            return await self._llm.assess(history)
        except LLMError as e:
            log.warning("client assessment failed: %s", e)
            return None

    def handoff(self, user_id: str, default: Lang = Lang.UZ_LATN) -> BotReply:
        """Client explicitly asked for a human; `default` is the client's chosen language, if known."""
        lang = self._store.language(user_id) or default
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
            return BotReply(t("empty", lang), lang, show_menu=True)

        if not images and is_small_talk(text):
            # A greeting or a bare "help": ask what the problem is and offer the quick questions.
            lang = detect_language(text, default=known_lang or Lang.UZ_LATN)
            return BotReply(f"{t('ask_problem', lang)}\n\n{t('menu_hint', lang)}", lang, topic="greeting",
                            client_text=mask_pii(text), show_menu=True)

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
        reply.videos = self._videos_for(msg.user_id, reply)
        self._store.set_language(msg.user_id, reply.language)
        self._store.add(msg.user_id, _history_text(text, len(images), reply.screen_id), reply.text)
        return reply

    def _videos_for(self, user_id: str, reply: BotReply):
        """Instruction videos for the answer's topic, once per conversation; never with a replaced answer."""
        if reply.guardrail_triggered:
            return []
        videos = []
        for video in self._videos.for_topic(reply.topic):
            if self._store.video_sent(user_id, video.id):
                continue
            self._store.mark_video_sent(user_id, video.id)
            videos.append(video.attachment(reply.language))
        return videos

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
            show_menu=ans.topic == "greeting" and not escalate,
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
