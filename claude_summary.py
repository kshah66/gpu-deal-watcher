"""Claude's take on new GPU deal alerts.

For each newly alerted deal, fetch its deal page (Slickdeals thread, DealNews or
Tom's Hardware article), then ask Claude whether it still looks live, what the
catches are, and which recent deal is the best buy right now. The result is a
short plain-text message for Discord/ntfy.

Needs the `anthropic` package and ANTHROPIC_API_KEY. Model is configurable with
CLAUDE_MODEL (default claude-opus-5-5).
"""
from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from html.parser import HTMLParser

import anthropic

MODEL = os.environ.get("CLAUDE_MODEL") or "claude-opus-5-5"
# Server-side refusal fallback is supported on these model families; Haiku has none.
_FALLBACK_MODELS = ("claude-opus-5", "claude-fable-5", "claude-sonnet-5-5")
PAGE_CHARS = 12000  # per page; keeps each review to roughly 10-15K input tokens

SYSTEM = """You review graphics card deals for one shopper and reply with a short Discord message.

The shopper wants a NEW (not open-box/refurbished/used) Nvidia desktop card, an RTX 5070 Ti or better with at least 16 GB of VRAM
(the 12 GB RTX 5070 is ruled out),
must fit a case with a maximum card length (given below), and compares prices including sales tax.

For each NEW deal you are given the post title and an excerpt of its deal page. Using only that evidence:
- Say whether it still looks live. Signs it is dead: "expired", "sold out", "out of stock", "no longer available",
  or comments saying the price is gone. If the page excerpt is missing or unclear, say "couldn't verify".
- Name the catches that matter: membership required (e.g. Costco), in-store pickup only or limited locations,
  mobile-app-only price, rebate or promo code needed, per-customer limits, a seller other than the retailer.
- Note the exact card model and, if you can tell, whether it is a long or thick card.

Then pick the single best buy right now. Only recommend a deal whose page shows it is live. Recent deals were not
re-checked: mention one only if it was alerted in the last 48 hours, and label it "unverified". Never recommend a
deal older than that. If nothing qualifies, say "Best buy now: none confirmed live - keep waiting" and stop.

Format: plain text with Discord markdown, no headings, at most 1200 characters.
One line per new deal starting with ✅ (looks live), ❌ (looks dead) or ⚠️ (live with a catch / unverified),
then a line starting with "Best buy now:"."""


class _TextExtractor(HTMLParser):
    """Pull readable text out of a page, skipping scripts, styles and navigation."""
    _SKIP = {"script", "style", "noscript", "svg", "nav", "footer", "header", "form"}

    def __init__(self):
        super().__init__()
        self.parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skip_depth += 1

    def handle_endtag(self, tag):
        if tag in self._SKIP and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data):
        if not self._skip_depth and data.strip():
            self.parts.append(data.strip())


def page_text(raw: bytes) -> str:
    p = _TextExtractor()
    p.feed(raw.decode("utf-8", "replace"))
    return re.sub(r"\s+", " ", " ".join(p.parts))


def _excerpt(url: str, fetch) -> str:
    try:
        text = page_text(fetch(url))
    except Exception as e:
        return f"(page could not be fetched: {type(e).__name__})"
    if len(text) > PAGE_CHARS:
        return text[:PAGE_CHARS] + " ... (excerpt; rest of page omitted)"
    return text or "(page had no readable text)"


def _when(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def build_prompt(new: list[dict], recent: list[dict], settings: dict, fetch) -> str:
    tax = settings["tax_rate"]
    lines = [
        f"Now: {_when(datetime.now(timezone.utc).timestamp())}",
        f"Sales tax rate: {tax:.5f}" if tax else "Sales tax: not set (prices are pre-tax)",
        f"Max card length: {settings['max_length_mm']} mm",
        (f"Stores the shopper can visit: {settings['nearby_stores']}. For in-store or limited-location deals, "
         "say whether the page or comments suggest stock at one of these stores."
         if settings.get("nearby_stores") else "Nearby stores: not given."),
        "Price ceilings incl. tax: " + ", ".join(f"{k} ${v:,.0f}" for k, v in settings["ceilings"].items()),
        "",
        "NEW deals (just alerted):",
    ]
    for i, d in enumerate(new, 1):
        length = f"~{d['length']}mm" if d.get("length") else "length unknown"
        lines += [
            f"<deal {i}>",
            f"Title: {d['title']}",
            f"Card: {d['card']} | price ${d['price']:,.2f} (~${d['total']:,.0f} with tax) | {length} | via {d['source']}"
            + (" | in-store/limited locations" if d.get("in_store") else ""),
            f"Link: {d['thread']}",
            f"Page excerpt: {_excerpt(d['thread'], fetch)}",
            f"</deal {i}>",
        ]
    if recent:
        lines += ["", "RECENT deals already alerted in the past week (NOT re-checked; may have expired):"]
        for d in recent[-10:]:
            lines.append(f"- {_when(d['alerted_at'])}: {d['card']} ${d['price']:,.2f} - {d['title']} ({d['thread']})")
    return "\n".join(lines)


def review(new: list[dict], recent: list[dict], settings: dict, fetch) -> str | None:
    client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY
    params = dict(
        model=MODEL,
        max_tokens=16000,
        system=SYSTEM,
        output_config={"effort": "low"},  # a short summarization task; raise to "medium" for more care
        messages=[{"role": "user", "content": build_prompt(new, recent, settings, fetch)}],
    )
    if MODEL.startswith(_FALLBACK_MODELS):
        # If a safety classifier declines, the API retries on a suitable fallback model in the same call.
        response = client.beta.messages.create(
            betas=["server-side-fallback-2026-07-01"], fallbacks="default", **params)
    else:
        response = client.messages.create(**params)

    if response.stop_reason == "refusal":
        category = response.stop_details.category if response.stop_details else None
        print(f"claude review: model declined (category={category}, request {response._request_id})")
        return None
    text = "\n".join(b.text for b in response.content if b.type == "text").strip()
    if response.stop_reason == "max_tokens":
        text += "\n(cut off)"
    usage = response.usage
    print(f"claude review: {usage.input_tokens} input / {usage.output_tokens} output tokens ({response.model})")
    return text or None
