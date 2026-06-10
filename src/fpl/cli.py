"""Command-line interface."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

from .scrape import run_scrape
from .report import generate_report, publish_reports

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

    # shell ----------------------------------------------------------------
    subparsers.add_parser("shell", parents=[common],
                          help="Open a sqlite3 shell on the DB")

    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if args.command == "scrape":
        asyncio.run(run_scrape(
            league_id=args.league,
            db_path=args.db,
            concurrency=args.concurrency,
            skip_player_history=args.skip_player_history,
            owned_only=args.owned_only,
        ))

    elif args.command == "report":
        output = args.output
        if output is None:
            gw_label = args.gw if args.gw is not None else "latest"
            ext = args.fmt
            output = args.db.parent / "reports" / f"dossier_GW{gw_label}.{ext}"
        path = generate_report(args.db, output, args.league, args.gw,
                               fmt=args.fmt, narrative=args.narrative,
                               refresh_narrative=args.refresh_narrative)
        print(f"Report written: {path}")

    elif args.command == "publish":
        season_dir = publish_reports(
            args.db, args.league, args.docs,
            season=args.season, event=args.gw, all_gws=args.all_gws,
            narrative=args.narrative, refresh_narrative=args.refresh_narrative,
        )
        print(f"Published to: {season_dir} (manifest + index.html updated)")

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


def _env_int(name: str) -> int | None:
    val = os.environ.get(name)
    return int(val) if val else None


if __name__ == "__main__":
    sys.exit(main())
