"""Usage page: what the model calls cost, from LiteLLM's spend log.

LiteLLM appends one JSON line per successful call to USAGE_DIR/spend-YYYY-MM.jsonl
(config/litellm_spend_log.py): time, model, input and output tokens, and its own cost in
USD. The portal mounts that directory read-only and only sums it. Costs are shown in the
currency LibreChat uses (interface.currency in the rendered librechat.yaml), so the two
agree.
"""

from __future__ import annotations

import calendar
import datetime as dt
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

MONTH = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
_FILE = re.compile(r"^spend-(\d{4}-\d{2})\.jsonl$")
SYMBOLS = {"USD": "$", "EUR": "€", "GBP": "£", "JPY": "¥"}


@dataclass
class Totals:
    calls: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    cost: float = 0.0  # USD
    unpriced: int = 0  # calls whose model has no public price (counted as 0)

    def add(self, rec: dict[str, Any]) -> None:
        self.calls += 1
        self.tokens_in += rec["in"]
        self.tokens_out += rec["out"]
        if rec["cost"] is None:
            self.unpriced += 1
        else:
            self.cost += rec["cost"]


@dataclass(frozen=True)
class Currency:
    code: str = "USD"
    rate: float = 1.0  # units of `code` per 1 USD

    def money(self, usd: float) -> str:
        value = usd * self.rate
        prefix = SYMBOLS.get(self.code) or f"{self.code} "
        if 0 < value < 0.005:
            return f"< {prefix}0.01"
        return f"{prefix}{value:,.2f}"


@dataclass
class Report:
    currency: Currency
    current: str  # this month (YYYY-MM, UTC)
    month: str  # the month shown by model (YYYY-MM)
    this_month: Totals
    last_month: Totals
    all_time: Totals
    months: list[tuple[str, Totals]] = field(default_factory=list)  # newest first
    by_model: list[tuple[str, Totals]] = field(default_factory=list)  # most expensive first
    since: str | None = None  # first recorded day (YYYY-MM-DD)

    @property
    def empty(self) -> bool:
        return self.all_time.calls == 0

    @property
    def selected(self) -> Totals:
        return dict(self.months).get(self.month, Totals())


def month_label(key: str) -> str:
    year, month = key.split("-")
    return f"{calendar.month_name[int(month)]} {year}"


def _previous(key: str) -> str:
    year, month = (int(p) for p in key.split("-"))
    return f"{year - 1}-12" if month == 1 else f"{year}-{month - 1:02d}"


def _parse(line: str) -> dict[str, Any] | None:
    try:
        raw = json.loads(line)
        cost = raw.get("cost")
        return {
            "ts": str(raw["ts"]),
            "model": str(raw.get("model") or "unknown"),
            "in": int(raw.get("in") or 0),
            "out": int(raw.get("out") or 0),
            "cost": float(cost) if isinstance(cost, int | float) else None,
        }
    except (ValueError, KeyError, TypeError, AttributeError):
        return None  # a torn or foreign line: skip it


class UsageLog:
    def __init__(self, log_dir: Path, models_dir: Path) -> None:
        self.log_dir = log_dir
        self.models_dir = models_dir
        # file name -> ((mtime_ns, size), records); a month's file only grows
        self._cache: dict[str, tuple[tuple[int, int], list[dict[str, Any]]]] = {}

    def records(self) -> dict[str, list[dict[str, Any]]]:
        """Records per month (YYYY-MM), read from disk only when a file changed."""
        out: dict[str, list[dict[str, Any]]] = {}
        try:
            files = sorted(p for p in self.log_dir.iterdir() if _FILE.match(p.name))
        except OSError:
            return out
        for path in files:
            try:
                st = path.stat()
                stamp = (st.st_mtime_ns, st.st_size)
                cached = self._cache.get(path.name)
                if cached is None or cached[0] != stamp:
                    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
                    cached = (stamp, [r for r in map(_parse, lines) if r is not None])
                    self._cache[path.name] = cached
            except OSError:
                continue
            out[_FILE.match(path.name).group(1)] = cached[1]  # type: ignore[union-attr]
        return out

    def currency(self) -> Currency:
        """LibreChat's display currency, so the portal and the chat show the same amounts."""
        try:
            lc = yaml.safe_load((self.models_dir / "librechat.yaml").read_text()) or {}
            cur = (lc.get("interface") or {}).get("currency") or {}
            code, rate = str(cur["code"]).upper(), float(cur["rate"])
            if re.fullmatch(r"[A-Z]{3}", code) and rate > 0:
                return Currency(code, rate)
        except (OSError, yaml.YAMLError, KeyError, TypeError, ValueError, AttributeError):
            pass
        return Currency()

    def report(self, month: str | None = None, today: dt.date | None = None) -> Report:
        today = today or dt.datetime.now(dt.UTC).date()
        current = today.strftime("%Y-%m")
        by_month = self.records()
        months: list[tuple[str, Totals]] = []
        all_time = Totals()
        for key in sorted(by_month, reverse=True):
            totals = Totals()
            for rec in by_month[key]:
                totals.add(rec)
                all_time.add(rec)
            if totals.calls:
                months.append((key, totals))
        per_month = dict(months)
        chosen = month if month and MONTH.match(month) else current
        models: dict[str, Totals] = {}
        for rec in by_month.get(chosen, []):
            models.setdefault(rec["model"], Totals()).add(rec)
        first = [r["ts"][:10] for key in sorted(by_month) for r in by_month[key][:1]]
        return Report(
            currency=self.currency(),
            current=current,
            month=chosen,
            this_month=per_month.get(current, Totals()),
            last_month=per_month.get(_previous(current), Totals()),
            all_time=all_time,
            months=months,
            by_model=sorted(models.items(), key=lambda kv: (-kv[1].cost, -kv[1].calls, kv[0])),
            since=first[0] if first else None,
        )
