# Kalshi Trading MCP (self-hosted, auditable)

A minimal Model Context Protocol server that lets the Claude agent read markets
and place/cancel trades on Kalshi. Built in-house instead of using an unvetted
third-party package, so every line is reviewable in `server.py`.

## Why this exists
There is no *official* Kalshi agentic-trading product (unlike Robinhood). The
community MCP servers are near-zero-adoption packages that you'd have to trust
with live trading keys. This server is ~250 lines of readable Python you control.

## Safety design
- **Defaults to the DEMO environment** (`KALSHI_ENV=demo`) — fake money, real
  markets. Switch to `prod` only deliberately.
- **Private key stays on disk.** The server reads it from a file path; the key
  is never embedded here or in any config, and is signed with locally.
- **Hard per-order spend cap** (`KALSHI_MAX_ORDER_USD`, default $5). An order is
  rejected before submission if its estimated cost exceeds the cap.
- **Trading-only surface.** No withdraw/transfer capability exists in this server.
- You cannot go negative buying Kalshi contracts: max loss per contract is what
  you paid (settles at $0 or $1).

## One-time setup
1. **Create a Kalshi API key** (Account & security → API Keys → Create key).
   Download the private key `.pem` (shown once) and save it, e.g.
   `C:\Users\joshc\.kalshi\kalshi_private_key.pem`. Copy the **API Key ID**.
   - For risk-free testing, do this on the **demo** site (demo.kalshi.co) first —
     it's a separate account with separate keys.
2. Dependencies are already installed in `.venv` (mcp, httpx, cryptography).
3. Add the config block below to the project `.mcp.json`, filling in your
   Key ID and the path to your `.pem`. Then restart Claude Code and authenticate
   is automatic (API-key based, no OAuth).

## Config block for `.mcp.json`
```json
"kalshi-trading": {
  "command": "C:\\Users\\joshc\\OneDrive\\Desktop\\Claude\\kalshi-mcp\\.venv\\Scripts\\python.exe",
  "args": ["C:\\Users\\joshc\\OneDrive\\Desktop\\Claude\\kalshi-mcp\\server.py"],
  "env": {
    "KALSHI_ENV": "demo",
    "KALSHI_API_KEY_ID": "<your-api-key-id>",
    "KALSHI_PRIVATE_KEY_PATH": "C:\\Users\\joshc\\.kalshi\\kalshi_private_key.pem",
    "KALSHI_MAX_ORDER_USD": "5"
  }
}
```

## Tools
Read: `whoami`, `get_balance`, `list_markets`, `get_market`, `get_orderbook`,
`get_positions`, `get_orders`.
Write: `place_order` (guarded by spend cap), `cancel_order`.

## API reference notes
- Base URLs: prod `https://api.elections.kalshi.com/trade-api/v2`,
  demo `https://demo-api.kalshi.co/trade-api/v2`.
- Auth: headers `KALSHI-ACCESS-KEY`, `KALSHI-ACCESS-TIMESTAMP` (ms),
  `KALSHI-ACCESS-SIGNATURE`. Signed string = `{ts}{METHOD}{path}` where path
  includes `/trade-api/v2` and excludes the query string. RSA-PSS / SHA-256.
- Prices are integer cents 1–99. Legacy `/portfolio/orders` create/cancel is
  slated for deprecation no earlier than 2026-05-06; migrate to
  `/portfolio/events/orders` eventually.
