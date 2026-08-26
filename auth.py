# auth.py
"""Minimal single-user OAuth authorization server for the t212-mcp server.

Lets claude.ai (or any MCP client that speaks OAuth + dynamic client
registration) connect to this server. There is exactly one human user: whoever
knows MCP_AUTH_PASSWORD. The /authorize step redirects the browser to a small
password-gated /login page instead of a real third-party login screen.
"""

import json
import secrets
import time

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    OAuthAuthorizationServerProvider,
    RefreshToken,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from paths import STATE_DIR, STATE_FILE

AUTH_CODE_TTL = 300
ACCESS_TOKEN_TTL = 3600
REFRESH_TOKEN_TTL = 60 * 60 * 24 * 180  # 180 days
PENDING_LOGIN_TTL = 600
MAX_LOGIN_ATTEMPTS = 10


class SingleUserOAuthProvider(OAuthAuthorizationServerProvider[AuthorizationCode, RefreshToken, AccessToken]):
    def __init__(self, password: str, public_url: str):
        self._password = password
        self.public_url = public_url.rstrip("/")

        self._clients: dict[str, OAuthClientInformationFull] = {}
        self._auth_codes: dict[str, AuthorizationCode] = {}
        self._access_tokens: dict[str, AccessToken] = {}
        self._refresh_tokens: dict[str, RefreshToken] = {}

        # req_id -> (client_id, AuthorizationParams, expires_at, attempts)
        self._pending: dict[str, tuple[str, AuthorizationParams, float, int]] = {}

        self._load_state()

    # -- persistence (survives restarts on a VPS) -----------------------

    def _load_state(self) -> None:
        if not STATE_FILE.exists():
            return
        try:
            data = json.loads(STATE_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            return
        for client_id, raw in data.get("clients", {}).items():
            self._clients[client_id] = OAuthClientInformationFull.model_validate(raw)
        for token, raw in data.get("access_tokens", {}).items():
            self._access_tokens[token] = AccessToken.model_validate(raw)
        for token, raw in data.get("refresh_tokens", {}).items():
            self._refresh_tokens[token] = RefreshToken.model_validate(raw)

    def _save_state(self) -> None:
        data = {
            "clients": {cid: c.model_dump(mode="json") for cid, c in self._clients.items()},
            "access_tokens": {t: a.model_dump(mode="json") for t, a in self._access_tokens.items()},
            "refresh_tokens": {t: r.model_dump(mode="json") for t, r in self._refresh_tokens.items()},
        }
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(data))
        STATE_FILE.chmod(0o600)

    # -- dynamic client registration -------------------------------------

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        return self._clients.get(client_id)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        self._clients[client_info.client_id] = client_info
        self._save_state()

    # -- authorization code flow ------------------------------------------

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        req_id = secrets.token_urlsafe(16)
        self._pending[req_id] = (client.client_id, params, time.time() + PENDING_LOGIN_TTL, 0)
        return f"{self.public_url}/login?req={req_id}"

    def peek_pending(self, req_id: str) -> tuple[OAuthClientInformationFull, AuthorizationParams] | None:
        entry = self._pending.get(req_id)
        if entry is None:
            return None
        client_id, params, expires_at, _ = entry
        if expires_at < time.time():
            del self._pending[req_id]
            return None
        client = self._clients.get(client_id)
        if client is None:
            del self._pending[req_id]
            return None
        return client, params

    def record_failed_attempt(self, req_id: str) -> bool:
        """Returns False if the request should now be invalidated (too many tries)."""
        entry = self._pending.get(req_id)
        if entry is None:
            return False
        client_id, params, expires_at, attempts = entry
        attempts += 1
        if attempts >= MAX_LOGIN_ATTEMPTS:
            del self._pending[req_id]
            return False
        self._pending[req_id] = (client_id, params, expires_at, attempts)
        return True

    def check_password(self, password: str) -> bool:
        return secrets.compare_digest(password.encode(), self._password.encode())

    def complete_login(self, req_id: str) -> str | None:
        """Password verified; issue an auth code and return the redirect URL."""
        entry = self.peek_pending(req_id)
        if entry is None:
            return None
        client, params = entry
        del self._pending[req_id]

        code = secrets.token_urlsafe(32)
        self._auth_codes[code] = AuthorizationCode(
            code=code,
            scopes=params.scopes or [],
            expires_at=time.time() + AUTH_CODE_TTL,
            client_id=client.client_id,
            code_challenge=params.code_challenge,
            redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            resource=params.resource,
        )

        return construct_redirect_uri(str(params.redirect_uri), code=code, state=params.state)

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        return self._auth_codes.get(authorization_code)

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        del self._auth_codes[authorization_code.code]

        access_token = secrets.token_urlsafe(32)
        refresh_token = secrets.token_urlsafe(32)
        now = time.time()

        self._access_tokens[access_token] = AccessToken(
            token=access_token,
            client_id=client.client_id,
            scopes=authorization_code.scopes,
            expires_at=int(now + ACCESS_TOKEN_TTL),
            resource=authorization_code.resource,
        )
        self._refresh_tokens[refresh_token] = RefreshToken(
            token=refresh_token,
            client_id=client.client_id,
            scopes=authorization_code.scopes,
            expires_at=int(now + REFRESH_TOKEN_TTL),
        )
        self._save_state()

        return OAuthToken(
            access_token=access_token,
            token_type="bearer",
            expires_in=ACCESS_TOKEN_TTL,
            refresh_token=refresh_token,
            scope=" ".join(authorization_code.scopes) if authorization_code.scopes else None,
        )

    async def load_refresh_token(self, client: OAuthClientInformationFull, refresh_token: str) -> RefreshToken | None:
        return self._refresh_tokens.get(refresh_token)

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        del self._refresh_tokens[refresh_token.token]
        self._access_tokens = {
            t: a for t, a in self._access_tokens.items() if a.client_id != client.client_id
        }

        new_access_token = secrets.token_urlsafe(32)
        new_refresh_token = secrets.token_urlsafe(32)
        now = time.time()

        self._access_tokens[new_access_token] = AccessToken(
            token=new_access_token,
            client_id=client.client_id,
            scopes=scopes,
            expires_at=int(now + ACCESS_TOKEN_TTL),
        )
        self._refresh_tokens[new_refresh_token] = RefreshToken(
            token=new_refresh_token,
            client_id=client.client_id,
            scopes=scopes,
            expires_at=int(now + REFRESH_TOKEN_TTL),
        )
        self._save_state()

        return OAuthToken(
            access_token=new_access_token,
            token_type="bearer",
            expires_in=ACCESS_TOKEN_TTL,
            refresh_token=new_refresh_token,
            scope=" ".join(scopes) if scopes else None,
        )

    async def load_access_token(self, token: str) -> AccessToken | None:
        access_token = self._access_tokens.get(token)
        if access_token is None:
            return None
        if access_token.expires_at and access_token.expires_at < time.time():
            del self._access_tokens[token]
            return None
        return access_token

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        self._access_tokens.pop(token.token, None)
        self._refresh_tokens.pop(token.token, None)
        self._save_state()


