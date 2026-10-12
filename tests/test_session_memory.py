"""Within one session the model remembers every message and image; a new session starts clean."""
import asyncio
import io
import json
from types import SimpleNamespace as NS

import anthropic
import httpx2 as httpx

from ai_support.channels.telegram_bot import TelegramSupportBot
from ai_support.chatlog import BOT, CLIENT, OPERATOR, SYSTEM, ChatLog
from ai_support.config import Settings
from ai_support.engine import OLD_IMAGE_NOTE, SupportEngine, fit_history
from ai_support.feedback import FeedbackStore
from ai_support.llm import SESSION_START, ClaudeSupportLLM, Turn, history_messages
from ai_support.models import Image, IncomingMessage, Lang
from ai_support.users import UserStore
from fakes import FakeLLM, FakeSTT, answer, png


def test_history_messages_keep_images_merge_sides_and_start_with_the_client():
    shot = Image(png(), "image/png")
    msgs = history_messages([
        Turn("assistant", "Bot greeting"),
        Turn("user", "Karta qo'shilmayapti", [shot]),
        Turn("user", "Yana bir rasm", [shot]),
        Turn("assistant", "Operator javobi"),
        Turn("assistant", "Yana javob"),
    ])
    assert [m["role"] for m in msgs] == ["user", "assistant", "user", "assistant"]
    assert msgs[0]["content"] == [{"type": "text", "text": SESSION_START}]
    client = msgs[2]["content"]
    assert [b["type"] for b in client] == ["image", "text", "image", "text"]
    assert client[1]["text"] == "<client_message>\nKarta qo'shilmayapti\n</client_message>"
    assert [b["text"] for b in msgs[3]["content"]] == ["Operator javobi", "Yana javob"]
    assert msgs[-1]["content"][-1]["cache_control"] == {"type": "ephemeral"}
    assert history_messages([]) == []


