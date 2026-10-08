# t212-mcp

An MCP server exposing your Trading212 account (summary, portfolio, cash, order
history, dividends) as tools, meant to be connected directly to claude.ai as a
custom remote connector.

Because claude.ai reaches this server over the public internet and your T212
account is `live`, the server requires OAuth login (a single password you set)
before it will serve any tool calls — see [auth.py](auth.py).

## 0. Get the code onto the VPS, and install prerequisites

```bash
git clone <this-repo-url> t212-mcp && cd t212-mcp
```

You'll need on the VPS: `git`, `rsync`, and [uv](https://docs.astral.sh/uv/) installed **system-wide** (not just under your own `$HOME`, since the app later runs as its own dedicated system user with no home directory):

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
sudo cp "$HOME/.local/bin/uv" /usr/local/bin/uv
```

## 1. Configure

```bash
python3 setup_wizard.py
```

Walks you through it interactively: your T212 API key/secret, a domain (offers a free `<ip>.sslip.io` one if you don't have your own), and the login password — then writes `.env` with `chmod 600`. Re-run it any time to regenerate `.env` from scratch (it'll ask before overwriting).

Prefer to edit by hand instead? `cp .env.example .env` and fill in:

- `T212_API_KEY` / `T212_API_SECRET` — from Trading212 → Settings → API (Key/Secret pair, Basic auth).
- `T212_ENV` — `live` or `demo`.
- `MCP_PUBLIC_URL` — the HTTPS URL this server will be reachable at, e.g. `https://mcp.example.com` (no trailing slash). Must match your DNS + reverse proxy setup.
- `MCP_DOMAIN` — same host as above, without the scheme (Caddy uses this to request a TLS cert).
- `MCP_AUTH_PASSWORD` — a password only you know; you'll type it once in the browser when connecting Claude.

## 2. Point DNS at your VPS

Create an `A`/`AAAA` record for the domain in `MCP_DOMAIN` pointing at your VPS's IP. Ports 80 and 443 must be reachable from the internet.

Before installing a TLS reverse proxy below, check whether something is already bound to 80/443 on this machine (an existing nginx/Caddy serving other sites):

```bash
sudo ss -tlnp | grep -E ':80|:443'
```

## 3. Run it

```bash
sudo useradd --system --no-create-home --shell /usr/sbin/nologin t212mcp
sudo mkdir -p /opt/t212-mcp
sudo rsync -a --exclude .venv --exclude __pycache__ --exclude .git ./ /opt/t212-mcp/
sudo chown -R t212mcp:t212mcp /opt/t212-mcp
cd /opt/t212-mcp
# t212mcp has no home dir, so point uv's cache somewhere it can write:
sudo -u t212mcp env HOME=/opt/t212-mcp UV_CACHE_DIR=/opt/t212-mcp/.cache/uv \
    uv sync --frozen --no-dev

sudo cp deploy/t212-mcp.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now t212-mcp
```

Then get TLS in front of it on port 8000, depending on what `ss -tlnp` showed above:

- **Nothing running on 80/443** — install Caddy (single static binary, ~10-20MB RAM idle) and point it at [deploy/Caddyfile](deploy/Caddyfile):
  ```bash
  sudo apt install -y debian-keyring debian-archive-keyring apt-transport-https
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | sudo tee /etc/apt/sources.list.d/caddy-stable.list
  sudo apt update && sudo apt install -y caddy
  set -a; source .env; set +a
  sudo bash -c "sed 's/{\$MCP_DOMAIN}/'\"$MCP_DOMAIN\"'/' deploy/Caddyfile > /etc/caddy/Caddyfile"
  sudo systemctl reload caddy
  ```
- **nginx already running** for other sites — copy [deploy/nginx.conf.example](deploy/nginx.conf.example) into `/etc/nginx/sites-available/t212-mcp`, replace `mcp.example.com` in it with your real `$MCP_DOMAIN`, symlink it into `sites-enabled`, `nginx -t && systemctl reload nginx`, then `sudo certbot --nginx -d $MCP_DOMAIN` (doesn't touch your existing server blocks).

Check it's up:

```bash
curl https://$MCP_DOMAIN/.well-known/oauth-authorization-server
```

## 4. Connect it to Claude

In claude.ai: **Settings → Connectors → Add custom connector**, paste `https://<MCP_DOMAIN>/mcp` as the URL. Claude will open a browser tab to this server's login page — enter `MCP_AUTH_PASSWORD` — and you'll be redirected back, connected.

## 5. Check it end to end

After any deploy (or whenever Claude says the connector isn't working), run on the VPS:

```bash
sudo deploy/healthcheck.sh
```

It checks the systemd unit, the localhost-only bind, the OAuth metadata both locally and through the TLS proxy, that unauthenticated `/mcp` gets a 401, and then — reusing the access token Claude obtained, from `oauth_state.json` — runs `initialize`, `tools/list` and one read-only tool call (`get_account_cash`) through the public URL. Pass `--no-t212` to skip that last call. Exits non-zero on any failure, so it can gate a CI deploy. The authenticated checks are skipped until Claude has connected at least once.

## Local development

`uv run python server.py` runs the same streamable-HTTP + OAuth server locally on `HOST:PORT` (default `127.0.0.1:8000`, so only the local reverse proxy can reach it) — `MCP_PUBLIC_URL` and `MCP_AUTH_PASSWORD` are required env vars even for local runs, since the OAuth provider is wired in unconditionally. Point `MCP_PUBLIC_URL` at `http://127.0.0.1:8000` for local-only testing (no TLS, no Claude connector, just for exercising the tool code and the OAuth flow with curl).
