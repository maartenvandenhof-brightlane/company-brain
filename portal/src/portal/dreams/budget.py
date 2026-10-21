"""The model tiers and the budget guard for every call the portal makes by itself: dream
passes, ideas and the taste note.

  tiers     MODEL_CHEAP, and MODEL_STRONG as a preference list, resolved against the models
            LiteLLM serves (assistant.available_models): the exact name first, then a served
            name ending in /<name> (a provider prefix). A name matching several is an error.
  prices    model-prices.json (LiteLLM's public price list, USD per token), looked up through
            the served model's litellm_params.model the way scripts/refresh_models.py does. A
            model without a price is never used automatically.
  spend     month to date (UTC calendar month), in EUR: all LiteLLM spend from the spend log
            (usage.py), and the automated calls recorded in `automation_calls`.
  guard     before each call: input characters / 4 x input price, plus the call's max output
            tokens x output price. The call is refused when it would take either total over
            its cap. A refused strong-tier call is estimated again with the cheap model; when
            that is refused too, the pass is skipped.

USD becomes EUR at the rate the Usage page uses (LibreChat's currency, which
refresh_models.py takes from the ECB's reference rates); when that currency is not EUR, the
ECB file itself. Without a rate nothing can be estimated, so nothing is called.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from portal.assistant import available_models, connect
from portal.processing.llm import BudgetRefused
from portal.usage import UsageLog

CHARS_PER_TOKEN = 4
ESTIMATE_OUTPUT_TOKENS = 4096  # a call that sets no max_tokens is estimated with this
PRICES_FILE = "model-prices.json"
ECB_FILE = "ecb-rates.xml"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS automation_calls (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    run_kind TEXT NOT NULL,
    run_id INTEGER,
    pass TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cost_eur REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS automation_calls_ts ON automation_calls(ts);
"""
_ECB_USD = re.compile(r"currency=['\"]USD['\"]\s+rate=['\"]([0-9.]+)['\"]")


class ModelAmbiguous(ValueError):
    """A configured model name matches several served models."""


def resolve(name: str, served: list[str]) -> str | None:
    """The served model for a configured name: the exact name, else the one served name that
    ends in /<name>. None when nothing matches; ModelAmbiguous when several do."""
    if name in served:
        return name
    hits = [m for m in served if m.endswith("/" + name)]
    if len(hits) > 1:
        raise ModelAmbiguous(f"{name} matches several served models: {', '.join(hits)}")
    return hits[0] if hits else None


def resolve_first(names: list[str], served: list[str]) -> str | None:
    """The first name of a preference list that LiteLLM serves."""
    for name in names:
        if (model := resolve(name, served)) is not None:
            return model
    return None


