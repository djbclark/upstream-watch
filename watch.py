#!/usr/bin/env python3
"""Off-machine backstop for fleet-watch's upstream watcher: ping Telegram when anyone else acts on
the operator's upstream issues and pull requests.

fleet-watch (on the Mac) is the primary path: it reads GitHub notifications every 5 min and relays
each event into the registered agent session. It is blind while the Mac sleeps, and it reads unread
notifications only, so a thread read on github.com before it looks is missed. This runs every
15 min on GitHub Actions in a public repo (free minutes), from GitHub search (`involves:<self>`, so
every future PR and issue is covered), and only pings: it has no way to start work, and the event
text it quotes is a third party's. Its logs are public: they print counts, never the ping text.

State (JSON, carried between runs in the Actions cache): `seen` maps event key -> timestamp. With
no state the first run seeds `seen` and pings nothing. Each run looks back LOOKBACK hours, so a
skipped or late scheduled run loses nothing; `seen` dedupes the overlap. An event whose ping fails
stays unseen and is retried next run (the job then fails, which GitHub emails about).

Env: GH_TOKEN, TELEGRAM_BOT_TOKEN, TELEGRAM_TARGET (chat:thread), WATCH_SELF, WATCH_REPOS,
WATCH_IGNORE, WATCH_STATE (path), WATCH_DRY=1 (print instead of pinging, state untouched),
WATCH_TEST=1 (send one test ping and stop).
"""
import datetime as dt
import json
import os
import sys
import urllib.parse
import urllib.request

API = "https://api.github.com"
LOOKBACK = 6  # hours
KEEP = 21  # days of seen keys


def env(name, default=None):
    v = os.environ.get(name, default)
    if v is None:
        sys.exit(f"watch: {name} is not set")
    return v


def gh(path, params=None):
    """All pages of a GitHub REST list (or the one object), as a list."""
    url = path if path.startswith("http") else f"{API}/{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    out = []
    while url:
        req = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {env('GH_TOKEN')}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28"})
        with urllib.request.urlopen(req, timeout=30) as r:
            body = json.load(r)
            link = r.headers.get("Link", "")
        if isinstance(body, dict) and "items" in body:
            out += body["items"]
        elif isinstance(body, list):
            out += body
        else:
            return [body]
        url = next((p.split(";")[0].strip(" <>") for p in link.split(",") if 'rel="next"' in p), None)
    return out


def events(item, self_login, ignore):
    """Every action on ITEM (a search hit) by someone other than the operator, bots and IGNORE."""
    repo = item["repository_url"].split("/repos/", 1)[1]
    n = item["number"]
    is_pr = "pull_request" in item
    url = item["html_url"]
    raw = []
    for c in gh(f"repos/{repo}/issues/{n}/comments", {"per_page": 100}):
        raw.append((f"ic:{c['id']}", c["user"], c["created_at"], "comment", c.get("body") or ""))
    if is_pr:
        for r in gh(f"repos/{repo}/pulls/{n}/reviews", {"per_page": 100}):
            if r.get("submitted_at"):
                raw.append((f"review:{r['id']}", r["user"], r["submitted_at"],
                            f"review {r['state'].lower()}", r.get("body") or ""))
        for c in gh(f"repos/{repo}/pulls/{n}/comments", {"per_page": 100}):
            raw.append((f"rc:{c['id']}", c["user"], c["created_at"], "inline comment", c.get("body") or ""))
    if item.get("state") == "closed" and item.get("closed_at"):
        merged = bool((item.get("pull_request") or {}).get("merged_at"))
        who = {"login": "?", "type": "User"}
        if is_pr:
            p = gh(f"repos/{repo}/pulls/{n}")[0]
            who = p.get("merged_by") or who
        if who["login"] == "?":
            who = (gh(f"repos/{repo}/issues/{n}")[0].get("closed_by")) or who
        raw.append((f"closed:{item['closed_at']}", who, item["closed_at"],
                    "merged" if merged else "closed", item.get("title") or ""))
    out = []
    for key, user, at, kind, text in raw:
        user = user or {"login": "ghost", "type": "User"}
        if user["login"] == self_login or user.get("type") == "Bot" or user["login"] in ignore:
            continue
        out.append({"key": f"{repo}#{n}:{key}", "url": url, "ref": f"{repo}#{n}", "at": at,
                    "kind": kind, "login": user["login"], "text": " ".join(text.split())[:120]})
    return out


def ping(text):
    chat, _, thread = env("TELEGRAM_TARGET").partition(":")
    data = {"chat_id": chat, "text": text, "disable_web_page_preview": "true"}
    if thread:
        data["message_thread_id"] = thread
    req = urllib.request.Request(f"https://api.telegram.org/bot{env('TELEGRAM_BOT_TOKEN')}/sendMessage",
                                 data=urllib.parse.urlencode(data).encode())
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r).get("ok") is True
    except Exception as e:  # the error text names no secret: urllib reports status and reason only
        print(f"watch: telegram send failed: {type(e).__name__}: {getattr(e, 'code', '')}", file=sys.stderr)
        return False


def main():
    self_login = env("WATCH_SELF")
    repos = env("WATCH_REPOS").split()
    ignore = set(env("WATCH_IGNORE", "").split())
    path = env("WATCH_STATE")
    dry = os.environ.get("WATCH_DRY") == "1"
    if os.environ.get("WATCH_TEST") == "1":
        ok = ping("upstream-watch (GitHub Actions backstop): test ping, the upstream watcher can reach you")
        print(f"watch: test ping {'sent' if ok else 'FAILED'}")
        return 0 if ok else 1
    now = dt.datetime.now(dt.timezone.utc)
    try:
        with open(path) as f:
            state = json.load(f)
    except (OSError, ValueError):
        state = None
    seeding = state is None
    seen = {} if seeding else state.get("seen", {})

    since = (now - dt.timedelta(hours=LOOKBACK)).strftime("%Y-%m-%dT%H:%M:%SZ")
    q = " ".join([f"involves:{self_login}", *(f"repo:{r}" for r in repos), f"updated:>={since}"])
    items = gh("search/issues", {"q": q, "per_page": 100, "sort": "updated"})
    # Only events inside the window: an item updated now still carries months of older comments.
    fresh = [e for it in items for e in events(it, self_login, ignore)
             if e["at"] >= since and e["key"] not in seen]
    fresh.sort(key=lambda e: e["at"])
    failed = 0
    for e in fresh:
        if seeding:
            seen[e["key"]] = e["at"]
            continue
        line = (f"upstream-watch (GitHub Actions backstop): {e['url']} {e['kind']} by {e['login']}"
                f" at {e['at']}: {e['text']}")
        if dry:
            print("would ping:", e["url"], e["kind"], "by", e["login"], "at", e["at"])  # no text: logs are public
        elif ping(line):
            seen[e["key"]] = e["at"]
        else:
            failed += 1
    print(f"watch: {len(items)} item(s) updated since {since}, {len(fresh)} new event(s)"
          f"{' (seeded, nothing pinged)' if seeding else ''}, {failed} ping failure(s)")
    if dry:
        return 0
    cutoff = (now - dt.timedelta(days=KEEP)).strftime("%Y-%m-%dT%H:%M:%SZ")
    seen = {k: v for k, v in seen.items() if v >= cutoff}
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path + ".new", "w") as f:
        json.dump({"version": 1, "updated_at": now.isoformat(), "seen": seen}, f, indent=1, sort_keys=True)
    os.replace(path + ".new", path)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
