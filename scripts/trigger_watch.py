#!/usr/bin/env python3
"""Ask GitHub to run the gpu-deal-watcher workflow now.

GitHub's own schedule often runs this workflow only every few hours, so run
this script from your desktop's scheduler (Task Scheduler, launchd or cron)
every 10 minutes. The watcher itself still runs on GitHub, with the secrets
and de-dupe state already stored there.

Needs a fine-grained GitHub token with access to this repository only and
"Actions: Read and write" permission. The token is read from the
GPU_WATCHER_TOKEN environment variable, or from the file
~/.gpu-deal-watcher-token (first line). Never commit it.

Usage:
  python trigger_watch.py            # trigger once (for a scheduler)
  python trigger_watch.py --loop     # keep triggering every 10 minutes
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

REPO = os.environ.get("GPU_WATCHER_REPO", "kshah66/gpu-deal-watcher")
WORKFLOW = "watch.yml"
BRANCH = "main"
INTERVAL_MIN = 10
API = f"https://api.github.com/repos/{REPO}/actions/workflows/{WORKFLOW}"
TOKEN_FILE = Path.home() / ".gpu-deal-watcher-token"


def log(msg):
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}", flush=True)


def token():
    tok = os.environ.get("GPU_WATCHER_TOKEN", "").strip()
    if not tok and TOKEN_FILE.exists():
        lines = TOKEN_FILE.read_text().splitlines()
        tok = lines[0].strip() if lines else ""
    if not tok:
        sys.exit(f"No token: set GPU_WATCHER_TOKEN or put it in {TOKEN_FILE}")
    return tok


def call(method, url, tok, body=None):
    req = urllib.request.Request(
        url,
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={
            "Authorization": f"Bearer {tok}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "gpu-deal-watcher-trigger",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        raw = r.read()
        return r.status, json.loads(raw) if raw else None


def busy(tok):
    """True if a run is already queued or running (avoids piling up runs)."""
    for status in ("queued", "in_progress"):
        _, data = call("GET", f"{API}/runs?status={status}&per_page=1", tok)
        if data and data.get("total_count"):
            return True
    return False


def trigger_once():
    tok = token()
    try:
        if busy(tok):
            log("a run is already queued or running; skipping")
            return True
        status, _ = call("POST", f"{API}/dispatches", tok, {"ref": BRANCH})
        log(f"triggered (HTTP {status})")
        return True
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:300]
        hint = {
            401: "token is invalid or expired",
            403: "token lacks 'Actions: Read and write', or the workflow is disabled (e.g. after PURCHASED)",
            404: "repo/workflow not found, or the token can't see this repo",
            422: "workflow is disabled or the branch doesn't exist",
        }.get(e.code, "")
        log(f"FAILED HTTP {e.code} {hint}: {detail}")
    except (urllib.error.URLError, TimeoutError) as e:
        log(f"FAILED network error: {e}")
    return False


def main():
    if "--loop" in sys.argv:
        log(f"triggering every {INTERVAL_MIN} min; Ctrl+C to stop")
        while True:
            trigger_once()
            time.sleep(INTERVAL_MIN * 60)
    sys.exit(0 if trigger_once() else 1)


if __name__ == "__main__":
    main()
