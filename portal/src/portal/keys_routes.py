"""API keys per provider.

  GET  /keys                     status per provider (masked hint only)
  POST /keys/{provider}/save     add or replace: validated with the provider first
  POST /keys/{provider}/test     test the stored key again
  POST /keys/{provider}/remove   remove; the provider's models and presets (or search features) disappear
  POST /keys/apply               refresh the model list (like `make models`)

Changing keys needs the password entered within the last few minutes (re-auth). Every
change is audited with the provider and a short fingerprint, never the key.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from starlette.datastructures import FormData
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response
from starlette.routing import Route

from portal.apply import Applier
from portal.keys import (
    CheckResult,
    KeyFormatError,
    KeyStore,
    Provider,
    check_key,
    clean_key,
    fingerprint,
)
from portal.sessions import Session
from portal.web import Portal, owner

Tester = Callable[[Provider, str], Awaitable[CheckResult]]
RESTART_NOTE = "LiteLLM and LibreChat reload in about 20 seconds."


class KeyRoutes:
    def __init__(
        self,
        portal: Portal,
        store: KeyStore,
        providers: dict[str, Provider],
        applier: Applier,
        *,
        tester: Tester = check_key,
    ) -> None:
        self.p = portal
        self.store = store
        self.providers = providers
        self.applier = applier
        self.tester = tester

    def routes(self) -> list[Route]:
        o = owner(self.p)
        sensitive = owner(self.p, reauth=True)
        return [
            Route("/keys", o(self.page)),
            Route("/keys/apply", sensitive(self.apply), methods=["POST"]),
            Route("/keys/{provider}/save", sensitive(self.save), methods=["POST"]),
            Route("/keys/{provider}/test", o(self.test), methods=["POST"]),
            Route("/keys/{provider}/remove", sensitive(self.remove), methods=["POST"]),
        ]

    @staticmethod
    def _back() -> Response:
        return RedirectResponse("/keys", status_code=303)

    def _provider(self, request: Request) -> Provider | None:
        return self.providers.get(request.path_params["provider"])

    def _audit(
        self, request: Request, kind: str, provider: Provider, outcome: str, **data: object
    ) -> None:
        self.p.audit.event(
            kind,
            client="portal",
            ip=self.p.ip(request),
            outcome=outcome,
            provider=provider.name,
            **data,
        )

    async def _apply(self, request: Request, session: Session, reason: str) -> None:
        result = await self.applier.apply()
        self.p.audit.event(
            "models_refresh",
            client="portal",
            ip=self.p.ip(request),
            outcome="ok" if result.ok else "failed",
            reason=reason,
        )
        if result.ok:
            session.notify(f"Model list refreshed. {RESTART_NOTE}")
        else:
            session.notify(result.output, ok=False)

    # ------------------------------------------------------------------ handlers

    async def page(self, request: Request, session: Session, _: FormData | None) -> Response:
        return self.p.render(
            request,
            "keys.html",
            session,
            rows=self.store.status(self.providers),
            can_change=session.recently_authenticated(self.p.settings.reauth_window_s),
            last=self.applier.last,
        )

    async def save(self, request: Request, session: Session, form: FormData | None) -> Response:
        provider = self._provider(request)
        if provider is None or form is None:
            return self._back()
        try:
            key = clean_key(str(form.get("key", "")))
        except KeyFormatError as exc:
            session.notify(f"{provider.label}: {exc}", ok=False)
            return self._back()
        result = await self.tester(provider, key)
        if not result.ok:
            self._audit(
                request,
                "key_test",
                provider,
                "rejected",
                fingerprint=fingerprint(key),
                detail=result.detail,
            )
            session.notify(f"{provider.label} key not saved: {result.detail}.", ok=False)
            return self._back()
        replaced = self.store.save(provider, key, detail=result.detail)
        action = "replaced" if replaced else "added"
        self._audit(
            request,
            "key_replace" if replaced else "key_add",
            provider,
            "ok",
            fingerprint=fingerprint(key),
        )
        session.notify(f"{provider.label} key {action} ({result.detail}).")
        await self._apply(request, session, f"{provider.name} key {action}")
        return self._back()

    async def test(self, request: Request, session: Session, _: FormData | None) -> Response:
        provider = self._provider(request)
        if provider is None:
            return self._back()
        key = self.store.get(provider)
        if key is None:
            session.notify(f"No {provider.label} key to test.", ok=False)
            return self._back()
        result = await self.tester(provider, key)
        self.store.record_test(provider, result.ok, result.detail)
        self._audit(
            request,
            "key_test",
            provider,
            "ok" if result.ok else "rejected",
            fingerprint=fingerprint(key),
            detail=result.detail,
        )
        session.notify(f"{provider.label}: {result.detail}.", ok=result.ok)
        return self._back()

    async def remove(self, request: Request, session: Session, _: FormData | None) -> Response:
        provider = self._provider(request)
        if provider is None:
            return self._back()
        fp = self.store.remove(provider)
        if fp is None:
            session.notify(f"No {provider.label} key was stored.", ok=False)
            return self._back()
        self._audit(request, "key_remove", provider, "ok", fingerprint=fp)
        hidden = (
            "Its models and presets are hidden"
            if provider.kind == "model"
            else "Its search features are off"
        )
        session.notify(f"{provider.label} key removed. {hidden}.")
        await self._apply(request, session, f"{provider.name} key removed")
        return self._back()

    async def apply(self, request: Request, session: Session, _: FormData | None) -> Response:
        await self._apply(request, session, "manual refresh")
        return self._back()
