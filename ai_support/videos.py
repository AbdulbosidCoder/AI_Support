"""Instruction videos (videos.json): which video goes with which support topic.

The library only says which video fits an answer; channels decide how to send it
(Telegram sends a local file, a URL or a cached file_id; the mobile app can show the URL).
A video is offered only when it is available: a file under the videos folder, a URL or a
channel file id. Placeholders without a source are kept in the file but never sent.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .models import Lang, VideoAttachment


@dataclass(frozen=True)
class Video:
    id: str
    title: dict[Lang, str]
    topics: tuple[str, ...]
    path: Path | None = None
    url: str = ""
    # Ids of an already uploaded copy per channel, e.g. {"telegram": "<file_id>"}.
    file_ids: dict[str, str] = field(default_factory=dict)

    @property
    def available(self) -> bool:
        return bool((self.path and self.path.is_file()) or self.url or self.file_ids)

    def attachment(self, lang: Lang) -> VideoAttachment:
        title = self.title.get(lang) or self.title.get(Lang.UZ_LATN) or next(iter(self.title.values()), "")
        return VideoAttachment(self.id, title, str(self.path) if self.path and self.path.is_file() else None,
                               self.url, dict(self.file_ids))


@dataclass
class VideoLibrary:
    videos: list[Video] = field(default_factory=list)

    @classmethod
    def load(cls, kb_dir: Path) -> "VideoLibrary":
        """Read kb_dir/videos.json; files are looked up in kb_dir/videos/. No file means no videos."""
        path = kb_dir / "videos.json"
        if not path.is_file():
            return cls()
        data = json.loads(path.read_text(encoding="utf-8"))
        videos = []
        for v in data.get("videos", []):
            videos.append(Video(
                id=v["id"],
                title={Lang(k): s for k, s in v.get("title", {}).items()},
                topics=tuple(v.get("topics", [])),
                path=kb_dir / "videos" / v["file"] if v.get("file") else None,
                url=v.get("url", ""),
                file_ids=dict(v.get("file_ids", {})),
            ))
        return cls(videos)

    def available(self) -> list[Video]:
        return [v for v in self.videos if v.available]

    def for_topic(self, topic: str) -> list[Video]:
        """Available videos for a knowledge-base topic ("section/id")."""
        return [v for v in self.available() if topic in v.topics]
