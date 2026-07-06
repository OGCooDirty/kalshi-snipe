"""
Autonomous news-latency SNIPE bot for Kalshi announcement-driven markets.

Edge: a market resolves YES when an entity is "announced" (a player traded/
signed, a performer confirmed, an actor cast). When that news breaks, thin
Kalshi markets reprice slowly. This bot watches credible news and buys the
still-cheap YES before the market catches up.

Coverage:
  - TARGETS: hand-picked one-off events (e.g. WC halftime performers).
  - SERIES: whole categories auto-expanded — every open event in the series
    (e.g. all NBA "next team" markets) becomes a target, with the entity name
    derived from the event title. One broad news query per series is shared
    across its events (cached) to keep request counts sane.

Conservative by design: credible sources only, strong confirmation phrases,
rumor/speculation filter, word-boundary name match, price + exposure caps,
dedupe, balance kill switch. Defined risk (buying YES can't go negative).

Config via env: KALSHI_ENV/KALSHI_API_KEY_ID/KALSHI_PRIVATE_KEY_PATH (auth),
SNIPE_USD (default 4), SNIPE_MAX_EXPOSURE (default 15), SNIPE_FLOOR (default 20),
SNIPE_DRY_RUN=1 (log instead of placing).
"""
import os, re, json, time, html, datetime
from email.utils import parsedate_to_datetime
import httpx
import server

PER_SNIPE_USD = float(os.environ.get("SNIPE_USD", "4"))
MAX_EXPOSURE = float(os.environ.get("SNIPE_MAX_EXPOSURE", "15"))
FLOOR = float(os.environ.get("SNIPE_FLOOR", "20"))
PRICE_HI, PRICE_LO = 0.97, 0.10   # 10c floor: if market prices it a longshot, distrust the "confirmation"
MAX_NEWS_AGE_DAYS = float(os.environ.get("SNIPE_NEWS_AGE_DAYS", "4"))  # ignore stale news
DRY_RUN = os.environ.get("SNIPE_DRY_RUN") == "1"
MAX_EVENTS_PER_SERIES = int(os.environ.get("SNIPE_MAX_PER_SERIES", "40"))

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_F = os.path.join(HERE, "snipe_state.json")
LOG_F = os.path.join(HERE, "snipe.log")

# Hand-picked one-off events (distinctive context keyword each).
TARGETS = [
    {"event": "KXWORLDCUPHALFTIME-26", "context": ["halftime", "world cup"],
     "query": '%22world+cup%22+halftime+(perform+OR+lineup+OR+headline)'},
    # Castings
    {"event": "KXBOND-30", "context": ["james bond", "007"],
     "query": 'James+Bond+(cast+OR+casting+OR+actor)'},
    {"event": "KXPERFORMROLEMILESMORALES", "context": ["miles morales"],
     "query": 'Miles+Morales+(cast+OR+casting+OR+Spider-Man)'},
    {"event": "KXROLEINPRODUCTIONDOOMSDAY", "context": ["doomsday", "avengers"],
     "query": 'Avengers+Doomsday+cast'},
    # Event headliners / performers
    {"event": "KXROLEATEVENTCOACHELLA-27DEC31", "context": ["coachella"],
     "query": 'Coachella+2027+(headline+OR+lineup)'},
    {"event": "KXROLEATEVENTLOLLA-26DEC31", "context": ["lollapalooza"],
     "query": 'Lollapalooza+2026+(headline+OR+lineup)'},
    {"event": "KXROLEATEVENTROLLING-27DEC31", "context": ["rolling loud"],
     "query": 'Rolling+Loud+(headline+OR+lineup)'},
    {"event": "KXPERFORMVS-26", "context": ["victoria's secret"],
     "query": "Victoria%27s+Secret+Fashion+Show+(perform+OR+headline)"},
    # Corporate
    {"event": "KXNBA2KCOVER-27", "context": ["2k27", "nba 2k"],
     "query": 'NBA+2K27+cover+athlete'},
    {"event": "KXNEWROLEGS-35DEC", "context": ["goldman"],
     "query": 'Goldman+Sachs+CEO'},
    {"event": "KXNEWROLEX-27JAN", "context": ["ceo of x", "x ceo"],
     "query": 'CEO+of+X+(named+OR+appointed)'},
]

# Whole categories — every open event in the series becomes a target. `strip`
# is removed from the event title to get the entity name (used as context).
SERIES = [
    {"series": "KXNEXTTEAMNBA", "strip": "'s next team",
     "query": "NBA+(traded+OR+signs+OR+signing+OR+agrees)"},
    {"series": "KXNEXTTEAMMLB", "strip": "'s next team",
     "query": "MLB+(traded+OR+trade+OR+signs)"},
    {"series": "KXNEXTTEAMNFL", "strip": "'s next team",
     "query": "NFL+(traded+OR+signs+OR+released)"},
    {"series": "KXNEXTTEAMNHL", "strip": "'s next team",
     "query": "NHL+(traded+OR+signs)"},
    {"series": "KXJOINCLUB", "strip": ": next club",
     "query": "football+transfer+(signs+OR+completes+OR+joins)"},
    {"series": "KXJOINLEAGUE", "strip": ": next club (league)",
     "query": "football+transfer+(signs+OR+completes+OR+joins)"},
    {"series": "KXWNBANEXTTEAM", "strip": "'s next team",
     "query": "WNBA+(traded+OR+signs+OR+signing)"},
]