def routes(models_dir: Path) -> dict[str, str]:
    """Served name -> the provider model LiteLLM routes it to (litellm_params.model)."""
    out: dict[str, str] = {}
    for filename in ("litellm.yaml", "litellm-models.yaml"):
        try:
            data = yaml.safe_load((models_dir / filename).read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            continue
        entries = data.get("model_list") if isinstance(data, dict) else None
        for entry in entries if isinstance(entries, list) else []:
            if not isinstance(entry, dict) or not isinstance(entry.get("model_name"), str):
                continue
            params = entry.get("litellm_params")
            routed = params.get("model") if isinstance(params, dict) else None
            if isinstance(routed, str) and routed:
                out.setdefault(entry["model_name"], routed)
    return out


@dataclass(frozen=True)
class Price:
    input: float  # USD per token
    output: float


@dataclass
class Call:
    ts: str
    run_kind: str
    run_id: int | None
    pass_: str
    model: str
    input_tokens: int
    output_tokens: int
    cost_eur: float


@dataclass
class Choice:
    """The model a pass may use, or None; `notes` say what was refused and why."""

    model: str | None
    notes: list[str] = field(default_factory=list)


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


class Budget:
    def __init__(
        self,
        state_dir: Path,
        models_dir: Path,
        usage: UsageLog,
        *,
        total_eur: float,
        automation_eur: float,
        cheap: str,
        strong: list[str],
        clock: Callable[[], dt.datetime] = _now,
    ) -> None:
        self.db = connect(state_dir)
        self.db.executescript(_SCHEMA)
        self.models_dir = models_dir
        self.usage = usage
        self.total_eur = total_eur
        self.automation_eur = automation_eur
        self.cheap = cheap
        self.strong = strong
        self.clock = clock
        self._lock = threading.Lock()
        self._prices: tuple[tuple[int, int], dict[str, Any]] | None = None

    # --- tiers -------------------------------------------------------------------

    def cheap_model(self) -> str | None:
        return resolve(self.cheap, available_models(self.models_dir))

    def strong_model(self) -> str | None:
        return resolve_first(self.strong, available_models(self.models_dir))

    def tier(self, name: str) -> list[str]:
        """The models to try for a tier, in order: strong falls back to cheap."""
        models = [self.strong_model()] if name == "strong" else []
        models.append(self.cheap_model())
        return list(dict.fromkeys(m for m in models if m))

    # --- prices and currency -----------------------------------------------------

    def _price_list(self) -> dict[str, Any]:
        path = self.models_dir / PRICES_FILE
        try:
            st = path.stat()
            stamp = (st.st_mtime_ns, st.st_size)
            if self._prices is None or self._prices[0] != stamp:
                data = json.loads(path.read_text(encoding="utf-8"))
                self._prices = (stamp, data if isinstance(data, dict) else {})
        except (OSError, ValueError):
            return {}
        return self._prices[1]

    def price(self, model: str) -> Price | None:
        prices = self._price_list()
        routed = routes(self.models_dir).get(model, model)
        info = prices.get(routed) or prices.get(routed.split("/", 1)[-1]) or prices.get(model)
        if not isinstance(info, dict):
            return None
        cost_in, cost_out = info.get("input_cost_per_token"), info.get("output_cost_per_token")
        if not isinstance(cost_in, int | float) or not isinstance(cost_out, int | float | None):
            return None
        return Price(float(cost_in), float(cost_out or 0))

    def eur_per_usd(self) -> float | None:
        currency = self.usage.currency()
        if currency.code == "EUR":
            return currency.rate
        try:
            found = _ECB_USD.search((self.models_dir / ECB_FILE).read_text(encoding="utf-8"))
        except OSError:
            return None
        usd_per_eur = float(found.group(1)) if found else 0.0
        return 1 / usd_per_eur if usd_per_eur > 0 else None

    # --- spend ---------------------------------------------------------------------

    def _month(self) -> str:
        return self.clock().strftime("%Y-%m")

    def month_total_eur(self) -> float:
        """All LiteLLM spend this month (unpriced calls count as nothing)."""
        rate = self.eur_per_usd() or 0.0
        records = self.usage.records().get(self._month(), [])
        return sum(r["cost"] or 0.0 for r in records) * rate

    def month_automation_eur(self) -> float:
        with self._lock:
            row = self.db.execute(
                "SELECT COALESCE(SUM(cost_eur), 0) AS eur FROM automation_calls WHERE ts LIKE ?",
                (self._month() + "-%",),
            ).fetchone()
        return float(row["eur"])

    def estimate(self, model: str, chars: int, max_tokens: int | None) -> float | None:
        """EUR for one call at most, or None when the model has no price or there is no rate."""
        price, rate = self.price(model), self.eur_per_usd()
        if price is None or rate is None:
            return None
        tokens_out = ESTIMATE_OUTPUT_TOKENS if max_tokens is None else max_tokens
        return (chars / CHARS_PER_TOKEN * price.input + tokens_out * price.output) * rate

    def refusal(self, model: str, chars: int, max_tokens: int | None) -> str | None:
        """Why this call may not be made, or None when it may."""
        if self.price(model) is None:
            return f"{model} has no price in {PRICES_FILE}"
        cost = self.estimate(model, chars, max_tokens)
        if cost is None:
            return "no EUR exchange rate to estimate the cost with"
        total, automated = self.month_total_eur(), self.month_automation_eur()
        if total + cost > self.total_eur:
            return (
                f"budget: EUR {total:.2f} spent this month + EUR {cost:.3f} for this call "
                f"would exceed BUDGET_TOTAL_EUR ({self.total_eur:.2f})"
            )
        if automated + cost > self.automation_eur:
            return (
                f"budget: EUR {automated:.2f} automated spend this month + EUR {cost:.3f} "
                f"for this call would exceed BUDGET_AUTOMATION_EUR ({self.automation_eur:.2f})"
            )
        return None

    def choose(self, models: list[str], chars: int, max_tokens: int | None) -> Choice:
        """The first of `models` whose call the guard allows."""
        choice = Choice(None)
        for model in models:
            reason = self.refusal(model, chars, max_tokens)
            if reason is None:
                choice.model = model
                return choice
            choice.notes.append(reason)
        if not models:
            choice.notes.append("no configured model is served")
        return choice

    # --- recording -------------------------------------------------------------------

    def record(
        self,
        run_kind: str,
        run_id: int | None,
        pass_: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
    ) -> float:
        """Record one automated call; its cost in EUR."""
        price, rate = self.price(model), self.eur_per_usd()
        cost = (
            (input_tokens * price.input + output_tokens * price.output) * rate
            if price is not None and rate is not None
            else 0.0
        )
        ts = self.clock().strftime("%Y-%m-%dT%H:%M:%SZ")
        with self._lock:
            self.db.execute(
                "INSERT INTO automation_calls (ts, run_kind, run_id, pass, model, input_tokens, "
                "output_tokens, cost_eur) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (ts, run_kind, run_id, pass_, model, input_tokens, output_tokens, cost),
            )
        return cost

    def calls(self, run_kind: str | None = None, run_id: int | None = None) -> list[Call]:
        sql, params = "SELECT * FROM automation_calls WHERE 1=1", list[Any]()
        if run_kind is not None:
            sql += " AND run_kind = ?"
            params.append(run_kind)
        if run_id is not None:
            sql += " AND run_id = ?"
            params.append(run_id)
        with self._lock:
            rows = self.db.execute(sql + " ORDER BY id", params).fetchall()
        return [
            Call(
                r["ts"], r["run_kind"], r["run_id"], r["pass"], r["model"],
                r["input_tokens"], r["output_tokens"], r["cost_eur"],
            )
            for r in rows
        ]  # fmt: skip

    def meter(self, run_kind: str, run_id: int | None, pass_: str = "") -> CallMeter:
        return CallMeter(self, run_kind, run_id, pass_)


@dataclass
class CallMeter:
    """The guard and the record for one run's calls (processing.llm.Meter). Set `pass_` as
    the run moves from pass to pass."""

    budget: Budget
    run_kind: str
    run_id: int | None
    pass_: str = ""
    cost_eur: float = 0.0

    def before(self, model: str, chars: int, max_tokens: int | None) -> None:
        reason = self.budget.refusal(model, chars, max_tokens)
        if reason is not None:
            raise BudgetRefused(reason)

    def after(self, model: str, input_tokens: int, output_tokens: int) -> None:
        self.cost_eur += self.budget.record(
            self.run_kind, self.run_id, self.pass_, model, input_tokens, output_tokens
        )
