"""Fetch r/FantasyPL discussion as grounding input for team selection.

Two backends, picked automatically:

  * **rss** (default, no setup) — Reddit still serves public Atom feeds at
    `/r/<sub>/<sort>.rss` and `<permalink>.rss`. No account, no app, no
    credentials. Rate limits are strict, so requests are paced and retried.
  * **oauth** (optional) — used when REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET
    are set. Higher limits, and adds scores + comment counts that RSS omits.
    Create a free "script" app at https://www.reddit.com/prefs/apps.

Note the plain `.json` endpoints are NOT usable: Reddit returns a 403 HTML
block page for unauthenticated clients regardless of User-Agent.
"""

from __future__ import annotations

import html
import json
import logging
import os
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
OAUTH_API = "https://oauth.reddit.com"
WWW = "https://www.reddit.com"
# Reddit blocks obvious bot agents on the RSS endpoints.
BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
              "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
API_UA = "macos:fpl-league-scraper:v0.2 (personal research script)"
ATOM = {"a": "http://www.w3.org/2005/Atom"}
TIMEOUT = 30.0
RSS_DELAY = 3.0        # seconds between RSS requests; below ~2s Reddit 429s

log = logging.getLogger(__name__)


class RedditError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# shared helpers
# --------------------------------------------------------------------------

def _clean(raw: str | None) -> str:
    if not raw:
        return ""
    txt = html.unescape(re.sub(r"<[^>]+>", " ", raw))
    return re.sub(r"\s+", " ", txt).strip()


def _is_link_only(body: str) -> bool:
    """RSS renders link posts as just 'submitted by /u/x [link] [comments]'."""
    return bool(re.fullmatch(r"submitted by\s+/u/\S+\s*(\[link\])?\s*(\[comments\])?",
                             body.strip(), re.I))


# --------------------------------------------------------------------------
# RSS backend (no credentials)
# --------------------------------------------------------------------------

def _rss_get(client: httpx.Client, url: str, params: dict | None = None,
             attempts: int = 5) -> str:
    """GET with pacing + exponential backoff; Reddit 429s aggressively."""
    for attempt in range(attempts):
        r = client.get(url, params=params, headers={"User-Agent": BROWSER_UA})
        if r.status_code == 429:
            wait = RSS_DELAY * (2 ** attempt)
            log.warning("429 from Reddit, backing off %.0fs (attempt %d/%d)",
                        wait, attempt + 1, attempts)
            time.sleep(wait)
            continue
        if r.status_code == 403:
            raise RedditError(
                "Reddit returned 403 for the RSS feed. This usually clears on "
                "its own; if it persists, set REDDIT_CLIENT_ID/SECRET to use "
                "the OAuth backend instead.")
        r.raise_for_status()
        return r.text
    raise RedditError(f"Rate-limited by Reddit after {attempts} attempts on {url}. "
                      "Try again in a few minutes, or use the OAuth backend.")


def _parse_entries(xml_text: str) -> list[ET.Element]:
    try:
        return ET.fromstring(xml_text).findall("a:entry", ATOM)
    except ET.ParseError as e:
        raise RedditError(f"Could not parse Reddit RSS ({e}). The feed may have "
                          "returned an HTML block page.") from e


def rss_posts(client: httpx.Client, sub: str, sort: str, period: str,
              limit: int) -> list[dict]:
    params = {"limit": min(limit, 100)}
    if sort == "top":
        params["t"] = period
    xml_text = _rss_get(client, f"{WWW}/r/{sub}/{sort}.rss", params)
    posts = []
    for rank, e in enumerate(_parse_entries(xml_text)[:limit], 1):
        link = e.find("a:link", ATOM)
        author = e.find("a:author/a:name", ATOM)
        content = e.find("a:content", ATOM)
        title = e.find("a:title", ATOM)
        published = e.find("a:published", ATOM) or e.find("a:updated", ATOM)
        url = link.get("href") if link is not None else ""
        body = _clean(content.text if content is not None else "")
        m = re.search(r"/comments/([a-z0-9]+)/", url)
        posts.append({
            "id": m.group(1) if m else f"rank{rank}",
            "rank": rank,                       # feed order == Reddit's ranking
            "title": _clean(title.text if title is not None else ""),
            "author": author.text if author is not None else None,
            "created": published.text if published is not None else None,
            "url": url,
            "text": "" if _is_link_only(body) else body[:4000],
            "score": None,                      # not exposed via RSS
            "num_comments": None,
            "flair": None,
        })
    return posts


