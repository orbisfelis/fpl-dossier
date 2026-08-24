"""Command-line interface."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from .scrape import run_scrape
from .report import generate_report, publish_reports

def _load_dotenv(path: Path = Path(".env")) -> None:
    """Populate os.environ from .env for local runs (docker compose already
    does this via env_file). Real environment variables always win, and the
    file is git-ignored so secrets stay local."""
    try:
        text = path.read_text()
    except OSError:
        return
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val


_load_dotenv()

DEFAULT_DB = Path(os.environ.get("FPL_DB", "/data/fpl.db"))
DEFAULT_DOCS = Path(os.environ.get("FPL_DOCS", "docs"))
DEFAULT_CONCURRENCY = 10


def main(argv: list[str] | None = None) -> int:
    # Shared options live on a parent parser so `--db` etc. work both before
    # AND after the subcommand name.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db", type=Path, default=DEFAULT_DB,
                        help=f"SQLite path (default: {DEFAULT_DB})")
    common.add_argument("--verbose", "-v", action="store_true")
    common.add_argument("--force-unsealed", action="store_true",
                        dest="force_unsealed",
                        help="Write to a sealed (finished) season anyway. "
                             "Deliberately verbose — a sealed season is a record.")

    parser = argparse.ArgumentParser(
        prog="fpl",
        description="Fantasy Premier League scraper + report generator",
        parents=[common],
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # scrape ---------------------------------------------------------------
    sp = subparsers.add_parser("scrape", parents=[common],
                               help="Pull data from FPL API into SQLite")
    sp.add_argument("--league", type=int,
                    default=_env_int("FPL_LEAGUE_ID"),
                    required="FPL_LEAGUE_ID" not in os.environ,
                    help="Classic league ID (or set FPL_LEAGUE_ID)")
    sp.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    sp.add_argument("--skip-player-history", action="store_true",
                    help="Skip /element-summary/ scrape (saves ~1-2 min)")
    sp.add_argument("--owned-only", action="store_true",
                    help="Only fetch player history for players owned by managers in the league")
    sp.add_argument("--entry", type=int, action="append", dest="extra_entries",
                    metavar="ID",
                    help="Also scrape this manager by entry ID, even if the league "
                         "standings don't list them yet (pre-season new joiners). "
                         "Repeatable: --entry 123 --entry 456")

    # report ---------------------------------------------------------------
    rp = subparsers.add_parser("report", parents=[common],
                               help="Generate a report (markdown or PDF)")
    rp.add_argument("--league", type=int,
                    default=_env_int("FPL_LEAGUE_ID"),
                    required="FPL_LEAGUE_ID" not in os.environ,
                    help="Classic league ID")
    rp.add_argument("--gw", type=int, default=None,
                    help="Gameweek to report on (default: latest scraped)")
    rp.add_argument("--format", "-f", choices=["md", "html"], default="md",
                    dest="fmt",
                    help="Output format: md (default) or html")
    rp.add_argument("--output", "-o", type=Path, default=None,
                    help="Output path (default: /data/reports/dossier_GW{N}.{fmt})")
    _add_narrative_opts(rp)

    # publish --------------------------------------------------------------
    pp = subparsers.add_parser("publish", parents=[common],
                               help="Render HTML report(s) into docs/ for GitHub Pages")
    pp.add_argument("--league", type=int,
                    default=_env_int("FPL_LEAGUE_ID"),
                    required="FPL_LEAGUE_ID" not in os.environ,
                    help="Classic league ID")
    pp.add_argument("--gw", type=int, default=None,
                    help="Gameweek to publish (default: latest scraped)")
    pp.add_argument("--all", action="store_true", dest="all_gws",
                    help="Publish every scraped gameweek (backfill an archive)")
    pp.add_argument("--season", default=None,
                    help="Season label for the docs/<season>/ folder (default: derived, e.g. 25-26)")
    pp.add_argument("--docs", type=Path, default=DEFAULT_DOCS,
                    help=f"GitHub Pages output dir (default: {DEFAULT_DOCS})")
    _add_narrative_opts(pp)

    # seal -----------------------------------------------------------------
    sl = subparsers.add_parser("seal", parents=[common],
                               help="Freeze a finished season: no more DB writes, "
                                    "no more regenerated pages")
    sl.add_argument("--season", required=True,
                    help="Season label, e.g. 25-26 (locks docs/<season>/)")
    sl.add_argument("--docs", type=Path, default=DEFAULT_DOCS)
    sl.add_argument("--reason", default="season complete")

    us = subparsers.add_parser("unseal", parents=[common],
                               help="Remove a season's seal (rarely correct)")
    us.add_argument("--season", required=True)
    us.add_argument("--docs", type=Path, default=DEFAULT_DOCS)

    # registry -------------------------------------------------------------
    # --registry is accepted either side of the subcommand, so both
    # `registry --registry X sync` and `registry sync --registry X` work.
    reg_common = argparse.ArgumentParser(add_help=False)
    reg_common.add_argument("--registry", type=Path, default=None,
                            help="Registry DB (default: data/registry.db, "
                                 "or $FPL_REGISTRY)")

    rg = subparsers.add_parser("registry", parents=[common, reg_common],
                               help="Track league/manager identity across seasons")
    rgsub = rg.add_subparsers(dest="registry_cmd", required=True)

    rgs = rgsub.add_parser("sync", parents=[reg_common],
                           help="Record this season's league id + roster")
    rgs.add_argument("--db", type=Path, default=DEFAULT_DB, dest="season_db",
                     help="Season DB to read the roster from")
    rgs.add_argument("--league", type=int, default=_env_int("FPL_LEAGUE_ID"),
                     required="FPL_LEAGUE_ID" not in os.environ)
    rgs.add_argument("--season", default=None,
                     help="Season label, e.g. 26-27 (default: derived)")
    rgs.add_argument("--lineage", default=None,
                     help="Force the lineage id these seasons belong to")

    rgsub.add_parser("list", parents=[reg_common],
                     help="Show each person's entry id by season")

    rgl = rgsub.add_parser("link", parents=[reg_common],
                           help="Repair a mis-linked manager")
    rgl.add_argument("--lineage", required=True)
    rgl.add_argument("--season", required=True)
    rgl.add_argument("--entry", type=int, required=True)
    rgl.add_argument("--person", type=int, required=True)

    # preseason ------------------------------------------------------------
    ps = subparsers.add_parser("preseason", parents=[common],
                               help="Build the pre-season welcome page (published as GW0)")
    ps.add_argument("--league", type=int,
                    default=_env_int("FPL_LEAGUE_ID"),
                    required="FPL_LEAGUE_ID" not in os.environ,
                    help="Classic league ID")
    ps.add_argument("--prev-db", type=Path, default=_env_path("FPL_PREV_DB"),
                    help="Archived previous-season DB — supplies the league "
                         "history and player intel (or set FPL_PREV_DB)")
    ps.add_argument("--season", default=None,
                    help="Season label for docs/<season>/ (default: derived)")
    ps.add_argument("--docs", type=Path, default=DEFAULT_DOCS,
                    help=f"GitHub Pages output dir (default: {DEFAULT_DOCS})")
    ps.add_argument("--url", default=os.environ.get("FPL_PUBLIC_URL", ""),
                    help="Public page URL, appended to the WhatsApp text "
                         "(or set FPL_PUBLIC_URL)")
    ps.add_argument("--output", "-o", type=Path, default=None,
                    help="Write a standalone HTML file instead of publishing to docs/")
    # Narrative flags only — preseason already defines its own --prev-db.
    ps.add_argument("--narrative", choices=["auto", "llm", "cli", "none"],
                    default="auto",
                    help="Pre-season column source: 'llm' = Claude API, "
                         "'cli' = local `claude` login, 'none' = omit it; "
                         "'auto' (default) prefers the key, then the CLI")
    ps.add_argument("--refresh-narrative", action="store_true",
                    help="Regenerate the column even if a cached one exists")

    # reddit ---------------------------------------------------------------
    rd = subparsers.add_parser("reddit", parents=[common],
                               help="Fetch r/FantasyPL discussion as grounding input")
    rd.add_argument("--sub", default="FantasyPL", help="Subreddit (default: FantasyPL)")
    rd.add_argument("--sort", default="top", choices=["top", "hot", "new", "rising"])
    rd.add_argument("--time", dest="period", default="week",
                    choices=["hour", "day", "week", "month", "year", "all"],
                    help="Time window for --sort top (default: week)")
    rd.add_argument("--limit", type=int, default=50,
                    help="Posts to fetch, max 100 (default: 50)")
    rd.add_argument("--comments", type=int, default=8, dest="comment_limit",
                    help="Top comments per post, 0 to skip (default: 8)")
    rd.add_argument("--backend", choices=["auto", "rss", "oauth"], default="auto",
                    help="'rss' needs no credentials (default when none are set); "
                         "'oauth' needs REDDIT_CLIENT_ID/SECRET and adds scores")
    rd.add_argument("--out", type=Path, default=None,
                    help="Output JSON path (a .md digest is written alongside)")

    # shell ----------------------------------------------------------------
    subparsers.add_parser("shell", parents=[common],
                          help="Open a sqlite3 shell on the DB")

    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    from .seal import SealedError
    try:
        return _dispatch(args, parser)
    except SealedError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2


def _dispatch(args, parser) -> int:
    if args.command == "scrape":
        asyncio.run(run_scrape(
            league_id=args.league,
            db_path=args.db,
            concurrency=args.concurrency,
            skip_player_history=args.skip_player_history,
            owned_only=args.owned_only,
            extra_entries=args.extra_entries,
            force_unsealed=args.force_unsealed,
        ))

    elif args.command == "report":
        output = args.output
        if output is None:
            gw_label = args.gw if args.gw is not None else "latest"
            ext = args.fmt
            output = args.db.parent / "reports" / f"dossier_GW{gw_label}.{ext}"
        path = generate_report(args.db, output, args.league, args.gw,
                               fmt=args.fmt, narrative=args.narrative,
                               refresh_narrative=args.refresh_narrative,
                               prev_db=args.prev_db)
        print(f"Report written: {path}")

    elif args.command == "publish":
        season_dir = publish_reports(
            args.db, args.league, args.docs,
            season=args.season, event=args.gw, all_gws=args.all_gws,
            narrative=args.narrative, refresh_narrative=args.refresh_narrative,
            prev_db=args.prev_db, force_unsealed=args.force_unsealed,
        )
        print(f"Published to: {season_dir} (manifest + index.html updated)")

    elif args.command == "seal":
        from .seal import seal as do_seal
        made = do_seal(args.db, args.docs / args.season, reason=args.reason)
        for m in made:
            print(f"sealed: {m}")
        if not made:
            print("nothing sealed (check --db and --season)")

    elif args.command == "unseal":
        from .seal import unseal as do_unseal
        gone = do_unseal(args.db, args.docs / args.season)
        for m in gone:
            print(f"unsealed: {m}")
        if not gone:
            print("nothing was sealed")

    elif args.command == "registry":
        from . import registry as reg
        from .report import current_season
        registry_path = args.registry or Path(
            os.environ.get("FPL_REGISTRY", reg.DEFAULT_REGISTRY))
        conn = reg.open_registry(registry_path)
        if args.registry_cmd == "sync":
            s = reg.sync_season(conn, args.season_db,
                                args.season or current_season(), args.league,
                                lineage_id=args.lineage)
            print(f"{s['label']} [{s['lineage_id']}] {s['season']} "
                  f"= league {s['league_id']}")
            print(f"  {s['managers']} managers: {s['linked']} linked to known "
                  f"people, {s['created']} new, {s['already']} already recorded")
            if s["previous_league_id"]:
                print(f"  previous season's league id: {s['previous_league_id']}")
        elif args.registry_cmd == "link":
            reg.link(conn, args.lineage, args.season, args.entry, args.person)
            print(f"Linked entry {args.entry} ({args.season}) "
                  f"to person {args.person}")
        else:
            people = reg.roster(conn)
            if not people:
                print("Registry is empty — run: fpl registry sync")
            else:
                seasons = people[0]["all_seasons"]
                print(f"{'person':>6}  {'name':<24}" +
                      "".join(f"{s:>12}" for s in seasons))
                for p in sorted(people, key=lambda x: x["name"].casefold()):
                    cells = "".join(
                        f"{(str(p['seasons'][s]['entry_id']) if s in p['seasons'] else '-'):>12}"
                        for s in seasons)
                    print(f"{p['person_id']:>6}  {p['name'][:24]:<24}{cells}")
        conn.close()

    elif args.command == "preseason":
        from .preseason import generate_preseason, publish_preseason
        if args.output:
            path = generate_preseason(args.db, args.output, args.league,
                                      prev_db=args.prev_db, season=args.season,
                                      public_url=args.url,
                                      narrative=args.narrative,
                                      refresh_narrative=args.refresh_narrative)
            print(f"Pre-season page written: {path}")
        else:
            path = publish_preseason(args.db, args.league, args.docs,
                                     prev_db=args.prev_db, season=args.season,
                                     public_url=args.url,
                                     narrative=args.narrative,
                                     refresh_narrative=args.refresh_narrative)
            print(f"Published: {path} (manifest + index.html updated)")

    elif args.command == "reddit":
        from .reddit import RedditError, scrape_reddit
        out = args.out
        if out is None:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
            out = args.db.parent / f"reddit_{args.sub}_{stamp}.json"
        try:
            path = scrape_reddit(
                sub=args.sub, sort=args.sort, period=args.period,
                limit=args.limit, comment_limit=args.comment_limit,
                out=out, db_path=args.db, backend=args.backend,
            )
        except RedditError as e:
            print(f"Reddit: {e}", file=sys.stderr)
            return 1
        print(f"Wrote: {path}\nDigest: {path.with_suffix('.md')}")

    elif args.command == "shell":
        import subprocess
        if not args.db.exists():
            print(f"DB not found: {args.db}", file=sys.stderr)
            return 1
        return subprocess.call(["sqlite3", str(args.db)])

    return 0


def _add_narrative_opts(p: argparse.ArgumentParser) -> None:
    """The top 'Week in Words' narrative source, shared by report + publish."""
    p.add_argument("--narrative", choices=["auto", "llm", "cli", "phrases"],
                   default="auto",
                   help="Top narrative source: 'llm' = Claude API (needs "
                        "ANTHROPIC_API_KEY); 'cli' = local logged-in `claude` CLI "
                        "(no key); 'phrases' = offline phrase banks; 'auto' "
                        "(default) prefers the API key, then the CLI, else phrases")
    p.add_argument("--refresh-narrative", action="store_true",
                   help="Regenerate the LLM narrative even if a cached one exists")
    p.add_argument("--prev-db", type=Path, default=_env_path("FPL_PREV_DB"),
                   help="Archived previous-season DB for same-week comparisons "
                        "(e.g. data/25_26_fpl.db); dormant until a prior season exists")


def _env_int(name: str) -> int | None:
    val = os.environ.get(name)
    return int(val) if val else None


def _env_path(name: str) -> Path | None:
    val = os.environ.get(name)
    return Path(val) if val else None


if __name__ == "__main__":
    sys.exit(main())
