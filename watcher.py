#!/usr/bin/env python3
"""GPU deal watcher -> Discord and/or ntfy push alerts.

Sources (each one optional, failures are reported, never faked):
  * r/buildapcsales  - Reddit OAuth API if REDDIT_CLIENT_ID/SECRET set, else public RSS
  * Slickdeals       - public search RSS (best effort)

Filters: target card, per-card price ceiling (optionally + sales tax),
new-only, no laptops/prebuilts, case fit (< MAX_LENGTH_MM), de-duplication.

Stdlib only. Run: python watcher.py   (or --dry-run to print instead of pushing,
--self-test to run the built-in filter tests).
"""
from __future__ import annotations

import base64
import html
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from pathlib import Path

# ----------------------------------------------------------------- config ---
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")          # pick a long random topic name
NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh")
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")  # Server Settings > Integrations > Webhooks
TAX_RATE = float(os.environ.get("TAX_RATE", "0"))      # e.g. 0.0825 -> ceilings include tax
MAX_LENGTH_MM = int(os.environ.get("MAX_LENGTH_MM", "350"))
MAX_AGE_DAYS = float(os.environ.get("MAX_AGE_DAYS", "3"))  # skip posts older than this (old deals are usually expired)
STATE_FILE = Path(os.environ.get("STATE_FILE", "state.json"))
DISCORD_USER_ID = os.environ.get("DISCORD_USER_ID", "").strip()  # @mention target for exceptional deals
DIGEST_HOUR_UTC = int(os.environ.get("DIGEST_HOUR_UTC") or 1)  # daily summary hour (UTC); 1 = 8 PM US Eastern
NEARBY_STORES = os.environ.get("NEARBY_STORES", "").strip()     # e.g. "Micro Center <store>, Best Buy <store>"
PURCHASED = os.environ.get("PURCHASED", "").strip()             # set once you buy: the watcher stops itself
USER_AGENT = "script:gpu-deal-watcher:v1.0 (personal use)"
# Reddit's API rules ask for "(by /u/<username>)" in the User-Agent. Read from a
# secret so the username isn't in the code, and sent only to Reddit.
_reddit_user = os.environ.get("REDDIT_USERNAME", "").strip().removeprefix("/u/").removeprefix("u/")
REDDIT_UA = f"script:gpu-deal-watcher:v1.0 (by /u/{_reddit_user})" if _reddit_user else USER_AGENT

# Alert ceilings ("max", compared WITH tax when TAX_RATE is set). Default top
# ceiling is $1,500. Lower ceilings on cheaper cards are "worth interrupting me"
# thresholds: e.g. the 5070 Ti already sits at ~$1,340-1,480, so alerting at
# $1,500 would just be noise - $1,300 means a real drop.
# "hot" (pre-tax listed price): at or below this, the alert is marked 🔥 and
# @mentions you, because deals that good sell out fast. Edit freely. Nvidia only.
CARDS = {
    "RTX 5090":    {"pattern": r"5090\b", "max": float(os.environ.get("MAX_5090", 1500)),
                    "hot": float(os.environ.get("HOT_5090", 1500))},
    "RTX 5080":    {"pattern": r"5080\b", "max": float(os.environ.get("MAX_5080", 1500)),
                    "hot": float(os.environ.get("HOT_5080", 1400))},
    "RTX 4080 Super": {"pattern": r"4080\s*-?\s*super\b", "max": float(os.environ.get("MAX_4080S", 1200)),
                    "hot": float(os.environ.get("HOT_4080S", 1000))},
    "RTX 5070 Ti": {"pattern": r"5070\s*-?\s*ti\b", "max": float(os.environ.get("MAX_5070TI", 1300)),
                    "hot": float(os.environ.get("HOT_5070TI", 1150))},
    # The plain RTX 5070 (12 GB) is deliberately not tracked: every card here has 16 GB+ of VRAM.
}
# Deals you can only get at a store, or only some stores.
IN_STORE = re.compile(r"in[\s-]?store|store pick[\s-]?up|pick[\s-]?up only|select (stores|locations)|"
                      r"\bymmv\b|micro ?center|\bb&m\b|while supplies last at", re.I)