def rss_comments(client: httpx.Client, post_url: str, limit: int) -> list[dict]:
    xml_text = _rss_get(client, post_url.rstrip("/") + "/.rss",
                        {"sort": "top", "limit": limit})
    out = []
    for e in _parse_entries(xml_text):
        author = e.find("a:author/a:name", ATOM)
        if author is not None and author.text == "/u/AutoModerator":
            continue
        body = _clean(e.find("a:content", ATOM).text
                      if e.find("a:content", ATOM) is not None else "")
        if body:
            out.append({"score": None, "body": body[:1500],
                        "author": author.text if author is not None else None})
    # RSS returns the submission itself as the first entry; drop it.
    return out[1:limit + 1]


# --------------------------------------------------------------------------
# OAuth backend (optional, richer)
# --------------------------------------------------------------------------

def get_token(client: httpx.Client) -> str:
    cid = os.environ.get("REDDIT_CLIENT_ID")
    secret = os.environ.get("REDDIT_CLIENT_SECRET")
    if not cid or not secret:
        raise RedditError("REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET not set")
    data: dict[str, Any] = {"grant_type": "client_credentials", "scope": "read"}
    user, pw = os.environ.get("REDDIT_USERNAME"), os.environ.get("REDDIT_PASSWORD")
    if user and pw:
        data = {"grant_type": "password", "username": user, "password": pw}
    r = client.post(TOKEN_URL, data=data, auth=(cid, secret),
                    headers={"User-Agent": API_UA})
    if r.status_code == 401:
        raise RedditError("Reddit rejected the credentials (401). Check the id/"
                          "secret and that the app type is 'script'.")
    r.raise_for_status()
    tok = r.json().get("access_token")
    if not tok:
        raise RedditError(f"No access_token in response: {r.text[:200]}")
    return tok


def _api_get(client: httpx.Client, token: str, path: str, **params: Any) -> Any:
    r = client.get(f"{OAUTH_API}{path}", params=params,
                   headers={"Authorization": f"bearer {token}",
                            "User-Agent": API_UA})
    r.raise_for_status()
    return r.json()


def api_posts(client: httpx.Client, token: str, sub: str, sort: str,
              period: str, limit: int) -> list[dict]:
    params: dict[str, Any] = {"limit": min(limit, 100)}
    if sort == "top":
        params["t"] = period
    data = _api_get(client, token, f"/r/{sub}/{sort}", **params)
    posts = []
    for rank, child in enumerate(data.get("data", {}).get("children", []), 1):
        d = child["data"]
        posts.append({
            "id": d["id"], "rank": rank, "title": d["title"],
            "author": d.get("author"), "score": d["score"],
            "num_comments": d["num_comments"], "flair": d.get("link_flair_text"),
            "created": datetime.fromtimestamp(d["created_utc"],
                                              timezone.utc).isoformat(),
            "url": f"https://reddit.com{d['permalink']}",
            "text": (d.get("selftext") or "")[:4000],
        })
    return posts


def api_comments(client: httpx.Client, token: str, sub: str, post_id: str,
                 limit: int) -> list[dict]:
    data = _api_get(client, token, f"/r/{sub}/comments/{post_id}",
                    limit=limit, sort="top", depth=1)
    if len(data) < 2:
        return []
    out = [{"score": d["data"].get("score", 0),
            "body": d["data"]["body"][:1500],
            "author": d["data"].get("author")}
           for d in data[1].get("data", {}).get("children", [])
           if d.get("data", {}).get("body")
           and d["data"].get("author") != "AutoModerator"]
    return sorted(out, key=lambda c: -c["score"])[:limit]


# --------------------------------------------------------------------------
# analysis + output
# --------------------------------------------------------------------------

_STOP = {
    "The", "This", "That", "What", "When", "Who", "Why", "How", "But", "And",
    "For", "Not", "You", "Your", "Team", "Draft", "Rate", "My", "It", "If",
    "Is", "Are", "Was", "Will", "Any", "All", "Can", "Should", "Would", "GW",
    "FPL", "Premier", "League", "Fantasy", "Gameweek", "Captain", "Transfer",
    "Wildcard", "Bench", "Boost", "Free", "Hit", "Chip", "Cup", "World", "I",
    "A", "In", "On", "Of", "To", "With", "No", "Yes", "Or", "So", "Do", "Does",
}


