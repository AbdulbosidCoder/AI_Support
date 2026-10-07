"""Instruction videos: KB mapping, engine attachment, Telegram sending."""
import asyncio
import json
import shutil
from pathlib import Path
from types import SimpleNamespace as NS

from aiogram.types import FSInputFile, URLInputFile

from ai_support.channels.telegram_bot import TelegramSupportBot, video_input
from ai_support.config import Settings
from ai_support.engine import SupportEngine
from ai_support.knowledge import KnowledgeBase
from ai_support.models import IncomingMessage, Lang, VideoAttachment
from ai_support.prompt import build_system_prompt
from ai_support.videos import VideoLibrary
from fakes import FakeLLM, FakeSTT, answer

ROOT = Path(__file__).resolve().parents[1]
KB_DIR = ROOT / "data" / "knowledge_base"
TOPIC = "apartment/add_contract"


def kb_with_video(tmp_path, **source) -> Path:
    """A copy of the real knowledge base whose contract video has the given source."""
    kb = tmp_path / "kb"
    shutil.copytree(KB_DIR, kb)
    data = json.loads((kb / "videos.json").read_text(encoding="utf-8"))
    video = data["videos"][0]
    video.update({"file": "", "url": "", "file_ids": {}}, **source)
    (kb / "videos.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    if source.get("file"):
        (kb / "videos" / source["file"]).write_bytes(b"\x00\x00\x00\x18ftypmp42")
    return kb


def test_every_video_points_to_known_topics_and_has_titles_in_all_languages():
    kb = KnowledgeBase.load(KB_DIR)
    assert kb.videos.videos, "videos.json is loaded"
    for v in kb.videos.videos:
        assert set(v.topics) <= set(kb.topic_ids()), v.id
        assert set(v.title) == set(Lang), v.id
        assert all("Xonsaroy Pay" in title for title in v.title.values()), v.id


def test_video_without_a_source_is_never_offered():
    kb = KnowledgeBase.load(KB_DIR)
    if not kb.videos.available():  # the file is not in the repo yet
        assert kb.videos.for_topic(TOPIC) == []
        assert "Видео-инструкция" not in kb.render()


def test_local_file_makes_video_available_and_prompt_mentions_it(tmp_path):
    kb = KnowledgeBase.load(kb_with_video(tmp_path, file="contract.mp4"))
    [video] = kb.videos.for_topic(TOPIC)
    assert video.path.name == "contract.mp4"
    assert kb.videos.for_topic("apartment/pay_by_contract") == [video]
    assert kb.videos.for_topic("cards/add_card") == []
    prompt = build_system_prompt(kb)
    section = prompt.split(f"topic={TOPIC}:")[1].split("### topic=")[0]
    assert "Видео-инструкция: есть" in section


def test_url_or_file_id_also_make_video_available(tmp_path):
    assert VideoLibrary.load(kb_with_video(tmp_path / "a", url="https://example.com/v.mp4")).for_topic(TOPIC)
    assert VideoLibrary.load(kb_with_video(tmp_path / "b", file_ids={"telegram": "AAA"})).for_topic(TOPIC)


def test_no_videos_json_means_no_videos(tmp_path):
    assert VideoLibrary.load(tmp_path).videos == []


def engine_with_video(tmp_path, llm):
    videos = VideoLibrary.load(kb_with_video(tmp_path, url="https://example.com/v.mp4"))
    return SupportEngine(llm, FakeSTT(), videos=videos)


def ask(engine, text="Shartnomani qanday qo'shaman?", user="1"):
    return asyncio.run(engine.handle(IncomingMessage(user, text)))


def test_engine_attaches_video_once_per_conversation_in_client_language(tmp_path):
    llm = FakeLLM(answer("Xonsaroy Pay ilovasida «To'lov» bo'limini oching. Video quyida.", "uz_latn", topic=TOPIC))
    engine = engine_with_video(tmp_path, llm)
    first = ask(engine)
    assert [v.id for v in first.videos] == ["contract_and_payment"]
    assert first.videos[0].title.startswith("Video-yo'riqnoma") and first.videos[0].url
    assert ask(engine).videos == []  # already sent in this conversation
    assert ask(engine, user="2").videos  # another client gets it
    engine.end_conversation("1")
    assert ask(engine).videos  # a new conversation sends it again


def test_engine_sends_no_video_for_other_topics(tmp_path):
    engine = engine_with_video(tmp_path, FakeLLM(answer("Kartalarim bo'limiga kiring.", topic="cards/add_card")))
    assert ask(engine, "Kartani qanday qo'shaman?").videos == []


def test_no_video_when_the_answer_was_replaced_by_guardrails(tmp_path):
    llm = FakeLLM(answer("To'lov 2 soat ichida qaytadi.", "uz_latn", topic=TOPIC))
    reply = ask(engine_with_video(tmp_path, llm))
    assert reply.guardrail_triggered and reply.videos == []


def test_restricted_request_still_escalates_with_video(tmp_path):
    llm = FakeLLM(answer("Shartnomani qo'shish tartibi videoda.", "uz_latn", topic=TOPIC))
    reply = ask(engine_with_video(tmp_path, llm), "Shartnoma bo'yicha to'lovni bekor qiling")
    assert reply.escalate and not reply.guardrail_triggered


def test_prompt_names_the_app_xonsaroy_pay():
    prompt = build_system_prompt(KnowledgeBase.load(KB_DIR))
    assert "приложение Xonsaroy Pay" in prompt and "Xonsaroy Pay ilovasi" in prompt


# --- Telegram -----------------------------------------------------------------------------------

def test_video_input_prefers_uploaded_copy_then_file_then_url():
    v = VideoAttachment("v", "t", path="/tmp/v.mp4", url="https://example.com/v.mp4")
    assert video_input(v, {"v": "FILEID"}) == "FILEID"
    assert isinstance(video_input(v, {}), FSInputFile)
    assert isinstance(video_input(VideoAttachment("v", "t", url="https://example.com/v.mp4"), {}), URLInputFile)
    assert video_input(VideoAttachment("v", "t", file_ids={"telegram": "X"}), {}) == "X"
    assert video_input(VideoAttachment("v", "t"), {}) is None


class VideoChat:
    def __init__(self, fail=False):
        self.sent = []
        self.fail = fail

    async def answer(self, text, reply_markup=None, **_):
        self.sent.append(("text", text))
        return NS(message_id=len(self.sent))

    async def answer_video(self, video, caption=None, **_):
        if self.fail:
            raise RuntimeError("telegram down")
        self.sent.append(("video", video, caption))
        return NS(video=NS(file_id="UPLOADED"))

    async def send_chat_action(self, *_):
        pass


def client_msg(chat, text, uid=42):
    return NS(text=text, caption=None, photo=None, document=None, reply_to_message=None, voice=None, audio=None,
              media_group_id=None, chat=NS(id=uid),
              from_user=NS(id=uid, username="ali", full_name="Ali", language_code="uz"),
              answer=chat.answer, answer_video=chat.answer_video, message_id=1)


def tg_bot(tmp_path):
    llm = FakeLLM(answer("Xonsaroy Pay ilovasida «To'lov» bo'limini oching. Video quyida.", "uz_latn", topic=TOPIC))
    return TelegramSupportBot(Settings(), engine_with_video(tmp_path, llm))


def test_telegram_sends_text_then_video_and_reuses_the_upload(tmp_path):
    bot = tg_bot(tmp_path)
    chat = VideoChat()
    asyncio.run(bot.on_client_message(client_msg(chat, "Shartnoma qo'shish"), chat))
    kinds = [s[0] for s in chat.sent]
    assert kinds == ["text", "video"]
    _, media, caption = chat.sent[1]
    assert isinstance(media, URLInputFile) and "Xonsaroy Pay" in caption
    other = VideoChat()
    asyncio.run(bot.on_client_message(client_msg(other, "Shartnoma qo'shish", uid=43), other))
    assert other.sent[1][1] == "UPLOADED"  # Telegram file_id of the first upload


def test_failed_video_does_not_break_the_answer(tmp_path):
    chat = VideoChat(fail=True)
    asyncio.run(tg_bot(tmp_path).on_client_message(client_msg(chat, "Shartnoma qo'shish"), chat))
    assert [s[0] for s in chat.sent] == ["text"]


# --- Contract steps written from the video -----------------------------------------------------

from ai_support import guardrails  # noqa: E402

CONTRACT_TOPICS = ["apartment/add_contract", "apartment/pay_by_contract", "apartment/schedule_receipt"]


def test_contract_steps_follow_the_video_and_break_no_hard_rule():
    kb = KnowledgeBase.load(KB_DIR)
    topics = {f'{t["section"]}/{t["id"]}': t for t in kb.topics}
    for tid in CONTRACT_TOPICS:
        steps = " ".join(topics[tid]["answer_steps"])
        assert not guardrails.find_violations(steps), tid
    add = " ".join(topics["apartment/add_contract"]["answer_steps"])
    for label in ("To'lov", "Shartnoma raqamini kiriting", "№ shartnoma", "QO'SHISH"):
        assert label in add
    pay = " ".join(topics["apartment/pay_by_contract"]["answer_steps"])
    for label in ("To'lov qilish", "Kartadan", "TO'LASH", "To'lovni tasdiqlash", "Tarix"):
        assert label in pay
    assert any("зачисл" in f for f in topics["apartment/pay_by_contract"]["forbidden"])
