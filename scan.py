"""Surface RESEARCH TARGETS for the autonomous agent: liquid, single
(non-parlay) markets resolving soon, priced in the contested band (not
priced-in near-certainties). These are where research can find an edge.
Sorted by 24h volume desc."""
import os, datetime
import server

def fp(x):
    try:
        return float(x)
    except Exception:
        return 0.0

now = datetime.datetime.now(datetime.timezone.utc)
MAX_DAYS = int(os.environ.get("SCAN_DAYS", "7"))
PAGES = int(os.environ.get("SCAN_PAGES", "6"))
MIN_VOL = float(os.environ.get("SCAN_MINVOL", "1000"))

def hours_to_close(s):
    try:
        dt = datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
        return (dt - now).total_seconds() / 3600.0
    except Exception:
        return 1e9

targets = []
cursor = ""
seen = 0
for page in range(PAGES):                       # hard cap — cannot run away
    params = {"status": "open", "limit": 200}
    if cursor:
        params["cursor"] = cursor
    data = server._request("GET", "/markets", params=params)
    markets = data.get("markets", [])
    seen += len(markets)
    print(f"[page {page+1}/{PAGES}: {len(markets)}, total {seen}]", flush=True)
    for m in markets:
        tkr = m.get("ticker", "")
        if "KXMVE" in tkr:
            continue
        h = hours_to_close(m.get("close_time", ""))
        if h <= 1 or h > MAX_DAYS * 24:
            continue
        vol = fp(m.get("volume_24h_fp")) or fp(m.get("volume_fp"))
        if vol < MIN_VOL:
            continue
        ya = fp(m.get("yes_ask_dollars")); yb = fp(m.get("yes_bid_dollars"))
        if not (0.05 <= ya <= 0.95):            # contested / researchable band
            continue
        title = (m.get("title") or "")[:78]
        targets.append((vol, h, yb, ya, tkr, title))
    cursor = data.get("cursor", "")
    if not cursor:
        break

targets.sort(reverse=True)                       # most liquid first
print(f"\n{len(targets)} research targets (single, <{MAX_DAYS}d, vol>={MIN_VOL:.0f}, 5-95c)\n")
print(f"{'VOL24h':>8} {'HRS':>5} {'YESbid':>6} {'YESask':>6}  TICKER")
for vol, h, yb, ya, tkr, title in targets[:25]:
    print(f"{vol:8.0f} {h:5.0f} {yb:6.2f} {ya:6.2f}  {tkr}")
    print(f"{'':22}{title}")
