#!/usr/bin/env python3
"""Move a RIFTWIRE storage location to another folder or drive (verified, resumable).

    backend/.venv/bin/python scripts/migrate_storage.py KIND DEST            # dry run
    backend/.venv/bin/python scripts/migrate_storage.py KIND DEST --execute  # do it
    backend/.venv/bin/python scripts/migrate_storage.py KIND --abort         # give up

KIND: recordings | temp | models | exports | assets
(the database, logs and thumbnails always stay on the internal disk).

* DRY RUN BY DEFAULT: shows what would be copied and every check; writes nothing.
* --execute requires RIFTWIRE to be stopped (scripts/stop.sh). It holds the worker lock
  and a storage-maintenance lock for the whole run.
* Every file is copied to a staging file on the destination, re-read and SHA-256-verified
  (recordings against the hash recorded at import), then promoted without overwriting.
  Before switching, every destination file is hashed again (--no-final-rehash skips that).
* Resumable: run the same command again after an interruption.
* The switch is one database transaction. ORIGINAL FILES ARE NEVER DELETED: they are
  listed at the end so you can check and remove them yourself.
* Refuses: an unmounted drive, a non-APFS drive (macOS), a non-empty destination folder,
  a destination overlapping another location, or too little space above the 60 GiB floor.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

from app.core.config import LOCATION_KINDS, get_settings  # noqa: E402
from app.db.migrations import schema_status  # noqa: E402
from app.db.session import Database  # noqa: E402
from app.services import migration  # noqa: E402
from app.services.locations import load_layout  # noqa: E402

GIB = 1024**3


def _gib(n: int | None) -> str:
    return "?" if n is None else f"{n / GIB:.2f} GiB"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("kind", choices=LOCATION_KINDS)
    ap.add_argument("dest", nargs="?", help="absolute destination folder (new or empty)")
    ap.add_argument("--execute", action="store_true", help="actually copy and switch")
    ap.add_argument("--no-final-rehash", action="store_true",
                    help="skip re-hashing every destination file before switching")
    ap.add_argument("--abort", action="store_true",
                    help="abandon an unfinished migration of KIND (location unchanged)")
    a = ap.parse_args(argv)

    settings = get_settings()
    db = Database(settings.database_url)
    try:
        st = schema_status(db.engine)
        if not st["ok"]:
            print(f"✗ database schema not at head ({st}); run scripts/migrate.sh")
            return 2
        if a.abort:
            m = migration.abort(db, a.kind)
            print("no unfinished migration" if m is None else
                  f"aborted migration to {m.dest_path}; location unchanged; files already "
                  "copied there were left in place")
            return 0
        if not a.dest:
            ap.error("DEST is required")
        dest = Path(a.dest).expanduser()
        if not a.execute:
            layout = load_layout(db, settings, "migration", register=False)
            p = migration.plan(db, settings, layout, a.kind, dest)
            print(f"DRY RUN — nothing will be written.\n"
                  f"  location:    {p.kind}\n  from:        {p.source}\n  to:          {p.dest}\n"
                  f"  files:       {len(p.files)} ({_gib(p.total_bytes)})"
                  + (f", {p.already_verified} already verified (resume)"
                     if p.resume_of else "")
                  + f"\n  destination free: {_gib(p.dest_free_bytes)}; floor "
                  f"{_gib(p.dest_floor_bytes)}")
            for f in p.files[:20]:
                print(f"    {f.rel_path}  ({f.size} bytes)")
            if len(p.files) > 20:
                print(f"    … and {len(p.files) - 20} more")
            if p.problems:
                print("✗ would be REFUSED:")
                for pr in p.problems:
                    print(f"   - {pr}")
                return 1
            print("✓ all checks pass. Stop RIFTWIRE (scripts/stop.sh), then re-run with "
                  "--execute.")
            return 0
        try:
            r = migration.execute(db, settings, a.kind, dest,
                                  rehash=not a.no_final_rehash, progress=print)
        except migration.MigrationRefused as e:
            print(f"✗ refused, nothing switched: {e}")
            return 1
        print(f"✓ '{r.kind}' now lives at {r.dest} ({r.copied} copied, "
              f"{r.already_verified} verified earlier, {_gib(r.total_bytes)}).")
        print(f"Original files were NOT deleted ({len(r.old_files)} in {r.source}). "
              "Check RIFTWIRE works, then remove them yourself if you want the space.")
        print("Start RIFTWIRE again with scripts/start.sh.")
        return 0
    finally:
        db.dispose()


if __name__ == "__main__":
    sys.exit(main())
