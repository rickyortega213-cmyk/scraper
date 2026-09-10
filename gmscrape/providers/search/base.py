"""Web search providers - used to find a business's website when Maps has none,
and to find out who owns it."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from ...config import Settings
from ...util import hostname, registered_domain, squeeze
from ..base import ApiClient


@dataclass
class SearchHit:
    url: str
    title: str = ""
    snippet: str = ""
    position: int = 0

    @property
    def domain(self) -> str:
        return registered_domain(self.url) or hostname(self.url)

    @property
    def host(self) -> str:
        return hostname(self.url)

    @property
    def text(self) -> str:
        return squeeze(f"{self.title} {self.snippet}")


@dataclass
class SearchResponse:
    query: str
    hits: list[SearchHit] = field(default_factory=list)
    ai_overview: str = ""          # Google's AI Overview text, when present
    answer: str = ""               # answer box / featured snippet
    knowledge: dict[str, Any] = field(default_factory=dict)   # knowledge panel
    extra_text: list[str] = field(default_factory=list)       # People Also Ask etc.
    from_cache: bool = False
    error: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.error

    def all_text_blocks(self) -> list[tuple[str, str]]:
        """(source_label, text) in decreasing order of trust."""
        blocks: list[tuple[str, str]] = []
        if self.ai_overview:
            blocks.append(("search_ai_overview", self.ai_overview))
        if self.answer:
            blocks.append(("search_answer", self.answer))
        knowledge_text = " ".join(
            str(v) for k, v in self.knowledge.items() if isinstance(v, (str, int, float))
        )
        if knowledge_text.strip():
            blocks.append(("search_knowledge", knowledge_text))
        for hit in self.hits:
            if hit.text:
                blocks.append(("search_snippet", hit.text))
        for text in self.extra_text:
            blocks.append(("search_related", text))
        return blocks


class WebSearchProvider(ABC):
    name: str = "base"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.client = ApiClient(timeout=30.0, retries=2)

    @abstractmethod
    def search(self, query: str, limit: int = 10) -> SearchResponse:
        """Run one query. Must not raise on API errors - set `error` instead."""

    def close(self) -> None:
        self.client.close()
