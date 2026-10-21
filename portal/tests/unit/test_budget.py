"""The model tiers and the budget guard (portal.dreams.budget)."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

from portal.dreams.budget import Budget, ModelAmbiguous, resolve, resolve_first
from portal.processing.llm import BudgetRefused
from tests.conftest import make_budget, priced_models

NOW = dt.datetime(2026, 9, 28, 12, 0, tzinfo=dt.UTC)
GLM_IN, GLM_OUT = 1.4e-06, 4.4e-06  # USD per token (tests/conftest.py PRICES)


def budget_at(tmp_path: Path, **overrides: object) -> Budget:
    models = priced_models(tmp_path / "models")
    return make_budget(tmp_path / "state", models, clock=lambda: NOW, **overrides)


def spend(tmp_path: Path, month: str, usd: float) -> None:
    logs = tmp_path / "usage"
    logs.mkdir(exist_ok=True)
    line = {"ts": f"{month}-02T10:00:00Z", "model": "glm-5-3", "in": 1, "out": 1, "cost": usd}
    with (logs / f"spend-{month}.jsonl").open("a") as f:
        f.write(json.dumps(line) + "\n")


# --- tiers -------------------------------------------------------------------------


def test_resolve_exact_then_suffix() -> None:
    served = ["glm-5-3", "mistral/glm-5-2", "mistral/mistral-large-latest"]
    assert resolve("glm-5-3", served) == "glm-5-3"
    assert resolve("glm-5-2", served) == "mistral/glm-5-2"  # a provider prefix
    assert resolve("mistral-large", served) is None  # a suffix only counts after a "/"
    assert resolve("claude-opus-5-5", served) is None


def test_an_ambiguous_name_is_an_error() -> None:
    served = ["mistral/glm-5-2", "together/glm-5-2"]
    with pytest.raises(ModelAmbiguous, match="mistral/glm-5-2, together/glm-5-2"):
        resolve("glm-5-2", served)
    with pytest.raises(ModelAmbiguous):
        resolve_first(["claude-opus-5-5", "glm-5-2"], served)


def test_strong_tier_is_the_first_served_and_falls_back_to_cheap(tmp_path: Path) -> None:
    budget = budget_at(tmp_path)
    assert budget.strong_model() == "glm-5-3"  # neither Claude nor GPT is served
    assert budget.cheap_model() == "mistral-large"
    assert budget.tier("strong") == ["glm-5-3", "mistral-large"]
    assert budget.tier("cheap") == ["mistral-large"]


# --- estimate and caps --------------------------------------------------------------


def test_estimate_is_input_chars_over_four_plus_max_output(tmp_path: Path) -> None:
    budget = budget_at(tmp_path)
    usd = 4000 / 4 * GLM_IN + 1000 * GLM_OUT
    assert budget.estimate("glm-5-3", 4000, 1000) == pytest.approx(usd * 0.9)
    assert budget.refusal("glm-5-3", 4000, 1000) is None


def test_refused_at_the_total_cap(tmp_path: Path) -> None:
    budget = budget_at(tmp_path)
    spend(tmp_path, "2026-09", 5.55)  # EUR 4.995 of the EUR 5 this month, chats included
    spend(tmp_path, "2026-08", 100.0)  # last month does not count
    reason = budget.refusal("glm-5-3", 40_000, 4000)
    assert reason is not None and "BUDGET_TOTAL_EUR" in reason
    assert budget.refusal("glm-5-3", 400, 100) is None  # a small call still fits


def test_refused_at_the_automation_cap(tmp_path: Path) -> None:
    budget = budget_at(tmp_path, automation_eur=0.01)
    budget.record("ideas", 1, "generate", "glm-5-3", 5000, 1000)  # about EUR 0.0103
    assert budget.month_automation_eur() == pytest.approx((5000 * GLM_IN + 1000 * GLM_OUT) * 0.9)
    reason = budget.refusal("glm-5-3", 400, 100)
    assert reason is not None and "BUDGET_AUTOMATION_EUR" in reason
    assert budget.month_total_eur() == 0  # the spend log is empty: only the second cap applies


def test_strong_refused_falls_back_to_cheap_then_skips(tmp_path: Path) -> None:
    budget = budget_at(tmp_path, automation_eur=0.02)
    # 60k characters and 4k out: about EUR 0.035 with GLM, EUR 0.012 with Mistral Large
    choice = budget.choose(budget.tier("strong"), 60_000, 4000)
    assert choice.model == "mistral-large"
    assert len(choice.notes) == 1 and "BUDGET_AUTOMATION_EUR" in choice.notes[0]

    budget.automation_eur = 0.001
    skipped = budget.choose(budget.tier("strong"), 60_000, 4000)
    assert skipped.model is None and len(skipped.notes) == 2


def test_an_unpriced_model_is_never_used(tmp_path: Path) -> None:
    budget = budget_at(tmp_path)
    assert budget.price("mistral/codestral-latest") is None
    reason = budget.refusal("mistral/codestral-latest", 10, 10)
    assert reason is not None and "no price" in reason
    assert budget.estimate("mistral/codestral-latest", 10, 10) is None


def test_eur_from_the_ecb_rates_when_the_display_currency_is_not_eur(tmp_path: Path) -> None:
    budget = budget_at(tmp_path)
    (tmp_path / "models" / "librechat.yaml").write_text("interface: {}\n")  # shown in USD
    assert budget.eur_per_usd() is None  # no rate at all: nothing can be estimated
    assert budget.refusal("glm-5-3", 10, 10) == "no EUR exchange rate to estimate the cost with"
    (tmp_path / "models" / "ecb-rates.xml").write_text(
        "<Cube currency='USD' rate='1.25'/><Cube currency='GBP' rate='0.85'/>"
    )
    assert budget.eur_per_usd() == pytest.approx(0.8)


def test_meter_checks_before_and_records_after(tmp_path: Path) -> None:
    budget = budget_at(tmp_path)
    meter = budget.meter("dream", 7, "deduction")
    meter.before("glm-5-3", 1000, 500)
    meter.after("glm-5-3", 250, 500)
    [call] = budget.calls("dream", 7)
    assert (call.pass_, call.model, call.input_tokens, call.output_tokens) == (
        "deduction", "glm-5-3", 250, 500,
    )  # fmt: skip
    assert call.cost_eur == pytest.approx(meter.cost_eur) and call.ts.startswith("2026-09-28")
    with pytest.raises(BudgetRefused):
        meter.before("mistral/codestral-latest", 10, 10)
    assert budget.calls("ideas") == []
