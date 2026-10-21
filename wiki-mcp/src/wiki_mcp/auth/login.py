"""Owner login + consent page for the OAuth authorization flow.

GET  /login?request=<id>   render the form for a pending authorization
POST /login                verify credentials (+TOTP), then redirect to the client
                           with ?code=..&state=..&iss=.. (RFC 9207), or access_denied
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
from dataclasses import dataclass
from html import escape
from urllib.parse import urlsplit

from mcp.server.auth.provider import construct_redirect_uri
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from wiki_mcp.audit import AuditLog
from wiki_mcp.auth.passwords import verify_owner
from wiki_mcp.auth.provider import SCOPES, OAuthProvider, PendingAuthorization, valid_request_id
from wiki_mcp.auth.ratelimit import Lockout

CSRF_COOKIE = "sb_login_csrf"
SCOPE_TEXT = {
    "wiki:read": "Read and search your wiki",
    "wiki:write": "Create and update pages (every change is a git commit)",
    "wiki:admin": "Accept or reject proposals",
    "wiki:private": (
        "Private pages: personal life, relationships, feelings. "
        "Only grant to assistants you trust with this."
    ),
}
PRIVATE_SCOPE = "wiki:private"  # opt-in: unchecked until the owner ticks it
EXPIRED = "This sign-in link has expired. Start the connection again from your app."


# "Show password" toggle and submit-once guard. The only script on these pages; the CSP allows
# exactly this text (by hash), nothing else. Without JavaScript the button stays hidden. The
# field is hidden again before submitting, so browsers never store the password as ordinary
# form history. A form submits once: a password manager's auto-submit plus Enter (or a double
# click) would otherwise send a second POST that cancels the redirect carrying the code. The
# guard is a flag, not disabled buttons, so the first submit still sends the clicked button.
REVEAL_SCRIPT = """
document.querySelectorAll("button[data-reveal]").forEach(function (b) {
  var input = document.getElementById(b.getAttribute("data-reveal"));
  if (!input) return;
  b.hidden = false;
  b.addEventListener("click", function () {
    var show = input.type === "password";
    input.type = show ? "text" : "password";
    b.setAttribute("aria-pressed", show ? "true" : "false");
    b.setAttribute("aria-label", show ? "Hide password" : "Show password");
    input.focus();
  });
  if (input.form) input.form.addEventListener("submit", function () { input.type = "password"; });
});
document.querySelectorAll("form").forEach(function (f) {
  var sent = false;
  f.addEventListener("submit", function (e) {
    if (sent) e.preventDefault();
    sent = true;
  });
  window.addEventListener("pageshow", function () { sent = false; });
});
"""
REVEAL_HASH = "sha256-" + base64.b64encode(hashlib.sha256(REVEAL_SCRIPT.encode()).digest()).decode()
EYE_ICON = (
    '<svg viewBox="0 0 24 24" width="20" height="20" aria-hidden="true" focusable="false">'
    '<path d="M1 12s4-7 11-7 11 7 11 7-4 7-11 7S1 12 1 12z" fill="none" stroke="currentColor" stroke-width="2"/>'
    '<circle cx="12" cy="12" r="3" fill="none" stroke="currentColor" stroke-width="2"/>'
    '<line class="slash" x1="3" y1="21" x2="21" y2="3" stroke="currentColor" stroke-width="2"/></svg>'
)


def password_field(field_id: str = "password") -> str:
    return (
        f'<div class="pw"><input id="{field_id}" name="{field_id}" type="password" '
        'autocomplete="current-password" required>'
        f'<button type="button" class="reveal" data-reveal="{field_id}" aria-label="Show password" '
        f'aria-pressed="false" hidden>{EYE_ICON}</button></div>'
    )


@dataclass
class LoginConfig:
    owner_username: str
    password_hash: str
    totp_secret: str
    wiki_title: str
    secure_cookies: bool


def _page(title: str, body: str, *, status: int = 200, form_action: str = "'self'") -> HTMLResponse:
    html = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow">
<title>{escape(title)}</title>
<style>
:root {{ color-scheme: light dark; --bg:#f6f7f9; --card:#fff; --fg:#1d2330; --muted:#5b6475; --accent:#2f5bd3; --err:#b3261e; --line:#d9dde5; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#111418; --card:#1a1f26; --fg:#e7eaf0; --muted:#9aa3b2; --accent:#7ea2ff; --err:#ffb4ab; --line:#2c333d; }} }}
* {{ box-sizing: border-box; }}
body {{ margin:0; min-height:100vh; display:grid; place-items:center; background:var(--bg); color:var(--fg);
  font:16px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif; padding:16px; }}
main {{ width:100%; max-width:420px; background:var(--card); border:1px solid var(--line); border-radius:12px; padding:24px; }}
h1 {{ font-size:1.25rem; margin:0 0 4px; }} p {{ margin:0 0 16px; color:var(--muted); }}
label {{ display:block; font-weight:600; margin:12px 0 4px; }}
input[type=text],input[type=password] {{ width:100%; padding:10px; border:1px solid var(--line); border-radius:8px;
  background:transparent; color:inherit; font:inherit; }}
fieldset {{ border:1px solid var(--line); border-radius:8px; margin:16px 0; padding:8px 12px; }}
fieldset label {{ font-weight:400; margin:6px 0; }}
.row {{ display:flex; flex-direction:row-reverse; gap:8px; margin-top:20px; }}
button {{ flex:1; padding:10px; border-radius:8px; border:1px solid var(--line); font:inherit; cursor:pointer;
  background:transparent; color:inherit; }}
button.primary {{ background:var(--accent); border-color:var(--accent); color:#fff; font-weight:600; }}
.err {{ color:var(--err); font-weight:600; }} code {{ word-break:break-all; }}
.pw {{ position:relative; }} .pw input {{ padding-right:44px; }}
.reveal {{ position:absolute; right:4px; top:50%; transform:translateY(-50%); flex:none; border:0;
  padding:6px; line-height:0; color:var(--muted); background:transparent; }}
.reveal[hidden] {{ display:none; }} .reveal .slash {{ display:none; }}
.reveal[aria-pressed="true"] .slash {{ display:inline; }}
.reveal:focus-visible {{ outline:2px solid var(--accent); border-radius:6px; }}
</style></head><body><main>{body}</main><script>{REVEAL_SCRIPT}</script></body></html>"""
    return HTMLResponse(
        html,
        status_code=status,
        headers={
            "Content-Security-Policy": (
                f"default-src 'none'; style-src 'unsafe-inline'; script-src '{REVEAL_HASH}'; "
                f"form-action {form_action}; "
                "frame-ancestors 'none'; base-uri 'none'"
            ),
            "X-Frame-Options": "DENY",
            "Referrer-Policy": "no-referrer",
            "Cache-Control": "no-store",
        },
    )