def make_llm(seen):
    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={
            "id": "m", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
            "content": [{"type": "text", "text": json.dumps({
                "language": "ru", "topic": "other", "screen_id": None, "answer": "Ок", "needs_escalation": False,
                "escalation_reason": "", "pii_visible": False})}],
            "stop_reason": "end_turn", "stop_sequence": None, "usage": {"input_tokens": 1, "output_tokens": 1}})
    client = anthropic.AsyncAnthropic(api_key="test", max_retries=0,
                                      http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    return ClaudeSupportLLM("SYSTEM", {"type": "object"}, client=client)


def test_request_resends_earlier_images_and_appends_to_a_waiting_client_turn():
    seen = []
    history = [Turn("user", "Вот ошибка", [Image(b"old", "image/png")]), Turn("assistant", "Вижу."),
               Turn("user", "Написал, пока ждал оператора")]
    asyncio.run(make_llm(seen).answer(history, "Ну что?", []))
    msgs = seen[0]["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant", "user"]
    assert msgs[0]["content"][0]["type"] == "image"
    assert "Ну что?" in msgs[2]["content"][-1]["text"] and "пока ждал" in msgs[2]["content"][0]["text"]


def test_engine_remembers_the_images_of_the_session_until_it_ends():
    llm = FakeLLM(answer("Понял"))
    engine = SupportEngine(llm, FakeSTT())
    asyncio.run(engine.handle(IncomingMessage("7", "Вот скриншот", images=[Image(png())])))
    asyncio.run(engine.handle(IncomingMessage("7", "А что делать?")))
    history = llm.calls[1][0]
    assert history[0].role == "user" and "Вот скриншот" in history[0].text and len(history[0].images) == 1
    assert history[1] == Turn("assistant", "Понял")
    engine.end_conversation("7")
    asyncio.run(engine.handle(IncomingMessage("7", "Новый вопрос")))
    assert llm.calls[2][0] == []


def test_engine_remembers_more_than_six_exchanges():
    llm = FakeLLM(answer("Ок"))
    engine = SupportEngine(llm, FakeSTT())
    for i in range(12):
        asyncio.run(engine.handle(IncomingMessage("7", f"Сообщение {i}")))
    assert "Сообщение 0" in llm.calls[-1][0][0].text and len(llm.calls[-1][0]) == 22


def test_only_the_latest_images_are_resent():
    img = Image(b"x")
    turns = [Turn("user", "1", [img, img]), Turn("assistant", "a"), Turn("user", "2", [img]), Turn("user", "3", [img])]
    fitted = fit_history(turns, 3)
    assert [len(t.images) for t in fitted] == [1, 0, 1, 1]
    assert OLD_IMAGE_NOTE in fitted[0].text and fitted[2].text == "2"
    assert all(not t.images for t in fit_history(turns, 0))


def test_engine_passes_the_channel_session_instead_of_its_own_memory():
    llm = FakeLLM()
    engine = SupportEngine(llm, FakeSTT(), max_history_images=1)
    given = [Turn("user", "Rasm", [Image(b"a")]), Turn("assistant", "Operator: tekshiramiz")]
    asyncio.run(engine.handle(IncomingMessage("7", "Yana rasm", images=[Image(png())]), given))
    history = llm.calls[0][0]
    # The new image takes the only place: the earlier one becomes a note.
    assert history[0].images == [] and OLD_IMAGE_NOTE in history[0].text and history[1].text == "Operator: tekshiramiz"


# --- the client bot: the session from the chat log -------------------------------------------

class TgBot:
    def __init__(self):
        self.downloads = []

    async def download(self, file_id):
        self.downloads.append(file_id)
        if file_id == "gone":
            raise RuntimeError("file is gone")
        return io.BytesIO(png())

    async def send_chat_action(self, *_):
        pass


def support_bot(llm=None):
    users = UserStore(":memory:")
    users.touch("telegram", "42", "42", "ali", "Ali", "ru")
    users.set_language("telegram", "42", Lang.UZ_LATN)
    users.set_phone("telegram", "42", "998901234567")
    feedback, chatlog = FeedbackStore(":memory:"), ChatLog(":memory:")
    bot = TelegramSupportBot(Settings(), SupportEngine(llm or FakeLLM(), FakeSTT()), users, feedback=feedback,
                             chatlog=chatlog)
    return bot, feedback, chatlog


def test_session_history_has_every_message_photo_and_operator_reply():
    bot, feedback, chatlog = support_bot()
    tg = TgBot()
    assert asyncio.run(bot.session_history(tg, "42")) is None  # no open session
    conv = feedback.bot_answered("telegram", "42", "uz_latn")
    chatlog.add("telegram", "42", CLIENT, "Earlier session", conversation_id=None)
    chatlog.add("telegram", "42", CLIENT, "Karta qo'shilmayapti", conversation_id=conv.id)
    chatlog.add("telegram", "42", BOT, "Xato matnini yuboring", conversation_id=conv.id)
    chatlog.add("telegram", "42", CLIENT, "[фото]", "photo", conversation_id=conv.id, files=["F1"])
    chatlog.add("telegram", "42", CLIENT, "[фото]", "photo", conversation_id=conv.id, files=["gone"])
    chatlog.add("telegram", "42", SYSTEM, "Передано специалисту", conversation_id=conv.id)
    chatlog.add("telegram", "42", OPERATOR, "Kartangiz banki qaysi?", operator_name="Ali", conversation_id=conv.id)
    chatlog.add("telegram", "42", CLIENT, "🎤 Agrobank", "voice", conversation_id=conv.id, files=["V1"])
    turns = asyncio.run(bot.session_history(tg, "42"))
    assert [(t.role, t.text, len(t.images)) for t in turns] == [
        ("user", "Karta qo'shilmayapti", 0), ("assistant", "Xato matnini yuboring", 0), ("user", "", 1),
        ("user", "[клиент прислал изображение]", 0), ("assistant", "Kartangiz banki qaysi?", 0),
        ("user", "🎤 Agrobank", 0)]
    assert sorted(tg.downloads) == ["F1", "gone"]  # voice is not downloaded: its text is in the log
    asyncio.run(bot.session_history(tg, "42"))
    assert tg.downloads.count("F1") == 1  # cached


def test_waiting_messages_are_not_repeated_as_history_when_the_ai_takes_over():
    bot, feedback, chatlog = support_bot()
    conv = feedback.bot_answered("telegram", "42", "ru")
    chatlog.add("telegram", "42", BOT, "Ответ бота", conversation_id=conv.id)
    chatlog.add("telegram", "42", CLIENT, "Жду оператора", conversation_id=conv.id)
    chatlog.add("telegram", "42", BOT, "Отвечает AI", conversation_id=conv.id)
    turns = asyncio.run(bot.session_history(TgBot(), "42", pending=True, skip=("Отвечает AI",)))
    assert [t.text for t in turns] == ["Ответ бота"]


def test_second_question_of_a_session_reaches_the_model_with_the_first():
    llm = FakeLLM(answer("Kartalarim bo'limini oching"))
    bot, feedback, chatlog = support_bot(llm)
    sent = []

    async def reply(text, reply_markup=None, **_):
        sent.append(text)

    def message(text):
        return NS(text=text, caption=None, photo=None, document=None, reply_to_message=None, voice=None, audio=None,
                  media_group_id=None, chat=NS(id=42), message_id=len(sent) + 1, answer=reply,
                  from_user=NS(id=42, username="ali", full_name="Ali", language_code="uz"))

    tg = TgBot()
    asyncio.run(bot.on_client_message(message("Karta qanday qo'shiladi?"), tg))
    asyncio.run(bot.on_client_message(message("Topolmadim"), tg))
    history = llm.calls[1][0]
    assert [t.text for t in history] == ["Karta qanday qo'shiladi?", "Kartalarim bo'limini oching"]
    # The session ends (maybe from the admin panel): the next question starts clean.
    feedback.end("telegram", "42", "client")
    asyncio.run(bot.on_client_message(message("Yangi savol"), tg))
    assert llm.calls[2][0] == []
