"""
Autonomous news-latency SNIPE bot for the Kalshi WC-halftime market.

The one edge the research endorsed: a market resolves YES when a performer is
"announced." So this bot, each run:
  1. Pulls credible news for newly-confirmed halftime performers.
  2. Matches confirmed names against still-open Kalshi performer markets.
  3. BUYS YES on any freshly-confirmed performer still trading cheap, before the
     thin market reprices toward $1.00 — taker order, for speed.

It is deliberately conservative (credible sources only, word-boundary name
match, in-context, price + exposure caps, dedupe) and bounded:
  - Per-snipe cap, total-exposure cap, and a balance kill switch.
  - Defined risk: buying YES contracts can't go negative.

Runs unattended on a timer (Windows Task Scheduler). Uses server.py for signed
V2 orders. Config via env (set in run_snipe.bat):
  KALSHI_ENV / KALSHI_API_KEY_ID / KALSHI_PRIVATE_KEY_PATH  (auth, as server.py)
  SNIPE_EVENT        event ticker (default KXWORLDCUPHALFTIME-26)
  SNIPE_USD          $ per snipe (default 4)
  SNIPE_MAX_EXPOSURE total $ this bot may ever deploy (default 15)
  SNIPE_FLOOR        kill switch: do nothing if balance < this (default 20)
"""
import os, re, json, time, html
import httpx
import server

# Multi-target config: the snipe edge works on ANY announcement-driven,
# named-entity market (a roster of named candidates resolved by a discrete
# public announcement). Add one dict per event. `context` = words that must
# appear in a news item for it to count (keeps cross-topic false-matches out);
# `query` = the news search. NOT for number/date/price markets — name-matching
# only works where outcomes are named entities.
TARGETS = [
    # Entertainment
    {"event": "KXWORLDCUPHALFTIME-26", "context": ["halftime", "world cup"],
     "query": '%22world+cup%22+halftime+(perform+OR+lineup+OR+headline)'},
    # MLB — run-up to the July 31 trade deadline (heavy news flow)
    {"event": "KXNEXTTEAMMLB-27TSKUBAL", "context": ["skubal"],
     "query": 'Tarik+Skubal+(trade+OR+traded)'},
    {"event": "KXNEXTTEAMMLB-27RDEVERS", "context": ["devers"],
     "query": 'Rafael+Devers+(trade+OR+traded)'},
    {"event": "KXNEXTTEAMMLB-27SALCANTARA", "context": ["alcantara"],
     "query": 'Sandy+Alcantara+(trade+OR+traded)'},
    {"event": "KXNEXTTEAMMLB-27MTROUT", "context": ["mike trout"],
     "query": 'Mike+Trout+(trade+OR+traded)'},
    # NBA — free-agency / trade season
    {"event": "KXNEXTTEAMNBA-26GANT", "context": ["giannis"],
     "query": 'Giannis+Antetokounmpo+(trade+OR+traded+OR+signs)'},
    {"event": "KXNEXTTEAMNBA-26LJAM", "context": ["lebron"],
     "query": 'LeBron+James+(trade+OR+traded+OR+signs)'},
    {"event": "KXNEXTTEAMNBA-26JBROWN7", "context": ["jaylen brown"],
     "query": 'Jaylen+Brown+(trade+OR+traded+OR+signs)'},
    {"event": "KXNEXTTEAMNBA-26KDURANT7", "context": ["kevin durant", "durant"],
     "query": 'Kevin+Durant+(trade+OR+traded+OR+signs)'},
]
PER_SNIPE_USD = float(os.environ.get("SNIPE_USD", "4"))
MAX_EXPOSURE = float(os.environ.get("SNIPE_MAX_EXPOSURE", "15"))
FLOOR = float(os.environ.get("SNIPE_FLOOR", "20"))
PRICE_HI, PRICE_LO = 0.90, 0.02            # only snipe in this ask band
DRY_RUN = os.environ.get("SNIPE_DRY_RUN") == "1"   # log instead of placing

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_F = os.path.join(HERE, "snipe_state.json")
LOG_F = os.path.join(HERE, "snipe.log")

CREDIBLE = ["fifa.com", "reuters.com", "apnews.com", "billboard.com", "cnn.com",
            "espn.com", "bbc.", "variety.com", "si.com", "rollingstone.com",
            "nytimes.com", "theguardian.com", "globalcitizen.org", "pitchfork.com",
            "ew.com", "people.com", "nbcnews.com", "cbsnews.com", "abcnews"]
