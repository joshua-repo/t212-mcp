#!/usr/bin/env python3
"""End-to-end health check for a deployed t212-mcp. Run on the VPS as root
(it reads the service's .env):

    sudo deploy/healthcheck.py            # all checks, incl. one read-only T212 API call
    sudo deploy/healthcheck.py --no-t212  # skip the T212 API call

Exits non-zero if any check fails.

The authenticated checks log in as their own OAuth client — registered once
and remembered in healthcheck_client.json — through the same /authorize ->
/login -> /token flow Claude uses, then revoke the tokens they got. Claude's
own client and tokens are never read or touched.

Stdlib only, so it runs under any system python3 even if the app's venv is
broken.
"""

import base64
import hashlib
import json
import os
import secrets
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

APP_DIR = Path(os.environ.get("APP_DIR", "/opt/t212-mcp"))
SERVICE = os.environ.get("SERVICE", "t212-mcp")
CLIENT_FILE = APP_DIR / "healthcheck_client.json"
REDIRECT_URI = "http://127.0.0.1/healthcheck-callback"
EXPECTED_TOOLS = {"get_account_summary", "get_portfolio", "get_account_cash", "get_order_history", "get_dividends"}

failures = 0


def report(ok: bool, msg: str) -> bool:
    global failures
    if not ok:
        failures += 1
    print(f"  \033[{'32mPASS' if ok else '31mFAIL'}\033[0m {msg}")
    return ok


def skip(msg: str) -> None:
    print(f"  \033[33mSKIP\033[0m {msg}")


def load_env() -> dict[str, str]:
    env = {}
    for line in (APP_DIR / ".env").read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            env[key.strip()] = value.strip().strip("'\"")
    return env


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


_opener = urllib.request.build_opener(_NoRedirect)


