"""Knowledge base loader: app screens (screens.json), support topics (faq.json) and operator
answers a person approved (see ai_support/handoffs.py)."""
from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from pathlib import Path

from . import guardrails
from .videos import VideoLibrary


@dataclass
class KnowledgeBase:
    screens: list[dict]
    topics: list[dict]
    global_rules: list[str]
    # Approved operator answers: {"id", "language", "topic", "question", "answer"}.
    learned: list[dict] = field(default_factory=list)
    videos: VideoLibrary = field(default_factory=VideoLibrary)

    @classmethod
    def load(cls, kb_dir: Path) -> "KnowledgeBase":
        screens = json.loads((kb_dir / "screens.json").read_text(encoding="utf-8"))
        faq = json.loads((kb_dir / "faq.json").read_text(encoding="utf-8"))
        return cls(screens["screens"], faq["topics"], screens.get("global_rules", []),
                   videos=VideoLibrary.load(kb_dir))

    def with_learned(self, learned: list[dict]) -> "KnowledgeBase":
        return replace(self, learned=list(learned))

    def screen_ids(self) -> list[str]:
        return [s["id"] for s in self.screens]

    def topic_ids(self) -> list[str]:
        return [f'{t["section"]}/{t["id"]}' for t in self.topics]

    def render(self) -> str:
        """Compact, deterministic text for the system prompt (stable for prompt caching)."""
        lines = ["## Экраны приложения (для распознавания скриншотов)"]
        for s in self.screens:
            lines.append(f'### screen_id={s["id"]} | page={s["page_id"]} | state={s["state"]}')
            lines.append(f'Страница: {s["page_name"]}')
            lines.append("Видимый текст: " + " · ".join(s["visible_text"]))
            if s.get("error_text"):
                lines.append(f'Текст ошибки: «{s["error_text"]}»')
            lines.append(f'Что происходит: {s["problem"]} {s["explanation_ru"]}')
            lines.append("Шаги: " + " ".join(f"{i}) {x}" for i, x in enumerate(s["fix_steps_ru"], 1)))
            if s.get("forbidden"):
                lines.append("Нельзя: " + " ".join(s["forbidden"]))
            if s.get("escalate"):
                lines.append("Эскалация: всегда.")
            elif s.get("escalate_if"):
                lines.append(f'Эскалация, если: {s["escalate_if"]}')
        lines.append("")
        lines.append("## Темы поддержки")
        for t in self.topics:
            lines.append(f'### topic={t["section"]}/{t["id"]}: {t["title"]}')
            lines.append("Ответ: " + " ".join(f"{i}) {x}" for i, x in enumerate(t["answer_steps"], 1)))
            if t.get("forbidden"):
                lines.append("Нельзя: " + " ".join(t["forbidden"]))
            if t.get("escalate_if"):
                lines.append(f'Эскалация, если: {t["escalate_if"]}')
            if self.videos.for_topic(f'{t["section"]}/{t["id"]}'):
                lines.append("Видео-инструкция: есть. Система сама отправит видео после твоего ответа; "
                             "коротко скажи, что видео ниже, ссылок не пиши.")
        # Defense in depth: an answer that breaks a hard rule never reaches the prompt, even if stored.
        learned = [x for x in self.learned if not guardrails.find_violations(x["answer"])]
        if learned:
            lines.append("")
            lines.append("## Проверенные ответы операторов")
            lines.append("Ответы сотрудников поддержки на вопросы, которые бот раньше передавал оператору; "
                         "одобрены человеком. Используй их как типовой ответ на похожий вопрос. Жёсткие правила "
                         "важнее: не переноси детали чужого обращения (суммы, даты, статусы, решения по операции).")
            for x in learned:
                lines.append(f'### learned={x["id"]} | lang={x["language"]} | topic={x["topic"] or "-"}')
                lines.append(f'Вопрос клиента: {x["question"]}')
                lines.append(f'Ответ оператора: {x["answer"]}')
        return "\n".join(lines)