def load_player_names(db_path: Path | None) -> set[str]:
    """web_name values from the season DB, so mention counts surface players
    rather than random capitalised words."""
    import sqlite3
    if not db_path or not db_path.exists():
        return set()
    con = sqlite3.connect(db_path)
    names = set()
    for (web,) in con.execute("SELECT web_name FROM players"):
        names.add(web)
        tail = re.split(r"[.\s]", web)[-1]     # 'B.Fernandes' -> 'Fernandes'
        if len(tail) > 2:
            names.add(tail)
    con.close()
    return names


def mention_counts(posts: list[dict], comments: dict[str, list[dict]],
                   known: set[str] | None = None) -> list[tuple[str, int]]:
    blob = " ".join([p["title"] + " " + p["text"] for p in posts]
                    + [c["body"] for cs in comments.values() for c in cs])
    counts: dict[str, int] = {}
    for tok in re.findall(r"\b[A-Z][a-zA-Zà-üÀ-Ü'\-]{2,}\b", blob):
        if tok in _STOP or (known and tok not in known):
            continue
        counts[tok] = counts.get(tok, 0) + 1
    return sorted(counts.items(), key=lambda kv: -kv[1])


def digest(posts: list[dict], comments: dict[str, list[dict]],
           mentions: list[tuple[str, int]], backend: str, top_n: int = 30) -> str:
    n_comments = sum(len(c) for c in comments.values())
    lines = [f"# r/FantasyPL digest — {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC",
             f"\nbackend: {backend} | {len(posts)} posts | {n_comments} comments"]
    if backend == "rss":
        lines.append("\n*(RSS provides no scores/comment counts; posts are in "
                     "Reddit's own ranking order.)*")
    lines.append("\n## Most-mentioned players\n")
    lines += [f"- {name}: {n}" for name, n in mentions[:top_n]] or ["- (none)"]
    lines.append("\n## Posts\n")
    for p in posts:
        head = f"### {p['rank']}. {p['title']}"
        if p.get("score") is not None:
            head += f"  [{p['score']}▲ {p['num_comments']}💬]"
        lines.append(head)
        meta = " ".join(filter(None, [p.get("author"), (p.get("created") or "")[:10]]))
        lines.append(f"*{meta}* — {p['url']}")
        if p["text"].strip():
            lines.append(f"\n{p['text'].strip()[:1200]}\n")
        for c in comments.get(p["id"], []):
            tag = f"[{c['score']}▲] " if c.get("score") is not None else ""
            lines.append(f"> {tag}{c['body'].strip()[:600]}")
        lines.append("")
    return "\n".join(lines)


def scrape_reddit(sub: str, sort: str, period: str, limit: int,
                  comment_limit: int, out: Path, db_path: Path | None = None,
                  backend: str = "auto") -> Path:
    if backend == "auto":
        backend = ("oauth" if os.environ.get("REDDIT_CLIENT_ID")
                   and os.environ.get("REDDIT_CLIENT_SECRET") else "rss")
    log.info("Backend: %s", backend)

    with httpx.Client(timeout=TIMEOUT, follow_redirects=True) as client:
        if backend == "oauth":
            token = get_token(client)
            posts = api_posts(client, token, sub, sort, period, limit)
        else:
            posts = rss_posts(client, sub, sort, period, limit)
        log.info("Got %d posts from r/%s (%s)", len(posts), sub, sort)

        comments: dict[str, list[dict]] = {}
        if comment_limit:
            for i, p in enumerate(posts, 1):
                try:
                    if backend == "oauth":
                        comments[p["id"]] = api_comments(
                            client, token, sub, p["id"], comment_limit)
                    else:
                        time.sleep(RSS_DELAY)      # stay under the rate limit
                        comments[p["id"]] = rss_comments(
                            client, p["url"], comment_limit)
                except (httpx.HTTPError, RedditError) as e:
                    log.warning("Comments failed for %s: %s", p["id"], e)
                if i % 5 == 0:
                    log.info("  comments: %d / %d", i, len(posts))

    mentions = mention_counts(posts, comments, load_player_names(db_path))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(
        {"fetched": datetime.now(timezone.utc).isoformat(), "backend": backend,
         "subreddit": sub, "sort": sort, "period": period,
         "posts": posts, "comments": comments, "mentions": mentions},
        indent=2, ensure_ascii=False))
    md = out.with_suffix(".md")
    md.write_text(digest(posts, comments, mentions, backend))
    log.info("Wrote %s and %s", out, md)
    return out
