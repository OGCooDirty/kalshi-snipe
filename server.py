"""
Kalshi Trading MCP server — minimal, auditable, self-hosted.

Built for Josh's Claude Code agent. Every line is here for you to read.

What it does:
  - Connects to the Kalshi trade-api v2 (prediction markets).
  - Reads your RSA private key from a local file path (the key NEVER leaves
    this machine and is never embedded in this file or any config).
  - Signs each request locally (RSA-PSS / SHA-256) per Kalshi's spec.
  - Exposes read tools (balance, markets, orderbook, positions, orders) and
    two write tools (place_order, cancel_order).

Safety:
  - Defaults to the DEMO environment (fake money) unless KALSHI_ENV=prod.
  - place_order enforces a hard per-order spend cap (KALSHI_MAX_ORDER_USD,
    default $5) so a logic bug can't sink the whole balance into one trade.
  - You can only BUY/SELL contracts; there is no withdraw/transfer capability
    here at all (the API surface is trading-only).

Configuration (all via environment variables, set in the MCP config):
  KALSHI_ENV               "demo" (default) or "prod"
  KALSHI_API_KEY_ID        Your Kalshi API Key ID (a UUID; not secret)
  KALSHI_PRIVATE_KEY_PATH  Absolute path to your downloaded .pem private key
  KALSHI_MAX_ORDER_USD     Optional. Max $ per single order. Default "5".
"""

import os
import time
import base64
import datetime
import json

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from mcp.server.fastmcp import FastMCP

# --- Environment / configuration -------------------------------------------

ENV = os.environ.get("KALSHI_ENV", "demo").strip().lower()
API_KEY_ID = os.environ.get("KALSHI_API_KEY_ID", "").strip()
PRIVATE_KEY_PATH = os.environ.get("KALSHI_PRIVATE_KEY_PATH", "").strip()
MAX_ORDER_USD = float(os.environ.get("KALSHI_MAX_ORDER_USD", "5"))

# Base hosts. The signed path always includes the "/trade-api/v2" prefix and
# excludes the query string (per Kalshi's auth spec).
HOSTS = {
    "prod": "https://api.elections.kalshi.com",
    "demo": "https://demo-api.kalshi.co",
}
HOST = HOSTS["prod"] if ENV == "prod" else HOSTS["demo"]
API_PREFIX = "/trade-api/v2"

mcp = FastMCP("kalshi-trading")


# --- Auth ------------------------------------------------------------------

def _load_private_key() -> rsa.RSAPrivateKey:
    if not PRIVATE_KEY_PATH:
        raise RuntimeError("KALSHI_PRIVATE_KEY_PATH is not set.")
    if not os.path.exists(PRIVATE_KEY_PATH):
        raise RuntimeError(f"Private key file not found: {PRIVATE_KEY_PATH}")
    with open(PRIVATE_KEY_PATH, "rb") as f:
        return serialization.load_pem_private_key(f.read(), password=None)


def _sign(timestamp_ms: str, method: str, path: str) -> str:
    """Sign "{timestamp}{METHOD}{path}" with RSA-PSS/SHA-256, return base64."""
    key = _load_private_key()
    message = (timestamp_ms + method.upper() + path).encode("utf-8")
    signature = key.sign(
        message,
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.DIGEST_LENGTH,
        ),
        hashes.SHA256(),
    )
    return base64.b64encode(signature).decode("utf-8")


def _request(method: str, endpoint: str, params: dict | None = None,
             body: dict | None = None) -> dict:
    """Make a signed request. `endpoint` is the path AFTER /trade-api/v2."""
    if not API_KEY_ID:
        raise RuntimeError("KALSHI_API_KEY_ID is not set.")
    signed_path = API_PREFIX + endpoint            # what we sign (no query)
    url = HOST + signed_path                        # query added by httpx
    ts = str(int(time.time() * 1000))
    headers = {
        "KALSHI-ACCESS-KEY": API_KEY_ID,
        "KALSHI-ACCESS-TIMESTAMP": ts,
        "KALSHI-ACCESS-SIGNATURE": _sign(ts, method, signed_path),
        "Content-Type": "application/json",
    }
    with httpx.Client(timeout=20.0) as client:
        resp = client.request(method, url, headers=headers,
                              params=params, json=body)
    if resp.status_code >= 400:
        raise RuntimeError(f"Kalshi API {resp.status_code}: {resp.text}")
    return resp.json() if resp.content else {}


# --- Read tools ------------------------------------------------------------

@mcp.tool()
def whoami() -> str:
    """Report which environment (demo/prod) and account this server is using."""
    return json.dumps({
        "environment": ENV,
        "host": HOST,
        "api_key_id_set": bool(API_KEY_ID),
        "max_order_usd": MAX_ORDER_USD,
    })


