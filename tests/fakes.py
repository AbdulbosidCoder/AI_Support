from ai_support.llm import LLMError, ModelAnswer
from ai_support.models import Audio
from ai_support.stt import STTError


def answer(text="Ok", lang="ru", escalate=False, reason="", pii=False, topic="other", screen=None):
    return ModelAnswer(lang, topic, screen, text, escalate, reason, pii)


class FakeLLM:
    def __init__(self, result=None, error=None):
        self.result = result or answer()
        self.error = error
        self.calls = []

    async def answer(self, history, text, images):
        self.calls.append((list(history), text, list(images)))
        if self.error:
            raise LLMError(self.error)
        return self.result


class FakeSTT:
    def __init__(self, text=None):
        self.text = text

    async def transcribe(self, audio: Audio) -> str:
        if self.text is None:
            raise STTError("disabled")
        return self.text
