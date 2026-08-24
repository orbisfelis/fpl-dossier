"""Sealing finished seasons so they stop changing.

A completed season is a record. Once its last gameweek is published, the
database and the pages generated from it should never move again — not for a
schema migration, not for a template change, not for a restyle.

This is not hypothetical. Regenerating the 25/26 archive to pick up a new dark
theme silently replaced all 38 AI-written columns with phrase-bank text,
because the machine doing the regeneration had no API key. The pages still
rendered, so nothing failed and nothing was noticed until someone read one.

A seal is a marker file next to the thing it protects:

    data/25_26_fpl.db.sealed          blocks writes to that database
    docs/25-26/.sealed                blocks regenerating those pages

Both carry a timestamp and a reason, and both are plain text so the state is
obvious from `ls` and greppable in git. Overriding needs an explicit
`--force-unsealed`, which is deliberately awkward to type.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

MARKER = ".sealed"


class SealedError(RuntimeError):
    """Raised when something tries to modify a sealed season."""


def db_marker(db_path: Path) -> Path:
    return db_path.with_name(db_path.name + MARKER)


def docs_marker(season_dir: Path) -> Path:
    return season_dir / MARKER


def _read(marker: Path) -> dict | None:
    if not marker.exists():
        return None
    try:
        return json.loads(marker.read_text())
    except (OSError, ValueError):
        # A malformed marker still means sealed. Failing open here would defeat
        # the point of the guard.
        return {"sealed_at": "unknown", "reason": "unreadable marker"}


def is_db_sealed(db_path: Path) -> dict | None:
    return _read(db_marker(Path(db_path)))


def is_docs_sealed(season_dir: Path) -> dict | None:
    return _read(docs_marker(Path(season_dir)))


def _describe(info: dict) -> str:
    at = info.get("sealed_at", "unknown")
    why = info.get("reason") or "season complete"
    return f"sealed {at} ({why})"


def check_db_writable(db_path: Path, force: bool = False) -> None:
    info = is_db_sealed(db_path)
    if info and not force:
        raise SealedError(
            f"{db_path} is {_describe(info)}. Refusing to write to a finished "
            f"season. Use --force-unsealed to override, or `fpl unseal` to "
            f"remove the seal permanently.")


def check_docs_writable(season_dir: Path, force: bool = False) -> None:
    info = is_docs_sealed(season_dir)
    if info and not force:
        raise SealedError(
            f"{season_dir} is {_describe(info)}. Refusing to regenerate pages "
            f"for a finished season — the published dossiers are the record. "
            f"Use --force-unsealed to override, or `fpl unseal` to remove it.")


def seal(db_path: Path | None, season_dir: Path | None,
         reason: str = "season complete") -> list[Path]:
    """Write the markers. Returns the paths created."""
    stamp = {"sealed_at": datetime.now(timezone.utc).isoformat(), "reason": reason}
    body = json.dumps(stamp, indent=2) + "\n"
    made = []
    if db_path:
        m = db_marker(Path(db_path))
        m.write_text(body)
        made.append(m)
    if season_dir:
        d = Path(season_dir)
        if d.is_dir():
            m = docs_marker(d)
            m.write_text(body)
            made.append(m)
    return made


def unseal(db_path: Path | None, season_dir: Path | None) -> list[Path]:
    removed = []
    for m in (db_marker(Path(db_path)) if db_path else None,
              docs_marker(Path(season_dir)) if season_dir else None):
        if m and m.exists():
            m.unlink()
            removed.append(m)
    return removed
