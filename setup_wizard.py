#!/usr/bin/env python3
"""Interactive wizard that writes .env for this server.

Run this once when setting up (or reconfiguring) the server:

    python3 setup_wizard.py

Stdlib only, on purpose — it must be runnable before `uv sync` on a fresh box.
"""

import getpass
import secrets
import urllib.request

from paths import ENV_FILE


def ask(prompt: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default else ""
    value = input(f"{prompt}{suffix}: ").strip()
    return value or (default or "")


def ask_required(prompt: str) -> str:
    while True:
        value = input(f"{prompt}: ").strip()
        if value:
            return value
        print("  (cannot be empty, try again)")


def ask_secret(prompt: str) -> str:
    while True:
        value = getpass.getpass(f"{prompt}: ").strip()
        if value:
            return value
        print("  (cannot be empty, try again)")


def detect_public_ip() -> str | None:
    for url in ("https://api.ipify.org", "https://ifconfig.me"):
        try:
            with urllib.request.urlopen(url, timeout=5) as resp:
                ip = resp.read().decode().strip()
                if ip.count(".") == 3:
                    return ip
        except OSError:
            continue
    return None


def main() -> None:
    print("=== t212-mcp setup wizard ===\n")

    if ENV_FILE.exists():
        overwrite = ask(f"{ENV_FILE} already exists, overwrite it? (y/N)", "N")
        if overwrite.lower() != "y":
            print("Cancelled.")
            return

    print("\n--- Trading212 API ---")
    print("Generate a Key/Secret pair from the Trading212 app: Settings -> API (Beta).")
    t212_env = ask("Environment (live/demo)", "live")
    while t212_env not in ("live", "demo"):
        t212_env = ask("Please enter live or demo", "live")
    t212_key = ask_secret("T212_API_KEY")
    t212_secret = ask_secret("T212_API_SECRET")

    print("\n--- Domain ---")
    print("claude.ai needs an HTTPS domain to reach this server (a bare IP can't get a trusted cert).")
    use_sslip = ask("No domain of your own? Use a free <ip>.sslip.io one (Y/n)", "Y")
    if use_sslip.lower() != "n":
        print("Detecting this machine's public IP...")
        ip = detect_public_ip()
        if ip is None:
            ip = ask_required("Auto-detection failed, enter this machine's public IPv4 manually")
        domain = ip.replace(".", "-") + ".sslip.io"
        print(f"Using: {domain}")
    else:
        domain = ask_required("Enter your domain (its A record must already point at this machine)")

    print("\n--- MCP login password ---")
    print("When claude.ai connects, a browser tab will prompt for this password.")
    auto_pw = ask("Auto-generate a strong password? (Y/n)", "Y")
    if auto_pw.lower() != "n":
        auth_password = secrets.token_urlsafe(18)
        print(f"Generated password: {auth_password}")
    else:
        auth_password = ask_secret("Custom password")

    env_content = f"""T212_ENV={t212_env}
T212_API_KEY={t212_key}
T212_API_SECRET={t212_secret}

MCP_PUBLIC_URL=https://{domain}
MCP_DOMAIN={domain}
MCP_AUTH_PASSWORD={auth_password}

HOST=127.0.0.1
PORT=8000
"""

    ENV_FILE.write_text(env_content)
    ENV_FILE.chmod(0o600)

    print(f"\nWrote {ENV_FILE} (mode 600, readable/writable only by you).")
    print("Next, follow the deployment steps in README.md to start the service.")


if __name__ == "__main__":
    main()
