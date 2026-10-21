"""Apply key changes without Docker access (the same steps as `make models`):

1. run scripts/refresh_models.py (mounted read-only) against the keys file: it lists the
   models each key can use and renders LiteLLM's and LibreChat's configs, hiding the
   aliases and presets of providers without a key;
2. write the reload stamp; the reload supervisor in the LiteLLM and LibreChat containers
   sees it and restarts the service in place (it reads the new keys and configs).

The child process gets no key in its environment; it reads the keys file itself.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ApplyResult:
    ok: bool
    output: str
    at: float


class Applier:
    def __init__(
        self,
        *,
        script: Path,
        models_dir: Path,
        config_dir: Path,
        keys_file: Path,
        currency: str = "USD",
        timeout: float = 180.0,
        extra_env: dict[str, str] | None = None,
    ) -> None:
        self.script = script
        self.models_dir = models_dir
        self.config_dir = config_dir
        self.keys_file = keys_file
        self.currency = currency
        self.timeout = timeout
        self.extra_env = extra_env or {}
        self.last: ApplyResult | None = None
        self._lock = asyncio.Lock()

    @property
    def stamp(self) -> Path:
        return self.models_dir / "reload.stamp"

    async def apply(self) -> ApplyResult:
        async with self._lock:  # one refresh at a time
            result = await self._run()
            if result.ok:
                self._touch_stamp()
            self.last = result
            return result

    async def _run(self) -> ApplyResult:
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": tempfile.gettempdir(),
            "COST_CURRENCY": self.currency,
            **self.extra_env,
        }
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            str(self.script),
            str(self.models_dir),
            str(self.config_dir),
            "--keys-file",
            str(self.keys_file),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=env,
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), self.timeout)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            return ApplyResult(False, "model refresh timed out; nothing was changed", time.time())
        text = out.decode(errors="replace")[-4000:]
        if proc.returncode != 0:
            # Only the last line: a traceback could quote file content.
            last = text.strip().splitlines()[-1:] or ["(no output)"]
            return ApplyResult(False, f"model refresh failed: {last[0][:300]}", time.time())
        return ApplyResult(True, text.strip(), time.time())

    def _touch_stamp(self) -> None:
        tmp = self.stamp.with_name(".reload.stamp.tmp")
        tmp.write_text(f"{time.time_ns()}\n")
        tmp.chmod(0o644)
        tmp.replace(self.stamp)
