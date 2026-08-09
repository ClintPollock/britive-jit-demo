#!/usr/bin/env python3
"""
Decide which objects to delete so a prefix keeps only the N most recent.

Reads object keys on stdin (one per line, order irrelevant) and prints the keys
that should be DELETED, one per line. Prints nothing when there is nothing to
prune, so callers can pipe straight into a delete loop.

Two modes, because the two prefixes in this demo have different shapes:

  --mode date   Group keys by the YYYY-MM-DD embedded in them and keep the N
                most recent DATES. Used for `daily/`, where one batch is two
                objects (`<date>.csv` + `<date>.manifest.json`) that must live
                and die together — pruning by object count would orphan a
                manifest from its batch.

  --mode name   Sort keys descending and keep the N most recent. Used for the
                per-run marker files, whose names carry a monotonically
                increasing GitHub run id.

Sorting is by NAME, never by last-modified time, on purpose: the workflow is
idempotent and re-running it on an existing date rewrites that object, which
would move it to the top of an mtime-ordered list and make an old batch look
new. The date in the key is the truth.

Usage:
    ... list keys ... | python3 scripts/prune.py --mode date --keep 10
"""

from __future__ import annotations

import argparse
import re
import sys

DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")


def pick(keys: list[str], mode: str, keep: int) -> list[str]:
    keys = [k.strip() for k in keys if k.strip()]
    # Ignore "directory" placeholder objects some tools create.
    keys = [k for k in keys if not k.endswith("/")]
    if keep < 0 or not keys:
        return []

    if mode == "name":
        return sorted(keys, reverse=True)[keep:]

    # mode == "date"
    dated: dict[str, list[str]] = {}
    undated: list[str] = []
    for k in keys:
        m = DATE_RE.search(k)
        (dated.setdefault(m.group(1), []).append(k) if m else undated.append(k))

    # Anything without a date is left alone rather than guessed at — deleting an
    # object we cannot place in time is not a risk worth taking automatically.
    doomed_dates = sorted(dated, reverse=True)[keep:]
    return [k for d in doomed_dates for k in sorted(dated[d])]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--mode", choices=("date", "name"), required=True)
    ap.add_argument("--keep", type=int, default=10)
    args = ap.parse_args(argv)

    # Force LF endings. On Windows, print() would emit CRLF, and the trailing \r
    # rides along into the object key — at which point `aws s3 rm` deletes a key
    # that does not exist and STILL EXITS 0, so the prune silently no-ops. CI is
    # Linux and never hit this, but it made local verification lie.
    sys.stdout.reconfigure(newline="\n")

    doomed = pick(sys.stdin.read().splitlines(), args.mode, args.keep)
    for k in doomed:
        print(k)
    # Progress goes to stderr so stdout stays a clean list for the caller.
    print(f"prune: {len(doomed)} object(s) to delete (keep={args.keep}, mode={args.mode})",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
