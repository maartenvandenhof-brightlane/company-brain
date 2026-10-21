"""The saved mode (config.yaml), wizard prefill, generated secrets and the adapter supervisor."""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from wiki_mcp.auth.passwords import hash_password
from wiki_mcp.brain import (
    ADMIN_HASH,
    INTERNAL_TOKEN,
    BrainConfig,
    DriveSettings,
    McpSettings,
    Mode,
    ensure_token,
    load_config,
    owner_hash,
    prefill,
    save_config,
    write_secret,
)
from wiki_mcp.config import Settings
from wiki_mcp.supervisor import Supervisor, plan, wanted


def test_config_round_trip_and_validation(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    assert load_config(path) is None  # setup state
    config = BrainConfig(
        Mode.BOTH, gdrive=DriveSettings("Brain", "keep", "[kb]", "Inbox", 10, "a@b.test", True)
    )
    save_config(path, config)
    assert load_config(path) == config
    assert BrainConfig.from_dict({"mode": "gdrive"}).gdrive == DriveSettings()
    for bad in (
        {"mode": "email"},
        {"mode": "mcp", "mcp": {"tunnel": "cloudflare"}},
        {"mode": "mcp", "mcp": {"hostname": "-brain"}},
        {"mode": "mcp", "mcp": {"hostname": "brain.example.com"}},
        {"mode": "gdrive", "gdrive": {"inbox": "gmail"}},
        {"mode": "gdrive", "gdrive": {"folder": ""}},
        {"mode": "gdrive", "gdrive": {"prefix": "a\nb"}},
        {"mode": "gdrive", "gdrive": {"poll_minutes": 0}},
        {"mode": "gdrive", "gdrive": {"poll_minutes": "soon"}},
        {"mode": "gdrive", "gdrive": {"keep_user": "not an email"}},
        ["mode", "mcp"],
    ):
        with pytest.raises(ValueError):
            BrainConfig.from_dict(bad)
    path.write_text("mode: [\n")
    with pytest.raises(ValueError, match="setup wizard"):
        load_config(path)  # a broken file fails loudly instead of resetting to setup


def test_env_only_prefills_the_wizard(tmp_path: Path) -> None:
    env = Settings(data_dir=tmp_path, brain_mode="gdrive", inbox_adapter="keep")
    assert prefill(None, env) == BrainConfig(Mode.GDRIVE, gdrive=DriveSettings(inbox="keep"))
    saved = BrainConfig(Mode.MCP)
    assert prefill(saved, env) == saved  # the saved config wins
    assert prefill(None, Settings(brain_mode="nonsense")) == BrainConfig(Mode.MCP)
    assert prefill(None, Settings()).mode is Mode.MCP
    keep = Settings(brain_mode="gdrive", keep_delete_after_ingest=True)
    assert prefill(None, keep).gdrive == DriveSettings(keep_delete=True)


def test_tokens_are_generated_once_and_private(tmp_path: Path) -> None:
    secrets_dir = tmp_path / "secrets"
    token = ensure_token(secrets_dir, INTERNAL_TOKEN)
    assert len(token) >= 32 and ensure_token(secrets_dir, INTERNAL_TOKEN) == token
    assert stat.S_IMODE((secrets_dir / INTERNAL_TOKEN).stat().st_mode) == 0o600
    assert stat.S_IMODE(secrets_dir.stat().st_mode) == 0o700


def test_owner_hash_prefers_the_wizards_password(tmp_path: Path) -> None:
    from_env = hash_password("from the environment")
    settings = Settings(data_dir=tmp_path, owner_password_hash=from_env)
    assert owner_hash(settings) == from_env
    write_secret(settings.secrets_dir, ADMIN_HASH, "$argon2id$wizard")
    assert owner_hash(settings) == "$argon2id$wizard"
    assert owner_hash(Settings(data_dir=tmp_path / "none")) == ""


class FakeAdapter:
    def __init__(self, name: str, modes: set[Mode], section: str, log: list[str]) -> None:
        self.name = name
        self.modes = frozenset(modes)
        self.section = section
        self.log = log

    async def start(self, config: BrainConfig) -> None:
        self.log.append(f"start {self.name}")

    async def stop(self) -> None:
        self.log.append(f"stop {self.name}")


def _adapters(log: list[str]) -> list[FakeAdapter]:
    return [
        FakeAdapter("mcp", {Mode.MCP, Mode.BOTH}, "mcp", log),
        FakeAdapter("drive", {Mode.GDRIVE, Mode.BOTH}, "gdrive", log),
        FakeAdapter("inbox", {Mode.GDRIVE, Mode.BOTH}, "gdrive", log),
    ]


def test_mode_resolution() -> None:
    adapters = _adapters([])
    assert wanted(None, adapters) == {}
    assert set(wanted(BrainConfig(Mode.MCP), adapters)) == {"mcp"}
    assert set(wanted(BrainConfig(Mode.GDRIVE), adapters)) == {"drive", "inbox"}
    assert set(wanted(BrainConfig(Mode.BOTH), adapters)) == {"mcp", "drive", "inbox"}
    assert wanted(BrainConfig(Mode.MCP), adapters)["mcp"] == McpSettings()


def test_plan_restarts_only_what_changed() -> None:
    a, b = DriveSettings(), DriveSettings(folder="Other")
    assert plan({}, {"mcp": 1}) == ([], ["mcp"])
    assert plan({"mcp": 1, "drive": a}, {"mcp": 1, "drive": a}) == ([], [])
    assert plan({"mcp": 1, "drive": a}, {"mcp": 1, "drive": b}) == (["drive"], ["drive"])
    assert plan({"mcp": 1, "drive": a}, {"drive": a}) == (["mcp"], [])


async def test_supervisor_follows_the_saved_mode() -> None:
    log: list[str] = []
    sup = Supervisor(_adapters(log))
    await sup.apply(None)
    assert log == [] and sup.config is None
    await sup.apply(BrainConfig(Mode.MCP))
    assert log == ["start mcp"] and sup.status() == {"mcp": True, "drive": False, "inbox": False}
    log.clear()
    await sup.apply(BrainConfig(Mode.BOTH))  # mcp keeps running untouched
    assert log == ["start drive", "start inbox"]
    log.clear()
    await sup.apply(BrainConfig(Mode.BOTH, gdrive=DriveSettings(folder="New")))
    assert sorted(log) == ["start drive", "start inbox", "stop drive", "stop inbox"]
    log.clear()
    await sup.apply(BrainConfig(Mode.GDRIVE, gdrive=DriveSettings(folder="New")))
    assert log == ["stop mcp"]
    log.clear()
    await sup.close()
    assert sorted(log) == ["stop drive", "stop inbox"] and not any(sup.status().values())


async def test_a_failing_adapter_does_not_stop_the_others() -> None:
    log: list[str] = []
    broken, *rest = _adapters(log)

    async def boom(config: BrainConfig) -> None:
        raise RuntimeError("no network")

    broken.start = boom  # type: ignore[method-assign]
    sup = Supervisor([broken, *rest])
    await sup.apply(BrainConfig(Mode.BOTH))
    assert sup.status() == {"mcp": False, "drive": True, "inbox": True}
