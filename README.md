# GPU deal watcher (through Cyber Monday)

Every 10 minutes it checks Slickdeals, DealNews, Tom's Hardware's deals feed, and r/buildapcsales (when Reddit API keys are set).
It keeps only new (not used or open-box) Nvidia cards with at least 16 GB of VRAM (RTX 5070 Ti, 5080, 5090, and new-stock 4080 Super; the 12 GB RTX 5070 is not tracked) that are under the price ceiling and under 350 mm long.
It skips laptops and prebuilts, de-duplicates, adds price context (vs. the week's lowest price seen), flags in-store-only deals, marks exceptional deals 🔥 with an @mention, posts a daily 📊 summary, and pushes alerts to your phone via a Discord webhook and/or ntfy.

## Setup (about 15 minutes)

1. **Pick a push channel** (one or both):
   - **Discord:** in a server you own, open the channel's settings → Integrations → Webhooks → New Webhook → Copy Webhook URL.
     Treat the URL like a password: anyone who has it can post to that channel.
     For phone pushes, set that channel's notifications to "All Messages" in the Discord app.
   - **ntfy:** install the ntfy app and subscribe to a long, random topic, e.g. `gpu-7f3k9q2x-deals`.
     Anyone who knows the topic name can read it, so don't use a guessable name.
2. **Repo:** create a new **public** GitHub repo (public repos get free, unlimited Actions minutes).
   Push `watcher.py` and `.github/workflows/watch.yml` to it.
3. **Secrets** (Settings → Secrets and variables → Actions):
   - `DISCORD_WEBHOOK_URL` and/or `NTFY_TOPIC` (at least one is required)
   - `REDDIT_CLIENT_ID` and `REDDIT_CLIENT_SECRET` (strongly recommended).
     Create a "script" app at https://www.reddit.com/prefs/apps.
     Without them, the watcher falls back to Reddit's public RSS, which Reddit often blocks from GitHub's servers.
     Reddit may also require you to request API access first; if it does, use the Slickdeals fallbacks below.
   - `ANTHROPIC_API_KEY` (optional): turns on **Claude's take**. When new deals match, the watcher opens each deal page and asks Claude whether it still looks live, what the catches are (membership, in-store only, promo codes), and which recent deal is the best buy right now, then posts that as a 🧠 message. Create a key at https://console.anthropic.com (API usage is billed separately from a Claude subscription). Default model `claude-opus-5-5` at low effort costs roughly $0.10-0.15 per review; it only runs when a deal matches. Set the Actions **variable** `CLAUDE_MODEL` to `claude-haiku-5-5` for a much cheaper (less careful) review.
   - `DISCORD_USER_ID` (optional): your Discord user ID, so 🔥 hot deals @mention you (Discord: Settings > Advanced > Developer Mode, then right-click your name > Copy User ID). Without it, hot deals ping `@everyone`.
   - `NEARBY_STORES` (optional): stores you can get to, e.g. `Micro Center <town>, Best Buy <town>`. Shown on in-store deals and passed to Claude's review. A secret, so your area stays out of the public logs.
   - `PURCHASED` (add it **after you buy**): what you bought, e.g. `MSI RTX 5070 Ti Shadow 3X, $1,379.99`. On its next run the watcher posts a final message and disables its own workflow. To restart, delete the secret and re-enable the workflow in the Actions tab.
   - `REDDIT_USERNAME` (optional, e.g. `yourname` without `/u/`). Reddit's API rules ask for it in the User-Agent; it's only sent to Reddit.
   - `TAX_RATE` (optional **secret**, e.g. `0.0825`) so the ceilings include sales tax. Use a secret, not a variable: variable values show up in public run logs.
4. Go to Actions → gpu-deal-watcher → **Run workflow** once.
   The first run quietly records what's already posted, so you won't get a flood of old deals.
   Check the log: each source should report `N items`.
5. **Test the push:**
   - Discord: `curl -H "Content-Type: application/json" -d '{"content":"test"}' "<webhook-url>"`
   - ntfy: `curl -d "test" ntfy.sh/<your-topic>`
6. **After Nov 30:** disable the workflow (Actions → ⋯ → Disable).

If a source fails 3 runs in a row, you get one warning push per day.
That way you know the watcher has a blind spot instead of assuming it's quiet.

## Tuning

| Env var | Default | Meaning |
|---|---|---|
| `MAX_5080` | 1500 | max price to alert on (5080s sell for ~$2,199+ as of Nov 2026) |
| `MAX_5090` | 1500 | effectively never fires; kept in case of a pricing error |
| `MAX_4080S` | 1200 | only if new old stock shows up |
| `MAX_5070TI` | 1300 | it's at ~$1,340-1,480 now; $1,300 or less is a real drop |
| `HOT_5070TI` / `HOT_5080` / `HOT_4080S` / `HOT_5090` | 1150 / 1400 / 1000 / 1500 | listed price (pre-tax) at or below which an alert is 🔥 and @mentions you |
| `DIGEST_HOUR_UTC` (variable) | 1 | hour for the daily 📊 summary (1 UTC = 8 PM US Eastern) |
| `MAX_AGE_DAYS` | 3 | skip posts older than this; old deals are usually expired |
| `MAX_LENGTH_MM` | 350 | max card length to alert on; set to your case clearance |

The card lengths in `LENGTHS_MM` are approximate per product line.
Always check the spec sheet. Unknown models still alert, marked "length unknown".

## What it can't monitor reliably (and what to use instead)

| Source | Status | Alternative |
|---|---|---|
| r/buildapcsales | Public feeds block GitHub's servers (404/403); works only with approved Reddit API keys | MonitoRSS Discord bot (may be blocked too); Slickdeals usually picks up the same deals |
| Best Buy | API keys aren't issued to free or .edu email addresses | Covered by Slickdeals; save the cards you want in the Best Buy app |
| Amazon | Blocks scraping (bot checks) | Keepa or camelcamelcamel price-drop alerts on 2–3 specific listings |
| Newegg | Blocks bots (Akamai) | Covered by Slickdeals; Newegg's own price alerts also work |
| Walmart | Blocks bots (PerimeterX) | Covered by Slickdeals |
| Micro Center | No API, in-store pricing | Check the weekly ad and the Black Friday page manually on Nov 26–27 |
| B&H | Blocks bots | Covered by Slickdeals |
| Tom's Hardware, DealNews | Work (RSS); a newly added source records its existing posts first, so you only get new deals |
| Neowin, Ben's Bargains, Newegg RSS | Block GitHub's servers (403) or are mostly news | Not used |
| Nvidia Founders Edition stock API | Responds, but US product codes couldn't be confirmed | Not used; check nvidia.com/Best Buy for FE drops |
| Slickdeals | Works (search RSS) | The Slickdeals app's "Deal Alerts" for "RTX 5070 Ti" and "RTX 5080" is a zero-code backup |

`python watcher.py --self-test` runs the filter tests.
`python watcher.py --dry-run` prints alerts instead of pushing them.
