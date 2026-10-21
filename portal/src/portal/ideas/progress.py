"""Where an ideas run is, for the Ideas tab's progress display (GET /ideas/status).

A run has five steps. Reading is counted page by page; each model call (generation, then
scoring) is one request with no progress of its own, so the page shows it as busy rather
than as a guessed percentage.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

STEPS = ("Read the wiki", "Ask the model", "Check sources", "Score", "Save")
SCOPES = ("recent", "wiki")  # what was filed since the last run | the whole wiki


@dataclass
class Progress:
    scope: str
    model: str
    step: int = 1  # 1-based index into STEPS
    done: int = 0  # pages and sources read
    total: int = 0
    finished: bool = False
    error: str = ""
    proposed: int = 0
    kept: int = 0
    started: float = field(default_factory=time.monotonic)
    ended: float | None = None

    def read(self, count: int = 1) -> None:
        self.done += count
        self.total = max(self.total, self.done)

    def expect(self, count: int) -> None:
        self.total = max(self.total, self.done + count)

    def to(self, step: int) -> None:
        self.step = step

    def end(self, error: str = "") -> None:
        self.error = error
        self.finished = True
        self.ended = time.monotonic()

    @property
    def seconds(self) -> int:
        return round((self.ended or time.monotonic()) - self.started)

    @property
    def seconds_since_end(self) -> float:
        return time.monotonic() - self.ended if self.ended is not None else 0.0

    @property
    def text(self) -> str:
        if self.finished:
            if self.error:
                return f"Stopped: {self.error}"
            if not self.done:
                return "Nothing new to read."
            read = f"{self.done} pages and conversations"
            if self.kept:
                return f"Done: {self.kept} new idea{'' if self.kept == 1 else 's'} from {read}."
            if self.proposed:
                return (
                    f"Done: nothing new from {read}. The model's {self.proposed} "
                    f"suggestion{'' if self.proposed == 1 else 's'} repeated earlier ideas, "
                    "cited pages the wiki doesn't have, broke the format, or scored too low."
                )
            return f"Done: the model found nothing worth suggesting in {read}."
        if self.step == 1:
            return f"Reading page {self.done} of {self.total}" if self.total else "Finding pages"
        if self.step == 2:
            return f"Waiting for {self.model}; this can take a minute"
        if self.step == 3:
            return "Checking that every cited page exists"
        if self.step == 4:
            return "Scoring the suggestions"
        return "Saving the ideas"

    def as_dict(self) -> dict[str, Any]:
        return {
            "scope": self.scope,
            "model": self.model,
            "step": self.step,
            "steps": list(STEPS),
            "done": self.done,
            "total": self.total,
            "finished": self.finished,
            "error": self.error,
            "kept": self.kept,
            "text": self.text,
            "seconds": self.seconds,
        }
