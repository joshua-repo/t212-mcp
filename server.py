# server.py
import base64
import os

import httpx
from dotenv import load_dotenv
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.mcpserver import MCPServer

from auth import SingleUserOAuthProvider, register_auth_routes
from paths import ENV_FILE

load_dotenv(ENV_FILE)

T212_ENV = os.environ.get("T212_ENV", "live")
T212_BASE = {
    "live": "https://live.trading212.com/api/v0",
    "demo": "https://demo.trading212.com/api/v0",
}[T212_ENV]
T212_API_KEY = os.environ["T212_API_KEY"]
T212_API_SECRET = os.environ["T212_API_SECRET"]
T212_AUTH_HEADER = "Basic " + base64.b64encode(
    f"{T212_API_KEY}:{T212_API_SECRET}".encode()
).decode()

MCP_PUBLIC_URL = os.environ["MCP_PUBLIC_URL"].rstrip("/")
MCP_AUTH_PASSWORD = os.environ["MCP_AUTH_PASSWORD"]

oauth_provider = SingleUserOAuthProvider(password=MCP_AUTH_PASSWORD, public_url=MCP_PUBLIC_URL)

mcp = MCPServer(
    "t212-mcp",
    auth_server_provider=oauth_provider,
    auth=AuthSettings(
        issuer_url=MCP_PUBLIC_URL,
        resource_server_url=f"{MCP_PUBLIC_URL}/mcp",
        client_registration_options=ClientRegistrationOptions(enabled=True),
        revocation_options=RevocationOptions(enabled=True),
    ),
)
register_auth_routes(mcp, oauth_provider)


async def t212_get(path: str, params: dict | None = None) -> dict:
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{T212_BASE}{path}",
            headers={"Authorization": T212_AUTH_HEADER},
            params=params,
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()


@mcp.tool(description="Get account summary (cash, investment value, P&L, etc.)")
async def get_account_summary() -> dict:
    return await t212_get("/equity/account/summary")


@mcp.tool(description="Get the T212 account's current portfolio holdings")
async def get_portfolio() -> dict:
    return await t212_get("/equity/portfolio")


@mcp.tool(description="Get account cash balance and net value")
async def get_account_cash() -> dict:
    return await t212_get("/equity/account/cash")


@mcp.tool(description="Get order history")
async def get_order_history(limit: int = 20) -> dict:
    return await t212_get("/equity/history/orders", params={"limit": limit})


@mcp.tool(description="Get dividend payout history")
async def get_dividends(limit: int = 20) -> dict:
    return await t212_get("/history/dividends", params={"limit": limit})


if __name__ == "__main__":
    mcp.run(
        transport="streamable-http",
        stateless_http=True,
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", 8000)),
    )