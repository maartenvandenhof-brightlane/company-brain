"""What the portal's assistant features share: the prompt, the model and their settings.

The prompt is config/assistant-prompt.md. LibreChat's "Second Brain" presets get it through
the model refresh (scripts/refresh_models.py puts it into the rendered librechat.yaml), and
the portal reads the same file, so the two chats cannot drift apart. config/ is mounted
read-only in the portal.

The model is the owner's pick (Settings), else the model of LibreChat's default preset,
from the rendered librechat.yaml. Settings live in `assistant.sqlite` under the portal's
state directory, next to the ideas and the chats.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any

import yaml

PROMPT_FILE = "assistant-prompt.md"
DB_FILE = "assistant.sqlite"
FALLBACK_MODEL = "glm-5-3"  # the template's default preset, before any render


class PromptMissing(RuntimeError):
    """config/assistant-prompt.md is missing or empty."""


def load_prompt(config_dir: Path) -> str:
    """The assistant prompt, read fresh each time (a config edit applies without a restart)."""
    path = config_dir / PROMPT_FILE
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise PromptMissing(f"{path} could not be read ({type(exc).__name__})") from exc
    if not text:
        raise PromptMissing(f"{path} is empty")
    return text


def presets(models_dir: Path) -> list[dict[str, Any]]:
    """The "Second Brain" presets of the rendered LibreChat config (only usable ones are
    rendered); empty when there is no rendered config yet."""
    try:
        config = yaml.safe_load((models_dir / "librechat.yaml").read_text(encoding="utf-8"))
        specs = config["modelSpecs"]["list"]
    except (OSError, yaml.YAMLError, KeyError, TypeError):
        return []
    return (
        [s for s in specs if isinstance(s, dict) and s.get("name")]
        if isinstance(specs, list)
        else []
    )


def default_preset(models_dir: Path) -> dict[str, Any] | None:
    """The preset new LibreChat chats open with (the default flag), else the first one."""
    specs = presets(models_dir)
    marked = [s for s in specs if s.get("softDefault") or s.get("default")]
    return marked[0] if marked else specs[0] if specs else None


def available_models(models_dir: Path) -> list[str]:
    """Every model LiteLLM serves, as LibreChat's model menu lists them: the aliases in the
    rendered litellm.yaml, then the providers' models in litellm-models.yaml."""
    names: list[str] = []
    for filename in ("litellm.yaml", "litellm-models.yaml"):
        try:
            data = yaml.safe_load((models_dir / filename).read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            continue
        entries = data.get("model_list") if isinstance(data, dict) else None
        for entry in entries if isinstance(entries, list) else []:
            name = entry.get("model_name") if isinstance(entry, dict) else None
            if isinstance(name, str) and name and name not in names:
                names.append(name)
    return names


def model_choices(models_dir: Path) -> tuple[list[tuple[str, str]], list[str]]:
    """What the owner can pick: the presets' models as (model, preset label), default first,
    then every other model LiteLLM serves."""
    specs = presets(models_dir)
    specs.sort(key=lambda s: not (s.get("softDefault") or s.get("default")))
    listed: list[tuple[str, str]] = []
    for spec in specs:
        model = str((spec.get("preset") or {}).get("model") or "")
        if model and model not in {m for m, _ in listed}:
            listed.append((model, str(spec.get("label") or spec["name"])))
    others = [m for m in available_models(models_dir) if m not in {m for m, _ in listed}]
    return listed, others


def default_model(models_dir: Path) -> str:
    preset = default_preset(models_dir)
    model = str(((preset or {}).get("preset") or {}).get("model") or "")
    return model or FALLBACK_MODEL


def connect(state_dir: Path) -> sqlite3.Connection:
    """A connection to assistant.sqlite (autocommit; callers serialise with their own lock)."""
    state_dir.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(state_dir / DB_FILE), check_same_thread=False, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA foreign_keys=ON")
    return db


class Prefs:
    """The owner's assistant settings (key/value)."""

    def __init__(self, state_dir: Path, models_dir: Path) -> None:
        self.db = connect(state_dir)
        self.db.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
        self.models_dir = models_dir
        self._lock = threading.Lock()

    def get(self, key: str) -> str | None:
        with self._lock:
            row = self.db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return None if row is None else str(row["value"])

    def set(self, key: str, value: str | None) -> None:
        with self._lock:
            if value is None:
                self.db.execute("DELETE FROM settings WHERE key = ?", (key,))
            else:
                self.db.execute(
                    "INSERT INTO settings (key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (key, value),
                )

    @property
    def model(self) -> str:
        """The model the portal's chat uses (ideas and dreams use the budget tiers)."""
        return self.get("model") or default_model(self.models_dir)