def http(method: str, url: str, *, form: dict | None = None, json_body: dict | None = None,
         headers: dict | None = None, timeout: float = 30) -> tuple[int, dict, str]:
    """Returns (status, headers, body); never raises on HTTP status, never follows redirects."""
    headers = dict(headers or {})
    data = None
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    elif json_body is not None:
        data = json.dumps(json_body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with _opener.open(req, timeout=timeout) as resp:
            return resp.status, dict(resp.headers), resp.read().decode(errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read().decode(errors="replace")
    except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
        return 0, {}, str(e)


# -- OAuth login as the healthcheck client ---------------------------------


def register_client(public_url: str) -> dict:
    status, _, body = http("POST", f"{public_url}/register", json_body={
        "client_name": "t212-mcp healthcheck",
        "redirect_uris": [REDIRECT_URI],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "client_secret_post",
    })
    if status not in (200, 201):
        raise RuntimeError(f"client registration failed ({status}): {body[:200]}")
    client = json.loads(body)
    CLIENT_FILE.write_text(json.dumps({"client_id": client["client_id"], "client_secret": client["client_secret"]}))
    CLIENT_FILE.chmod(0o600)
    return client


def authorize(public_url: str, password: str, client: dict) -> tuple[int, str]:
    """Runs /authorize -> /login. Returns (status, auth code or error text)."""
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    client["_verifier"] = verifier

    query = urllib.parse.urlencode({
        "response_type": "code",
        "client_id": client["client_id"],
        "redirect_uri": REDIRECT_URI,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
        "resource": f"{public_url}/mcp",
    })
    status, headers, body = http("GET", f"{public_url}/authorize?{query}")
    login_url = headers.get("Location", "")
    req_id = urllib.parse.parse_qs(urllib.parse.urlparse(login_url).query).get("req", [""])[0]
    if status != 302 or not req_id:
        return status, f"/authorize: {body[:200] or login_url}"

    status, headers, body = http("POST", f"{public_url}/login", form={"req": req_id, "password": password})
    params = urllib.parse.parse_qs(urllib.parse.urlparse(headers.get("Location", "")).query)
    if status == 401:
        return status, "/login: wrong password (check MCP_AUTH_PASSWORD in .env)"
    if status != 302 or params.get("state") != [state] or "code" not in params:
        return status, f"/login: {body[:200]}"
    return status, params["code"][0]


def login(public_url: str, password: str) -> dict:
    """Returns the token response for a fresh healthcheck session."""
    client = json.loads(CLIENT_FILE.read_text()) if CLIENT_FILE.exists() else None
    if client is None:
        client = register_client(public_url)
    status, result = authorize(public_url, password, client)
    if status == 400 and "client" in result.lower():
        # Server-side OAuth state was reset and forgot our client; register again.
        client = register_client(public_url)
        status, result = authorize(public_url, password, client)
    if status != 302:
        raise RuntimeError(f"login failed ({status}) at {result}")

    status, _, body = http("POST", f"{public_url}/token", form={
        "grant_type": "authorization_code",
        "code": result,
        "redirect_uri": REDIRECT_URI,
        "code_verifier": client["_verifier"],
        "client_id": client["client_id"],
        "client_secret": client["client_secret"],
        "resource": f"{public_url}/mcp",
    })
    if status != 200:
        raise RuntimeError(f"token exchange failed ({status}): {body[:200]}")
    return {**json.loads(body), "client_id": client["client_id"], "client_secret": client["client_secret"]}


def revoke(public_url: str, session: dict) -> bool:
    ok = True
    for kind in ("access_token", "refresh_token"):
        if session.get(kind):
            status, _, _ = http("POST", f"{public_url}/revoke", form={
                "token": session[kind],
                "token_type_hint": kind,
                "client_id": session["client_id"],
                "client_secret": session["client_secret"],
            })
            ok &= status == 200
    return ok


# -- MCP calls -------------------------------------------------------------


def mcp_call(public_url: str, token: str, method: str, params: dict | None = None) -> tuple[int, dict | str]:
    """POSTs one JSON-RPC request; returns (status, parsed message or raw body)."""
    payload = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params is not None:
        payload["params"] = params
    status, _, body = http("POST", f"{public_url}/mcp", json_body=payload, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/json, text/event-stream",
    })
    for line in body.splitlines():  # SSE framing: "data: {...}"
        if line.startswith("data:"):
            body = line[5:].strip()
            break
    try:
        return status, json.loads(body)
    except json.JSONDecodeError:
        return status, body[:200]


def main() -> int:
    call_t212 = "--no-t212" not in sys.argv[1:]
    env = load_env()
    public_url = env["MCP_PUBLIC_URL"].rstrip("/")
    host, port = env.get("HOST") or "127.0.0.1", env.get("PORT") or "8000"
    print(f"t212-mcp health check ({public_url})")

    print("Service")
    active = subprocess.run(["systemctl", "is-active", "--quiet", SERVICE]).returncode == 0
    report(active, f"systemd unit {SERVICE} is {'active' if active else 'not active'}")
    listeners = subprocess.run(["ss", "-ltnH", f"sport = :{port}"], capture_output=True, text=True).stdout
    report(f"{host}:{port}" in listeners, f"listening on {host}:{port}")
    if any(f"{a}:{port}" in listeners for a in ("0.0.0.0", "*", "[::]")):
        report(False, f"port {port} is bound on all interfaces (bypasses the reverse proxy)")

    print("HTTP")
    status, _, _ = http("GET", f"http://{host}:{port}/.well-known/oauth-authorization-server", timeout=10)
    report(status == 200, f"local OAuth metadata ({status})")
    status, _, _ = http("GET", f"{public_url}/.well-known/oauth-authorization-server", timeout=10)
    report(status == 200, f"public OAuth metadata via TLS proxy ({status})")
    status, _, _ = http("POST", f"{public_url}/mcp", timeout=10)
    report(status == 401, f"unauthenticated /mcp rejected ({status})")

    print("OAuth")
    try:
        session = login(public_url, env["MCP_AUTH_PASSWORD"])
        report(True, "register/authorize/login/token flow issued a token")
    except (RuntimeError, KeyError, json.JSONDecodeError) as e:
        report(False, str(e))
        session = None

    print("MCP (authenticated)")
    if session is None:
        skip("no token, see OAuth failure above")
    else:
        token = session["access_token"]
        try:
            status, msg = mcp_call(public_url, token, "initialize", {
                "protocolVersion": "2025-06-18", "capabilities": {},
                "clientInfo": {"name": "healthcheck", "version": "0"},
            })
            name = msg.get("result", {}).get("serverInfo", {}).get("name") if isinstance(msg, dict) else None
            report(status == 200 and name == "t212-mcp", f"initialize ({status})" + ("" if name else f": {msg}"))

            status, msg = mcp_call(public_url, token, "tools/list")
            tools = {t["name"] for t in msg.get("result", {}).get("tools", [])} if isinstance(msg, dict) else set()
            missing = EXPECTED_TOOLS - tools
            report(status == 200 and not missing,
                   f"tools/list returns all {len(EXPECTED_TOOLS)} tools" if not missing
                   else f"tools/list ({status}), missing: {', '.join(sorted(missing)) or msg}")

            if call_t212:
                status, msg = mcp_call(public_url, token, "tools/call", {"name": "get_account_cash", "arguments": {}})
                result = msg.get("result") if isinstance(msg, dict) else None
                ok = status == 200 and result is not None and not result.get("isError")
                detail = "" if ok else f" ({status}): {(result or {}).get('content', [{}])[0].get('text', msg) if result else msg}"
                report(ok, "tools/call get_account_cash reached the T212 API" + detail)
            else:
                skip("T212 API call (--no-t212)")
        finally:
            report(revoke(public_url, session), "revoked the healthcheck's tokens")

    print()
    if failures:
        print(f"{failures} check(s) failed")
        return 1
    print("All checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
