"""Discover announcement-driven, named-entity EVENTS via the /events endpoint
(one entry per event, no parlay noise). Prints matching event tickers + titles."""
import server

KEYS = ["next team", "next head coach", "next manager", "next club", "next coach",
        "cast as", "cast", "cover athlete", "nominee", "next chancellor",
        "perform", "headline", "next host", "next president", "appointed",
        "next ceo", "ipo", "draft pick", "traded", "signs"]

events = {}
cursor = ""
seen = 0
for _ in range(20):
    p = {"status": "open", "limit": 200}
    if cursor:
        p["cursor"] = cursor
    d = server._request("GET", "/events", params=p)
    evs = d.get("events", [])
    seen += len(evs)
    for e in evs:
        ev = e.get("event_ticker", "")
        title = (e.get("title") or e.get("sub_title") or "")
        tl = title.lower()
        if ev and ev not in events and any(k in tl for k in KEYS):
            events[ev] = title[:75]
    cursor = d.get("cursor", "")
    if not cursor:
        break

print(f"scanned {seen} events, {len(events)} matches\n")
for ev, t in sorted(events.items()):
    print(f"{ev:42} {t}")
