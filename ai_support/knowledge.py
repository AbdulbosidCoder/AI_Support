"""Knowledge base loader: app screens (screens.json) and support topics (faq.json)."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass
class KnowledgeBase:
    screens: list[dict]
    topics: list[dict]
    global_rules: list[str]

    @classmethod
    def load(cls, kb_dir: Path) -> "KnowledgeBase":
        screens = json.loads((kb_dir / "screens.json").read_text(encoding="utf-8"))
        faq = json.loads((kb_dir / "faq.json").read_text(encoding="utf-8"))
        return cls(screens["screens"], faq["topics"], screens.get("global_rules", []))

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
            if t.get("escalate_if"):
                lines.append(f'Эскалация, если: {t["escalate_if"]}')
        return "\n".join(lines)