@mcp.tool()
def get_balance() -> str:
    """Get the account's cash balance (in cents) and available funds."""
    return json.dumps(_request("GET", "/portfolio/balance"))


@mcp.tool()
def list_markets(status: str = "open", limit: int = 20,
                 event_ticker: str = "", series_ticker: str = "") -> str:
    """List markets. status: open|closed|settled. Optionally filter by
    event_ticker or series_ticker. Returns up to `limit` markets."""
    params = {"status": status, "limit": limit}
    if event_ticker:
        params["event_ticker"] = event_ticker
    if series_ticker:
        params["series_ticker"] = series_ticker
    return json.dumps(_request("GET", "/markets", params=params))


@mcp.tool()
def get_market(ticker: str) -> str:
    """Get a single market by its ticker."""
    return json.dumps(_request("GET", f"/markets/{ticker}"))


@mcp.tool()
def get_orderbook(ticker: str, depth: int = 10) -> str:
    """Get the orderbook (bids/asks) for a market ticker."""
    return json.dumps(_request("GET", f"/markets/{ticker}/orderbook",
                               params={"depth": depth}))


@mcp.tool()
def get_positions(limit: int = 50) -> str:
    """List current open positions."""
    return json.dumps(_request("GET", "/portfolio/positions",
                               params={"limit": limit}))


@mcp.tool()
def get_orders(status: str = "", limit: int = 50) -> str:
    """List orders. Optional status filter (resting|canceled|executed)."""
    params = {"limit": limit}
    if status:
        params["status"] = status
    return json.dumps(_request("GET", "/portfolio/orders", params=params))


# --- Write tools (guarded) -------------------------------------------------

@mcp.tool()
def place_order(ticker: str, action: str, side: str, count: int,
                price_cents: int, post_only: bool = True,
                client_order_id: str = "") -> str:
    """Place a limit order via Kalshi's V2 orders API (a resting maker order by
    default — earns $0 fees and qualifies for liquidity rewards).

    ticker:      market ticker (from list_markets/get_market)
    action:      "buy" or "sell"
    side:        "yes" or "no"  (which contract you want exposure to)
    count:       number of contracts (whole integer >= 1)
    price_cents: price in cents 1-99 for the chosen side
    post_only:   True (default) rejects the order if it would cross the book,
                 guaranteeing maker status. False allows taking liquidity.
    client_order_id: optional idempotency key

    Safety: rejected if estimated cost (count * price_cents / 100) exceeds
    KALSHI_MAX_ORDER_USD (currently ${max}).
    """
    action = action.lower().strip()
    side = side.lower().strip()
    if action not in ("buy", "sell"):
        return json.dumps({"error": "action must be 'buy' or 'sell'"})
    if side not in ("yes", "no"):
        return json.dumps({"error": "side must be 'yes' or 'no'"})
    if count < 1:
        return json.dumps({"error": "count must be >= 1"})
    if not (1 <= price_cents <= 99):
        return json.dumps({"error": "price_cents must be in 1..99"})

    # Hard spend cap — the core guardrail (defined-risk buy cost).
    est_cost_usd = (count * price_cents) / 100.0
    if est_cost_usd > MAX_ORDER_USD:
        return json.dumps({
            "error": "order blocked by safety cap",
            "estimated_cost_usd": est_cost_usd,
            "max_order_usd": MAX_ORDER_USD,
            "hint": "lower count/price or raise KALSHI_MAX_ORDER_USD in config",
        })

    # Kalshi V2 uses a single YES-denominated book: bid = buy YES, ask = sell
    # YES. Buying NO @ p == selling YES @ (1-p); selling NO @ p == buying YES @
    # (1-p). Translate the user's (action, side, price) into a book side/price.
    if side == "yes":
        book_price = price_cents
        book_side = "bid" if action == "buy" else "ask"
    else:  # no
        book_price = 100 - price_cents
        book_side = "ask" if action == "buy" else "bid"

    body = {
        "ticker": ticker,
        "side": book_side,
        "count": str(count),
        "price": f"{book_price / 100:.4f}",
        "time_in_force": "good_till_canceled",
        "self_trade_prevention_type": "taker_at_cross",
        "post_only": bool(post_only),
    }
    if client_order_id:
        body["client_order_id"] = client_order_id

    return json.dumps(_request("POST", "/portfolio/events/orders", body=body))


@mcp.tool()
def cancel_order(order_id: str) -> str:
    """Cancel a resting order by its Kalshi order_id (V2 endpoint)."""
    return json.dumps(_request("DELETE",
                               f"/portfolio/events/orders/{order_id}"))


if __name__ == "__main__":
    mcp.run()