def _login_page(req_id: str, error: str | None = None) -> str:
    error_html = f'<p class="error">{error}</p>' if error else ""
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>t212-mcp login</title>
<style>
body {{ font-family: system-ui, sans-serif; background: #0f172a; color: #e2e8f0;
       display: flex; align-items: center; justify-content: center; height: 100vh; margin: 0; }}
form {{ background: #1e293b; padding: 2rem; border-radius: 12px; width: 320px; }}
h1 {{ font-size: 1.1rem; margin: 0 0 1rem; }}
input {{ width: 100%; padding: 0.6rem; margin: 0.5rem 0 1rem; border-radius: 6px;
        border: 1px solid #334155; background: #0f172a; color: #e2e8f0; box-sizing: border-box; }}
button {{ width: 100%; padding: 0.6rem; border-radius: 6px; border: none;
         background: #22c55e; color: #052e16; font-weight: 600; cursor: pointer; }}
.error {{ color: #f87171; font-size: 0.9rem; }}
</style></head>
<body>
<form method="post" action="/login">
  <h1>Authorize t212-mcp</h1>
  <input type="hidden" name="req" value="{req_id}">
  <input type="password" name="password" placeholder="Password" autofocus required>
  {error_html}
  <button type="submit">Authorize</button>
</form>
</body></html>"""


def register_auth_routes(mcp, provider: SingleUserOAuthProvider) -> None:
    @mcp.custom_route("/login", methods=["GET"])
    async def login_form(request: Request) -> Response:
        req_id = request.query_params.get("req", "")
        if provider.peek_pending(req_id) is None:
            return HTMLResponse("<p>This login link is invalid or has expired.</p>", status_code=400)
        return HTMLResponse(_login_page(req_id))

    @mcp.custom_route("/login", methods=["POST"])
    async def login_submit(request: Request) -> Response:
        form = await request.form()
        req_id = str(form.get("req", ""))
        password = str(form.get("password", ""))

        if provider.peek_pending(req_id) is None:
            return HTMLResponse("<p>This login link is invalid or has expired.</p>", status_code=400)

        if not provider.check_password(password):
            if not provider.record_failed_attempt(req_id):
                return HTMLResponse("<p>Too many attempts. Please restart the connection from Claude.</p>", status_code=429)
            return HTMLResponse(_login_page(req_id, error="Wrong password."), status_code=401)

        redirect_url = provider.complete_login(req_id)
        if redirect_url is None:
            return HTMLResponse("<p>This login link is invalid or has expired.</p>", status_code=400)
        return RedirectResponse(url=redirect_url, status_code=302)