# Any title that names an AMD/Intel card is dropped.
NON_NVIDIA = re.compile(r"radeon|\brx\s*\d{4}|\barc\s*[ab]\d{3}|\bxfx\b|sapphire|powercolor", re.I)

# Things we never want: used/refurb/open-box, systems, laptops.
EXCLUDE = re.compile(
    r"\brig\b|open[\s-]?box|refurb|renewed|\bused\b|pre[\s-]?owned|\bb-?stock\b|recertified|"
    r"laptop|notebook|prebuilt|pre-built|gaming pc|desktop pc|\bpc\b.*\b(ryzen|core|intel)\b|"
    r"powerspec|ibuypower|cyberpower|skytech|alienware|\bomen\b|\blegion\b|nitro \d0|\btower\b",
    re.I,
)

# Approximate card lengths in mm by product line (largest SKU of that line
# across 5070/5070 Ti/5080). Manufacturer specs vary by revision -
# treat as a first-pass filter and verify on the spec sheet before buying.
LENGTHS_MM = [
    (r"rog\s*astral", 358), (r"aorus\s*master", 360), (r"vanguard", 357),
    (r"suprim", 338), (r"gaming\s*trio", 338), (r"amp\s*extreme", 332),
    (r"tuf", 348), (r"gaming\s*oc|aero", 340), (r"pny|epic-?x", 329),
    (r"zotac.*solid|\bsolid\b", 329), (r"inspire", 330), (r"prime", 304),
    (r"eagle|windforce", 304), (r"ventus\s*3x|shadow\s*3x", 303),
    (r"founders", 304), (r"ventus\s*2x|twin\s*edge|2x", 240),
]

