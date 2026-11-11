"""Empty the training database, or replace the full corpus it is loaded from.

    python -m tools.ml_reset db                         # empty rcm_ml and its RAW archive
    python -m tools.ml_reset corpus data/synthetic data/full

**Why `db` exists.** `load_corpus` skips a file it has already archived
(hash dedup, PD-9). That is right for a re-run and wrong for a new corpus:
loaded over the old one, the files that did not change are skipped, the ones
that did are added, and the models train on a mixture of two corpora that
nothing reports. A new corpus needs an empty database.

**Why `corpus` exists.** The Makefile used to do this with
`shutil.rmtree(..., ignore_errors=True)` and a move. Archived RAW files are
write-protected, so on Windows the delete failed without a word, the
destination survived, and the move put the corpus *inside* it. `make ml-db`
then found no corpus. Here a file that cannot be removed raises.

`db` refuses any database but the one named: `rcm` is what the operator UI
shows and `rcm_test` belongs to the suites.
"""

from __future__ import annotations

import argparse
import shutil
import stat
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from rcm.platform.config import get_settings

ML_DB = "rcm_ml"


def _clear_read_only(function: Callable[..., Any], path: str, _exc: BaseException) -> None:
    Path(path).chmod(stat.S_IWRITE)
    function(path)


def remove_tree(path: Path) -> None:
    """Delete a tree whose files may be write-protected; raise if any survives."""
    if path.exists():
        shutil.rmtree(path, onexc=_clear_read_only)


def replace_corpus(source: Path, destination: Path) -> None:
    if not (source / "x12").is_dir():
        raise SystemExit(f"no corpus at {source}")
    remove_tree(destination)
    shutil.move(source, destination)


def reset_db(database: str = ML_DB) -> list[str]:
    settings = get_settings()
    url = make_url(settings.migration_database_url.get_secret_value())
    if url.database != database:
        raise SystemExit(
            f"refusing to empty `{url.database}`: this only ever empties `{database}`. "
            "Run it through `make ml-reset`."
        )
    engine = create_engine(url)
    with engine.begin() as conn:
        tables: list[str] = list(
            conn.execute(
                text(
                    "SELECT tablename FROM pg_tables "
                    "WHERE schemaname = 'public' AND tablename <> 'alembic_version' "
                    "ORDER BY tablename"
                )
            ).scalars()
        )
        if tables:
            quoted = ", ".join(f'public."{t}"' for t in tables)
            conn.execute(text(f"TRUNCATE {quoted} RESTART IDENTITY CASCADE"))
        schemas: list[str] = list(
            conn.execute(
                text(
                    "SELECT schema_name FROM information_schema.schemata "
                    "WHERE schema_name LIKE 'analytics%'"
                )
            ).scalars()
        )
        for schema in schemas:
            conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
    engine.dispose()
    remove_tree(Path(settings.raw_archive_root))
    return tables


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("db")
    corpus = commands.add_parser("corpus")
    corpus.add_argument("source", type=Path)
    corpus.add_argument("destination", type=Path)
    args = parser.parse_args(argv)

    if args.command == "db":
        tables = reset_db()
        sys.stdout.write(f"{ML_DB}: {len(tables)} tables emptied, warehouse and archive removed\n")
    else:
        replace_corpus(args.source, args.destination)
        sys.stdout.write(f"corpus moved to {args.destination}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