CREDIBLE = ["fifa.com", "reuters.com", "apnews.com", "billboard.com", "cnn.com",
            "espn.com", "bbc.", "variety.com", "si.com", "rollingstone.com",
            "nytimes.com", "theguardian.com", "globalcitizen.org", "pitchfork.com",
            "ew.com", "people.com", "nbcnews.com", "cbsnews.com", "abcnews",
            "theathletic.com", "bleacherreport.com", "skysports.com", "nba.com",
            "mlb.com", "nfl.com", "nhl.com", "athletic", "yahoo.com",
            "foxsports.com", "cbssports.com", "usatoday.com"]

CONFIRM = [
    # performers / events
    "will perform at", "to perform at", "set to perform", "confirmed to",
    "announced as", "joins the lineup", "added to the lineup", "will headline",
    "to headline", "will take the stage", "named as a performer",
    "officially announced",
    # trades / signings (US sports)
    "traded to", "has been traded", "signs with", "signed with", "re-signs with",
    "acquired by", "dealt to", "agreed to a deal", "agrees to a deal",
    "agreed to terms with", "lands with", "claimed by",
    # soccer transfers
    "signs for", "signed for", "completes move to", "completes a move to",
    "completes transfer", "officially joins", "unveiled as", "joins on a",
    "completes the signing", "completes signing of",
    # hires / appointments
    "named head coach", "hired as", "named manager", "introduced as",
    "named the new", "appointed as", "appointed ceo", "named ceo",
    "named as the next", "will become the next", "to lead as",
    # castings
    "cast as", "to star as", "joins the cast", "has been cast",
]
NEGATIVE = ["odds", "betting", "could ", "might ", "rumor", "rumour",
            "speculat", "potential", "possibl", "who will", "predict",
            "candidate", "contender", "favorite", "fans want", "petition",
            "kalshi", "polymarket", "wish", "hope", "expected to", "reportedly",
            "in talks", "rumored", "linked", "trade talks", "interest in",
            "in the mix", "pursuing", "targeting", "wants to", "sweepstakes",
            "suitors", "could land", "would be", "bid for", "loan", "eyeing",
            "monitoring", "transfer target", "close to", "advanced talks",
            "edging closer", "set to", "agent", "weighing", "mock "]

_news_cache = {}


def log(msg):
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
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
    if query in _news_cache:
        return _news_cache[query]
    url = f"https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en"
    items = []
    try:
        r = httpx.get(url, timeout=20, follow_redirects=True,
                      headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code == 200:
            # Keep only recent items (kills stale/old articles). Strip <link>
            # URLs — Google's redirect URLs contain "?" + base64 that pollute
            # matching. Keep title/description/source (source has the domain).
            now = datetime.datetime.now(datetime.timezone.utc)
            for it in re.findall(r"<item>(.*?)</item>", r.text, re.S | re.I):
                m = re.search(r"<pubDate>(.*?)</pubDate>", it, re.I)
                if m:
                    try:
                        age = (now - parsedate_to_datetime(m.group(1))).total_seconds() / 86400
                        if age > MAX_NEWS_AGE_DAYS:
                            continue
                    except Exception:
                        pass
                items.append(html.unescape(
                    re.sub(r"<link>.*?</link>", " ", it, flags=re.S | re.I)).lower())
        else:
            log(f"news HTTP {r.status_code} for {query[:40]}")
    except Exception as e:
        log(f"news error {query[:40]}: {e}")
    _news_cache[query] = items
    return items


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


def expand_series():
    """Turn each SERIES into a list of targets (one per open event)."""
    targets = []
    for s in SERIES:
        try:
            evs = server._request("GET", "/events", params={
                "series_ticker": s["series"], "status": "open", "limit": 200,
            }).get("events", [])
        except Exception as e:
            log(f"series {s['series']} error: {e}")
            continue
        for e in evs[:MAX_EVENTS_PER_SERIES]:
            title = (e.get("title") or "").lower().strip()
            name = title.replace(s["strip"], "").strip()
            if len(name) < 4:
                continue
            targets.append({"event": e.get("event_ticker"), "context": [name],
                            "query": s["query"]})
    return targets


def confirmed_in_news(name, items, context):
    pat = re.compile(r"\b" + re.escape(name.lower()) + r"\b")
    for it in items:
        if any(neg in it for neg in NEGATIVE):
            continue
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

    all_targets = TARGETS + expand_series()
    log(f"covering {len(all_targets)} markets")
    for tgt in all_targets:
        if st.get("spent", 0) >= MAX_EXPOSURE:
            break
        items = get_news_items(tgt["query"])
        if not items:
            continue
        try:
            markets = open_markets(tgt["event"])
        except Exception as e:
            log(f"{tgt['event']}: markets error {e}")
            continue
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
                log("exposure cap; stopping")
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