# Strong CONFIRMATION phrases only (not bare words that match questions/rumors).
CONFIRM = [
    # performers / events
    "will perform at", "to perform at", "set to perform", "confirmed to",
    "announced as", "joins the lineup", "added to the lineup", "will headline",
    "to headline", "will take the stage", "named as a performer",
    "officially announced",
    # sports trades / signings
    "traded to", "has been traded", "signs with", "signed with", "re-signs with",
    "acquired by", "dealt to", "agreed to a deal", "agrees to a deal",
    "agreed to terms with", "lands with",
    # hires
    "named head coach", "hired as", "named manager", "introduced as",
    "named the new",
    # castings
    "cast as", "to star as", "joins the cast", "has been cast",
]
# Per-target context words live in TARGETS now; this stays empty/global-unused.
NEGATIVE = ["?", "odds", "betting", "could ", "might ", "rumor", "speculat",
            "potential", "possibl", "who will", "predict", "candidate",
            "contender", "favorite", "fans want", "petition", "kalshi",
            "polymarket", "wish", "hope", "expected to", "reportedly",
            "in talks", "rumored", "linked", "trade talks", "interest in",
            "in the mix", "pursuing", "targeting", "wants to", "sweepstakes",
            "suitors", "could land", "would be"]


def log(m):
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {m}"
    print(line, flush=True)
    try:
        open(LOG_F, "a", encoding="utf-8").write(line + "\n")
    except Exception:
        pass


def load_state():
    try:
        return json.load(open(STATE_F, encoding="utf-8"))
    except Exception:
        return {"bought": [], "spent": 0.0}


def save_state(s):
    json.dump(s, open(STATE_F, "w", encoding="utf-8"))


def get_news_items(query):
    """Return list of lowercased news 'item' blobs (title+desc+source)."""
    url = f"https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en"
    try:
        r = httpx.get(url, timeout=20, follow_redirects=True,
                      headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code != 200:
            log(f"news fetch HTTP {r.status_code}")
            return []
    except Exception as e:
        log(f"news fetch error {e}")
        return []
    items = re.findall(r"<item>(.*?)</item>", r.text, re.S | re.I)
    return [html.unescape(it).lower() for it in items]


def open_markets(event):
    out, cursor = [], ""
    for _ in range(5):
        p = {"event_ticker": event, "status": "open", "limit": 200}
        if cursor:
            p["cursor"] = cursor
        d = server._request("GET", "/markets", params=p)
        out += d.get("markets", [])
        cursor = d.get("cursor", "")
        if not cursor:
            break
    return out


def confirmed_in_news(name, items, context):
    """True if `name` is confirmed by a credible, in-context news item."""
    pat = re.compile(r"\b" + re.escape(name.lower()) + r"\b")
    for it in items:
        if any(neg in it for neg in NEGATIVE):
            continue                      # speculation/odds article — ignore
        if (pat.search(it)
                and any(c in it for c in context)
                and any(c in it for c in CONFIRM)
                and any(d in it for d in CREDIBLE)):
            return True
    return False


def main():
    bal = float(server._request("GET", "/portfolio/balance").get("balance", 0)) / 100.0
    if bal < FLOOR:
        log(f"KILL SWITCH: balance ${bal:.2f} < floor ${FLOOR:.2f}. Idle.")
        return
    st = load_state()
    if st.get("spent", 0) >= MAX_EXPOSURE:
        log(f"exposure cap ${MAX_EXPOSURE} reached; idle.")
        return

    for tgt in TARGETS:
        if st.get("spent", 0) >= MAX_EXPOSURE:
            break
        items = get_news_items(tgt["query"])
        if not items:
            log(f"{tgt['event']}: no news; skip")
            continue
        markets = open_markets(tgt["event"])
        log(f"{tgt['event']}: scanning {len(markets)} markets vs {len(items)} news items")
        for m in markets:
            tk = m.get("ticker", "")
            name = (m.get("yes_sub_title") or "").strip()
            if not name or tk in st["bought"] or len(name) < 4:
                continue
            if not confirmed_in_news(name, items, tgt["context"]):
                continue
            ya = float(m.get("yes_ask_dollars") or 0)
            if not (PRICE_LO <= ya <= PRICE_HI):
                log(f"CONFIRMED {name} but ask {ya} out of band; skip")
                continue
            if st["spent"] + PER_SNIPE_USD > MAX_EXPOSURE:
                log("would exceed exposure cap; stopping")
                break
            cnt = max(1, int(PER_SNIPE_USD / ya))
            cents = int(round(ya * 100))
            if DRY_RUN:
                log(f"[DRY] WOULD SNIPE {name} [{tgt['event']}]: {cnt} YES @ {cents}c")
                continue
            try:
                r = server.place_order(tk, "buy", "yes", cnt, cents, post_only=False)
                log(f"SNIPE {name} [{tgt['event']}]: bought {cnt} YES @ {cents}c -> {r}")
                st["bought"].append(tk)
                st["spent"] = st.get("spent", 0) + cnt * ya
                save_state(st)
            except Exception as e:
                log(f"order error {name}: {e}")
    log("run complete")


if __name__ == "__main__":
    main()