def error_page(message: str, status: int = 400) -> HTMLResponse:
    return _page(
        "Sign-in error", f"<h1>Can't continue</h1><p class=err>{escape(message)}</p>", status=status
    )


class LoginHandler:
    def __init__(
        self,
        provider: OAuthProvider,
        config: LoginConfig,
        audit: AuditLog,
        lockout: Lockout,
        client_ip: object,
    ) -> None:
        self.provider = provider
        self.config = config
        self.audit = audit
        self.lockout = lockout
        self._client_ip = client_ip
        # Approved requests: request id -> (redirect URL with the code, csrf, client, expiry).
        # A second submit of the same form gets the same redirect instead of an expired page
        # that would cancel the first one. Kept until the code expires; in memory (one process).
        self._approved: dict[str, tuple[str, str, str, float]] = {}

    def ip(self, request: Request) -> str:
        return self._client_ip(request.scope)  # type: ignore[operator,no-any-return]

    def _form(
        self, pending: PendingAuthorization, *, error: str = "", username: str = ""
    ) -> HTMLResponse:
        client_name = self.provider.label_for(pending.client_id)
        redirect = str(pending.params.redirect_uri)
        parts = urlsplit(redirect)
        origin = f"{parts.scheme}://{parts.netloc}"
        requested = set(pending.params.scopes or [])
        scope_boxes = []
        for scope in SCOPES:
            checked = (
                " checked"
                if scope == "wiki:read" or (scope in requested and scope != PRIVATE_SCOPE)
                else ""
            )
            disabled = " disabled" if scope == "wiki:read" else ""
            scope_boxes.append(
                f'<label><input type="checkbox" name="scope" value="{scope}"{checked}{disabled}> '
                f"{escape(SCOPE_TEXT[scope])}</label>"
            )
        totp = (
            '<label for="totp">Authenticator code</label>'
            '<input id="totp" name="totp" type="text" inputmode="numeric" autocomplete="one-time-code" required>'
            if self.config.totp_secret
            else ""
        )
        err = f'<p class="err" role="alert">{escape(error)}</p>' if error else ""
        # The owner's per-app toggle overrides the private-pages box: say so before approving.
        toggle = self.provider.private_toggle(client_name)
        if toggle is not None:
            effect = (
                "it sees private pages whatever you tick here"
                if toggle == "on"
                else "it does not see private pages, even if you tick the box"
            )
            scope_boxes.append(
                f"<p><strong>Private pages are turned {toggle} for "
                f"{escape(client_name)}</strong> in the portal (Connected apps): {effect}.</p>"
            )
        # Approve comes first in the markup: pressing Enter in a field (or a password manager
        # submitting the form) uses the first submit button. `.row` is row-reverse, so Deny
        # still shows on the left.
        body = f"""
<h1>Connect to {escape(self.config.wiki_title)}</h1>
<p><strong>{escape(client_name)}</strong> wants access to your wiki.<br>
After sign-in you will be sent to <code>{escape(parts.netloc)}</code>.</p>
<p>Each brain has one account: user name <code>owner</code> and the admin password chosen in the
setup wizard. New accounts can't be created here; every employee runs their own brain.</p>
{err}
<form method="post" action="/login">
<input type="hidden" name="request" value="{escape(pending.id)}">
<input type="hidden" name="csrf" value="{escape(pending.csrf)}">
<fieldset><legend>Permissions</legend>{"".join(scope_boxes)}</fieldset>
<label for="username">Username</label>
<input id="username" name="username" type="text" autocomplete="username" value="{escape(username)}" required>
<label for="password">Password</label>
{password_field()}
{totp}
<div class="row">
<button class="primary" type="submit" name="action" value="approve">Approve</button>
<button type="submit" name="action" value="deny" formnovalidate>Deny</button>
</div>
</form>"""
        response = _page(
            f"Sign in — {self.config.wiki_title}",
            body,
            status=401 if error else 200,
            form_action=f"'self' {origin}",
        )
        response.set_cookie(
            CSRF_COOKIE,
            pending.csrf,
            max_age=600,
            httponly=True,
            secure=self.config.secure_cookies,
            samesite="strict",
            path="/login",
        )
        return response

    def _approval(self, rid: str) -> tuple[str, str, str, float] | None:
        now = time.time()
        for key in [k for k, v in self._approved.items() if v[3] < now]:
            del self._approved[key]
        return self._approved.get(rid)

    async def get(self, request: Request) -> Response:
        rid = request.query_params.get("request", "")
        pending = self.provider.get_pending(rid) if valid_request_id(rid) else None
        if pending is None:
            if self._approval(rid) is not None:
                return error_page("This connection is already approved. Return to your app.")
            self.audit.event("login", ip=self.ip(request), outcome="expired", method="GET")
            return error_page(EXPIRED)
        return self._form(pending)

    def _redirect(self, pending: PendingAuthorization, **params: str | None) -> RedirectResponse:
        url = construct_redirect_uri(
            str(pending.params.redirect_uri),
            state=pending.params.state,
            iss=self.provider.issuer,
            **params,
        )
        return self._redirect_to(url)

    def _redirect_to(self, url: str) -> RedirectResponse:
        response = RedirectResponse(url, status_code=302, headers={"Cache-Control": "no-store"})
        response.delete_cookie(CSRF_COOKIE, path="/login")
        return response

    def _replay(self, request: Request, rid: str, csrf: str) -> Response:
        """A repeated approve for a request already approved: the same redirect again (no new
        code). The CSRF cookie went with the first response, so the form field must match."""
        approval = self._approval(rid)
        if approval is None or not hmac.compare_digest(csrf.encode(), approval[1].encode()):
            self.audit.event("login", ip=self.ip(request), outcome="expired", method="POST")
            return error_page(EXPIRED)
        self.audit.event("consent", client=approval[2], ip=self.ip(request), outcome="replayed")
        return self._redirect_to(approval[0])

    async def post(self, request: Request) -> Response:
        form = await request.form()
        rid = str(form.get("request", ""))
        pending = self.provider.get_pending(rid) if valid_request_id(rid) else None
        if pending is None:
            return self._replay(request, rid, str(form.get("csrf", "")))
        csrf_form = str(form.get("csrf", "")).encode()
        csrf_cookie = request.cookies.get(CSRF_COOKIE, "").encode()
        expected = pending.csrf.encode()
        if not (
            hmac.compare_digest(csrf_form, expected) and hmac.compare_digest(csrf_cookie, expected)
        ):
            self.audit.event("login", ip=self.ip(request), outcome="csrf_failed")
            return error_page(
                "Your session could not be verified. Reload the page and try again.", 403
            )

        client = self.provider.label_for(pending.client_id)
        ip = self.ip(request)
        if form.get("action") == "deny":
            self.provider.drop_pending(pending.id)
            self.audit.event("consent", client=client, ip=ip, outcome="denied")
            return self._redirect(
                pending, error="access_denied", error_description="The owner denied access"
            )

        keys = ("owner", f"ip:{ip}")
        wait = self.lockout.blocked_for(*keys)
        username = str(form.get("username", ""))[:200]
        if wait > 0:
            self.audit.event("login", client=client, ip=ip, outcome="locked", retry_after=int(wait))
            response = self._form(
                pending,
                error=f"Too many failed attempts. Try again in {int(wait) + 1} seconds.",
                username=username,
            )
            response.status_code = 429
            return response
        ok = verify_owner(
            username=username,
            password=str(form.get("password", ""))[:1000],
            totp_code=str(form.get("totp", ""))[:20],
            expected_username=self.config.owner_username,
            password_hash=self.config.password_hash,
            totp_secret=self.config.totp_secret,
        )
        if not ok:
            self.lockout.fail(*keys)
            self.audit.event("login", client=client, ip=ip, outcome="failed", username=username)
            return self._form(pending, error="Wrong username, password or code.", username=username)

        self.lockout.succeed(*keys)
        granted = ["wiki:read"] + [
            s for s in form.getlist("scope") if s in SCOPES and s != "wiki:read"
        ]
        code = self.provider.issue_code(pending, granted)
        self.audit.event("consent", client=client, ip=ip, outcome="approved", scopes=granted)
        redirect = self._redirect(pending, code=code)
        self._approved[pending.id] = (
            redirect.headers["location"],
            pending.csrf,
            client,
            time.time() + self.provider.code_ttl,
        )
        return redirect