# --------------------------------------------------------------- helpers ---
def http(url: str, data: bytes | None = None, headers: dict | None = None, timeout=20) -> bytes:
    req = urllib.request.Request(url, data=data, headers={"User-Agent": USER_AGENT, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def classify(title: str) -> str | None:
    t = title.lower()
    for name, c in CARDS.items():
        if re.search(c["pattern"], t):
            return name
    return None


def parse_price(title: str) -> float | None:
    """Find the deal price in a title.
    buildapcsales: '[GPU] Foo 5070 Ti ($1,149.99 - $50 promo = $1,099.99)'
    DealNews:      'MSI RTX 5070 Ti 16GB Graphics Card for $1,199 + free shipping'
    Tom's HW:      'Save $180 on this RTX 5070 Ti, now $1,199 (was $1,379)'
    Use the last amount >= $200, skipping savings and old prices ('save $X', 'was $X', '$X off')."""
    amounts = []
    for m in re.finditer(r"\$\s?([\d,]+(?:\.\d{1,2})?)", title):
        before = title[max(0, m.start() - 12):m.start()].lower()
        after = title[m.end():m.end() + 5].lower()
        if re.search(r"(save|saving|was|list|reg\.?|regularly|msrp|from)\s*$", before) or re.match(r"\s*off\b", after):
            continue
        amounts.append(float(m.group(1).replace(",", "")))
    amounts = [a for a in amounts if a >= 200]
    return amounts[-1] if amounts else None


def card_length(title: str) -> int | None:
    t = title.lower()
    for pat, mm in LENGTHS_MM:
        if re.search(pat, t):
            return mm
    return None


def evaluate(title: str, price: float | None, ignore_ceiling=False) -> tuple[bool, str, dict]:
    """Return (alert?, reason, info). ignore_ceiling=True is used for price history:
    "a new, fitting card of this type at this price", whatever the price."""
    if EXCLUDE.search(title):
        return False, "excluded (used/open-box/system/laptop)", {}
    if NON_NVIDIA.search(title):
        return False, "not Nvidia", {}
    card = classify(title)
    if not card:
        return False, "not a target card", {}
    if price is None:
        return False, "no price found", {"card": card}
    total = price * (1 + TAX_RATE)
    ceiling = CARDS[card]["max"]
    if total > ceiling and not ignore_ceiling:
        return False, f"{card} ${total:,.0f} > ${ceiling:,.0f}", {"card": card}
    length = card_length(title)
    if length and length > MAX_LENGTH_MM:
        return False, f"too long ({length}mm)", {"card": card}
    return True, "match", {"card": card, "price": price, "total": total, "length": length,
                           "hot": price <= CARDS[card]["hot"], "in_store": bool(IN_STORE.search(title))}


# --------------------------------------------------------------- sources ---
def reddit_posts() -> list[dict]:
    ua = {"User-Agent": REDDIT_UA}
    cid, secret = os.environ.get("REDDIT_CLIENT_ID"), os.environ.get("REDDIT_CLIENT_SECRET")
    if cid and secret:
        auth = base64.b64encode(f"{cid}:{secret}".encode()).decode()
        tok = json.loads(http("https://www.reddit.com/api/v1/access_token",
                              data=b"grant_type=client_credentials",
                              headers={**ua, "Authorization": f"Basic {auth}"}))["access_token"]
        raw = json.loads(http("https://oauth.reddit.com/r/buildapcsales/new?limit=100&raw_json=1",
                              headers={**ua, "Authorization": f"bearer {tok}"}))
        out = []
        for ch in raw["data"]["children"]:
            d = ch["data"]
            out.append({"id": "reddit:" + d["id"], "title": d["title"],
                        "url": d.get("url_overridden_by_dest") or d["url"],
                        "thread": "https://www.reddit.com" + d["permalink"],
                        "expired": d.get("link_flair_text", "") and "expired" in d.get("link_flair_text", "").lower(),
                        "published": d.get("created_utc")})
        return out
    # Unauthenticated fallbacks. Reddit often blocks or 404s cloud/datacenter IPs,
    # so try a few public endpoints and report each failure.
    errors = []
    for url in ("https://www.reddit.com/r/buildapcsales/new/.rss",
                "https://old.reddit.com/r/buildapcsales/new/.rss",
                "https://www.reddit.com/r/buildapcsales/new.json?limit=100&raw_json=1"):
        try:
            body = http(url, headers=ua)
        except Exception as e:
            errors.append(f"{urllib.parse.urlsplit(url).netloc}{urllib.parse.urlsplit(url).path}: {e}")
            continue
        label = f"{urllib.parse.urlsplit(url).netloc}{urllib.parse.urlsplit(url).path}"
        if body.lstrip()[:15].lower().startswith((b"<!doctype html", b"<html")):
            errors.append(f"{label}: got an HTML page, not a feed (likely Reddit's block page)")
            continue
        if url.endswith("raw_json=1") or b'"kind"' in body[:50]:
            return [{"id": "reddit:" + c["data"]["id"], "title": c["data"]["title"],
                     "url": c["data"].get("url_overridden_by_dest") or c["data"]["url"],
                     "thread": "https://www.reddit.com" + c["data"]["permalink"], "expired": False,
                     "published": c["data"].get("created_utc")}
                    for c in json.loads(body)["data"]["children"]]
        try:
            root = ET.fromstring(body)
        except ET.ParseError as e:
            errors.append(f"{label}: unparseable response ({e}): {body[:80]!r}")
            continue
        ns = {"a": "http://www.w3.org/2005/Atom"}
        out = []
        for e in root.findall("a:entry", ns):
            link = e.find("a:link", ns).get("href")
            out.append({"id": "reddit:" + e.find("a:id", ns).text, "title": html.unescape(e.find("a:title", ns).text),
                        "url": link, "thread": link, "expired": False,
                        "published": _iso_to_epoch(e.findtext("a:published", None, ns) or e.findtext("a:updated", None, ns))})
        print(f"reddit: using {urllib.parse.urlsplit(url).netloc}{urllib.parse.urlsplit(url).path}")
        return out
    raise RuntimeError("all public Reddit endpoints failed: " + " | ".join(errors))


def _rfc822_to_epoch(v: str | None) -> float | None:
    try:
        return parsedate_to_datetime(v.strip()).timestamp() if v else None
    except (TypeError, ValueError, IndexError):
        return None


def _iso_to_epoch(v: str | None) -> float | None:
    from datetime import datetime
    try:
        return datetime.fromisoformat(v.strip().replace("Z", "+00:00")).timestamp() if v else None
    except ValueError:
        return None


def rss_items(source: str, url: str) -> list[dict]:
    root = ET.fromstring(http(url))
    out = []
    for it in root.iter("item"):
        link = (it.findtext("link") or "").strip()
        guid = (it.findtext("guid") or link).strip()
        title = html.unescape((it.findtext("title") or "").strip())
        out.append({"id": f"{source}:{guid.split('?')[0]}", "title": title,
                    "url": link, "thread": link, "expired": False,
                    "published": _rfc822_to_epoch(it.findtext("pubDate"))})
    return out


def slickdeals_items() -> list[dict]:
    out = []
    for q in ("rtx 5070 ti", "rtx 5080", "rtx 5090"):
        out += rss_items("slickdeals", "https://slickdeals.net/newsearch.php?mode=frontpage&searcharea=deals"
                                       "&searchin=first&rss=1&q=" + urllib.parse.quote(q))
    return out


def tomshardware_items() -> list[dict]:
    # Editorial deal posts, e.g. "This MSI RTX 5070 Ti hits a new low price of $729"
    return rss_items("tomshardware", "https://www.tomshardware.com/feeds/articletype/deals")


def dealnews_items() -> list[dict]:
    # "X for $Y + free shipping" style titles; all-deals feed plus the Computers category.
    return (rss_items("dealnews", "https://www.dealnews.com/?rss=1")
            + rss_items("dealnews", "https://www.dealnews.com/c39/Computers/?rss=1"))


SOURCES = {"reddit": reddit_posts, "slickdeals": slickdeals_items,
           "tomshardware": tomshardware_items, "dealnews": dealnews_items}


# ------------------------------------------------------------ notify/state ---
def notify(title: str, body: str, url: str, priority: str = "high", tags: str = "rotating_light", dry=False,
           mention=False):
    """Send to every configured channel (Discord webhook and/or ntfy). Prints if none.
    mention=True @mentions you on Discord (DISCORD_USER_ID, else @everyone) and sends ntfy as urgent."""
    if dry or not (NTFY_TOPIC or DISCORD_WEBHOOK_URL):
        print(f"[ALERT] {title}\n  {body}\n  {url}")
        return
    errors = []
    if DISCORD_WEBHOOK_URL:
        warn = tags == "warning"
        icon = {"warning": "⚠️ ", "brain": "🧠 ", "fire": "🔥 ", "bar_chart": "📊 ", "tada": "🎉 "}.get(tags, "🚨 ")
        ping, allowed = "", {"parse": []}
        if mention:
            if DISCORD_USER_ID:
                ping, allowed = f"<@{DISCORD_USER_ID}> ", {"users": [DISCORD_USER_ID]}
            else:
                ping, allowed = "@everyone ", {"parse": ["everyone"]}
        payload = {
            "username": "GPU Deal Watcher",
            # Plain-text content is what shows in the phone push; the embed adds a clickable title.
            "content": ping + icon + title[:200],
            "embeds": [{"title": title[:256], "url": url, "description": body[:4000],
                        "color": 0xF0B232 if warn else {"brain": 0x5865F2, "fire": 0xED4245,
                                                        "bar_chart": 0x99AAB5}.get(tags, 0x76B900)}],
            "allowed_mentions": allowed,
        }
        try:
            http(DISCORD_WEBHOOK_URL, data=json.dumps(payload).encode(),
                 headers={"Content-Type": "application/json"})
        except Exception as e:
            errors.append(f"discord: {type(e).__name__}")
    if NTFY_TOPIC:
        ascii_ = lambda s: s.encode("ascii", "ignore").decode()
        try:
            http(f"{NTFY_SERVER}/{NTFY_TOPIC}", data=body.encode(),
                 headers={"Title": ascii_(title)[:250], "Click": url, "Tags": tags,
                          "Priority": "urgent" if mention else priority})
        except Exception as e:
            errors.append(f"ntfy: {type(e).__name__}")
    if errors:  # never print the webhook URL - it is a secret
        print("notify failed: " + ", ".join(errors), file=sys.stderr)


def claude_review(new: list[dict], recent: list[dict], dry=False, label=""):
    """Optional: ask Claude to check the new deals' pages and recommend the best buy.
    Needs ANTHROPIC_API_KEY and the `anthropic` package; skipped quietly otherwise."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("claude review: skipped (no ANTHROPIC_API_KEY)")
        return
    try:
        import claude_summary
        settings = {"ceilings": {k: v["max"] for k, v in CARDS.items()}, "tax_rate": TAX_RATE,
                    "max_length_mm": MAX_LENGTH_MM, "nearby_stores": NEARBY_STORES}
        text = claude_summary.review(new, recent, settings, fetch=http)
    except Exception as e:  # never let the review break alerting
        print(f"claude review: FAILED {type(e).__name__}: {str(e)[:300]}", file=sys.stderr)
        return
    if text:
        notify(f"{label}Claude's take on the new deal(s)", text, new[0]["url"],
               priority="default", tags="brain", dry=dry)
        print("claude review: sent")


def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {"seen": {}, "errors": {}}


def save_state(s: dict):
    cutoff = time.time() - 14 * 86400
    s["seen"] = {k: v for k, v in s["seen"].items() if v > cutoff}
    week = time.time() - 7 * 86400
    s["price_log"] = dict(sorted(((k, v) for k, v in s.get("price_log", {}).items() if v["ts"] > week),
                                 key=lambda kv: kv[1]["ts"])[-500:])
    STATE_FILE.write_text(json.dumps(s, indent=1))


def _price_context(plog: dict, card: str, item_id: str, price: float, now: float) -> str:
    """Compare a deal with the lowest price seen for the same card in the feeds this week."""
    week = [e["price"] for k, e in plog.items()
            if e["card"] == card and k != item_id and now - e["ts"] < 7 * 86400]
    if not week:
        return f"Price context: first {card} seen in the feeds this week"
    low = min(week)
    if price < low:
        return f"Price context: ${low - price:,.0f} below this week's low (${low:,.2f}) 📉"
    if price == low:
        return f"Price context: ties this week's low (${low:,.2f})"
    return f"Price context: ${price - low:,.0f} above this week's low (${low:,.2f})"


def main(dry=False):
    state = load_state()
    now = time.time()
    if PURCHASED:
        finish_after_purchase(state, dry=dry)
        return
    seeded = set(state.setdefault("seeded", []))
    if state["seen"] and not seeded:  # state from before per-source seeding existed
        seeded = {"slickdeals"}  # the only source that had succeeded before
    stats = state.setdefault("stats", {"since": now, "runs": 0, "alerts": 0, "ok": {}, "failed": {}})
    stats["runs"] += 1
    plog = state.setdefault("price_log", {})
    alerts = 0
    new_matches: list[dict] = []
    for name, fn in SOURCES.items():
        try:
            items = fn()
            state["errors"].pop(name, None)
            stats["ok"][name] = stats["ok"].get(name, 0) + 1
            dated = [i["published"] for i in items if i.get("published")]
            old = sum(1 for t in dated if now - t > MAX_AGE_DAYS * 86400)
            print(f"{name}: {len(items)} items ({len(dated)} dated, {old} older than {MAX_AGE_DAYS:g} days)")
        except Exception as e:
            print(f"{name}: FAILED {type(e).__name__}: {e}", file=sys.stderr)
            stats["failed"][name] = stats["failed"].get(name, 0) + 1
            err = state["errors"].setdefault(name, {"count": 0, "notified": 0})
            err["count"] += 1
            # Tell the user once a day if a source keeps failing - don't silently go blind.
            if err["count"] >= 3 and now - err["notified"] > 86400:
                notify(f"GPU watcher: {name} source failing",
                       f"{name} failed {err['count']} runs in a row: {type(e).__name__}: {str(e)[:200]}",
                       "https://github.com", priority="default", tags="warning", dry=dry)
                err["notified"] = now
            continue
        for it in items:
            fresh = not (it.get("published") and now - it["published"] > MAX_AGE_DAYS * 86400)
            price = parse_price(it["title"])
            # Price history: every fresh, new, fitting target card at any price (deduped by post).
            if fresh and it["id"] not in plog and not it.get("expired"):
                ok_any, _, info_any = evaluate(it["title"], price, ignore_ceiling=True)
                if ok_any:
                    plog[it["id"]] = {"card": info_any["card"], "price": price, "ts": it.get("published") or now,
                                      "title": it["title"][:200], "thread": it["thread"], "source": name}
            if it["id"] in state["seen"] or it.get("expired"):
                continue
            state["seen"][it["id"]] = now
            if not fresh:
                continue  # old post: the deal has most likely expired
            ok, why, info = evaluate(it["title"], price)
            if not ok:
                continue
            if name not in seeded:
                continue  # seed the de-dupe list on first run instead of spamming old posts
            length = f"~{info['length']}mm" if info["length"] else f"length unknown - verify <{MAX_LENGTH_MM}mm"
            tax = f" (~${info['total']:,.0f} w/ tax)" if TAX_RATE else ""
            lines = [it["title"], f"{length} | via {name}",
                     _price_context(plog, info["card"], it["id"], info["price"], now)]
            if info["in_store"]:
                lines.append("🏬 In-store / limited locations - check stock at your local store"
                             + (f" ({NEARBY_STORES})" if NEARBY_STORES else ""))
            lines.append(f"Thread: {it['thread']}")
            title = (("HOT DEAL: " if info["hot"] else "") + f"{info['card']} ${info['price']:,.2f}{tax}"
                     + (" [in-store]" if info["in_store"] else ""))
            notify(title, "\n".join(lines), it["url"], dry=dry,
                   tags="fire" if info["hot"] else "rotating_light", mention=info["hot"])
            alerts += 1
            stats["alerts"] += 1
            new_matches.append({**info, "title": it["title"], "url": it["url"], "thread": it["thread"],
                                "source": name, "alerted_at": now})
        seeded.add(name)  # after the first successful fetch, new items alert normally
    state["seeded"] = sorted(seeded)
    # Keep a week of alerted deals so Claude can compare new ones against recent ones.
    recent = [d for d in state.get("recent_alerts", []) if now - d["alerted_at"] < 7 * 86400]
    if new_matches:
        claude_review(new_matches, recent, dry=dry)
    state["recent_alerts"] = (recent + new_matches)[-30:]
    maybe_send_digest(state, now, dry=dry)
    save_state(state)
    print(f"done: {alerts} alert(s)")


def maybe_send_digest(state: dict, now: float, dry=False, force=False):
    """Once a day (first run at/after DIGEST_HOUR_UTC): proof of life + cheapest prices seen."""
    t = time.gmtime(now)
    today = time.strftime("%Y-%m-%d", t)
    if not force and (t.tm_hour < DIGEST_HOUR_UTC or state.get("digest_date") == today):
        return
    stats = state.get("stats", {})
    hours = (now - stats.get("since", now)) / 3600
    lines = [f"Last {hours:.0f}h: {stats.get('runs', 0)} runs, {stats.get('alerts', 0)} deal alert(s)."]
    names = sorted(set(stats.get("ok", {})) | set(stats.get("failed", {})) | set(SOURCES))
    src = []
    for n in names:
        good, bad = stats.get("ok", {}).get(n, 0), stats.get("failed", {}).get(n, 0)
        src.append(f"{n} ✅" if bad == 0 and good else f"{n} ❌ failed {bad}/{good + bad}" if bad else f"{n} -")
    lines.append("Sources: " + ", ".join(src))
    lines.append("")
    lines.append("Cheapest new, fitting card seen in the feeds (last 24h, any price, not re-checked):")
    plog = state.get("price_log", {})
    for card in CARDS:
        day = [e for e in plog.values() if e["card"] == card and now - e["ts"] < 86400]
        if not day:
            lines.append(f"• {card}: none seen")
            continue
        e = min(day, key=lambda x: x["price"])
        tax = f" (~${e['price'] * (1 + TAX_RATE):,.0f} w/ tax)" if TAX_RATE else ""
        lines.append(f"• {card}: ${e['price']:,.2f}{tax} - {e['title'][:90]} {e['thread']}")
    notify(f"Daily GPU watcher summary - {today}", "\n".join(lines), "https://github.com",
           priority="low", tags="bar_chart", dry=dry)
    state["digest_date"] = today
    state["stats"] = {"since": now, "runs": 0, "alerts": 0, "ok": {}, "failed": {}}
    print("digest: sent")


def finish_after_purchase(state: dict, dry=False):
    """PURCHASED is set: say goodbye once, then disable this workflow so it stops running."""
    if not state.get("purchase_notified"):
        notify("GPU watcher stopped - you bought a GPU",
               f"Purchased: {PURCHASED}\nNo more deal alerts. The watcher has disabled its own workflow.\n"
               "To restart it: delete the PURCHASED secret, then Actions > gpu-deal-watcher > Enable workflow.",
               "https://github.com", priority="default", tags="tada", dry=dry)
        state["purchase_notified"] = True
        save_state(state)
    repo, token = os.environ.get("GITHUB_REPOSITORY"), os.environ.get("GITHUB_TOKEN")
    if not (repo and token):
        print("purchased: not running in GitHub Actions, so the workflow was not disabled")
        return
    req = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/actions/workflows/watch.yml/disable", data=b"", method="PUT",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28", "User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            print(f"purchased: workflow disabled (HTTP {r.status})")
    except Exception as e:
        print(f"purchased: could not disable the workflow ({e}); disable it in the Actions tab", file=sys.stderr)


def test_review(max_deals=3):
    """Run Claude's review on the newest deals in the feeds that pass the filters,
    ignoring the de-dupe list and the age limit. Doesn't touch saved state."""
    now = time.time()
    matches = []
    for name, fn in SOURCES.items():
        try:
            items = fn()
        except Exception as e:
            print(f"{name}: FAILED {type(e).__name__}")
            continue
        for it in items:
            if any(m["thread"] == it["thread"] for m in matches):
                continue  # same post found by two searches
            ok, _, info = evaluate(it["title"], parse_price(it["title"]))
            if ok:
                matches.append({**info, "title": it["title"], "url": it["url"], "thread": it["thread"],
                                "source": name, "alerted_at": it.get("published") or now})
    matches.sort(key=lambda d: d["alerted_at"], reverse=True)
    print(f"test review: {len(matches)} matching deals in feeds; reviewing newest {min(max_deals, len(matches))}")
    for d in matches[:max_deals]:
        print(f"  - {d['card']} ${d['price']:,.2f} via {d['source']}")
    if matches:
        recent = [d for d in matches[max_deals:] if now - d["alerted_at"] < MAX_AGE_DAYS * 86400][:5]
        claude_review(matches[:max_deals], recent, label="TEST: ")


# ---------------------------------------------------------------- tests ---
def self_test():
    cases = [
        ("[GPU] MSI Shadow 3X OC RTX 5070 Ti 16GB ($1,249.99)", True),
        ("[GPU] PNY RTX 5070 Ti OC ($1,379.99)", False),                          # over $1,300
        ("[GPU] ASUS ROG Astral RTX 5080 ($1,399.99)", False),                    # too long
        ("[GPU] Gigabyte RTX 5080 Windforce ($1,449.99)", True),
        ("[GPU] MSI RTX 5080 Gaming Trio ($2,199.99)", False),                    # over $1,500
        ("[GPU] Gigabyte RTX 5070 Ti Gaming OC - Open Box ($999)", False),        # open box
        ("[Prebuilt] iBuyPower Ryzen 7 RTX 5080 ($1,499)", False),                # prebuilt
        ("[GPU] PowerColor Hellhound RX 9070 XT ($649.99)", False),               # AMD
        ("[GPU] Zotac RTX 5070 Twin Edge ($749.99)", False),                      # 12 GB 5070: not tracked
        ("[GPU] MSI RTX 5070 Ti Ventus 3X ($1,199.99)", True),
        ("[Laptop] MSI Vector RTX 5080 ($1,499)", False),
        ("[GPU] RTX 5090 Founders Edition ($1,999)", False),                      # over budget
        # DealNews / Tom's Hardware title formats
        ("Gigabyte GeForce RTX 5070 Ti Windforce 16GB Graphics Card for $1,149 + free shipping", True),
        ("Open-Box MSI RTX 5080 Ventus 3X for $1,099 + free shipping", False),     # open box
        ("Save $180 on this MSI RTX 5070 Ti Shadow 3X, now $1,199 (was $1,379)", True),
        ("This RTX 5080 is $200 off at Newegg, now $1,999", False),               # over budget
        ("RTX 5070 Ti gaming laptop drops to $1,299 at Walmart", False),          # laptop
        ("Save $1440 on this 4K gaming rig with an RTX 5070 Ti", False),          # rig/no price
    ]
    bad = 0
    for title, want in cases:
        got, why, _ = evaluate(title, parse_price(title))
        mark = "ok " if got == want else "BAD"
        bad += got != want
        print(f"{mark} {got!s:5} {why:40} {title}")
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        self_test()
    if "--test-review" in sys.argv:
        test_review()
        sys.exit(0)
    main(dry="--dry-run" in sys.argv)
