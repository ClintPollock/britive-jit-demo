#!/usr/bin/env python3
"""
Generate the day's synthetic customer-intake batch.

The nightly workflow runs this once per cloud job and uploads the result to
    <bucket>/daily/<YYYY-MM-DD>.csv

DETERMINISTIC BY DATE, ON PURPOSE. The RNG is seeded from the batch date and
nothing else, so:

  * all three cloud jobs produce a BYTE-IDENTICAL csv for a given day, even
    though three independent JIT identities wrote them. Comparing the sha256
    across S3 / GCS / Azure is a real integrity check, not theatre.
  * re-running the workflow on the same day is idempotent - it overwrites
    today's object with the same bytes rather than inventing a second batch.
  * the numbers still change every single day, so a demo is never canned.

The per-cloud detail that CANNOT go in the csv (which identity wrote it, which
run, when) goes in the sidecar manifest instead - see --manifest.

Usage:
    gen-intake.py --date 2026-08-09 --out /tmp/intake.csv
    gen-intake.py --date 2026-08-09 --out /tmp/intake.csv \
                  --manifest /tmp/intake.manifest.json \
                  --cloud aws --run-id 123 --repo o/r --actor someone
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
import random
import sys

FIRST = [
    "Marco", "Dana", "Priya", "Tomas", "Aiko", "Lucas", "Nadia", "Owen",
    "Sofia", "Ravi", "Elena", "Jonas", "Mei", "Andre", "Clara", "Hassan",
    "Ingrid", "Diego", "Yuki", "Fatima", "Peter", "Lena", "Omar", "Rosa",
    "Nikhil", "Greta", "Sean", "Amara", "Viktor", "Chloe",
]
LAST = [
    "Bellini", "Okafor", "Raman", "Novak", "Tanaka", "Moreau", "Haddad",
    "Fitzgerald", "Reyes", "Patel", "Vasquez", "Lindqvist", "Chen", "Silva",
    "Novotny", "Al-Amin", "Berg", "Castillo", "Watanabe", "Nasser",
    "Kowalski", "Muller", "Diallo", "Ferreira", "Iyer", "Schmidt",
]
COMPANY_A = [
    "Northwind", "Blue Harbor", "Ridgeline", "Cobalt", "Fairmont", "Ironwood",
    "Silverpine", "Redwood", "Harborview", "Granite", "Lakeshore", "Summit",
]
COMPANY_B = ["Logistics", "Analytics", "Health", "Robotics", "Foods", "Capital",
             "Systems", "Labs", "Freight", "Media", "Energy", "Retail"]

REGIONS = ["us-east", "us-west", "eu-west", "eu-central", "apac", "latam"]

# plan -> (monthly recurring revenue range, relative likelihood)
PLANS = {
    "starter":    ((29, 99), 40),
    "pro":        ((190, 490), 30),
    "business":   ((700, 1400), 20),
    "enterprise": ((2100, 4800), 10),
}

SOURCES = ["web-signup", "partner-referral", "outbound", "event", "self-serve-trial"]

# Ordinary sales notes. An earlier version salted ~8% of these with an SSN so
# the agent could "discover" leaked PII. It was cut: the rate was implausible
# (real leakage is orders of magnitude rarer), and more importantly it pulled
# the demo toward data-loss discovery, which is not what any of this proves.
# The records are already plainly sensitive - names, emails, phones, revenue
# per account - which is all the story needs.
NOTES = [
    "", "", "", "", "",
    "Requested SOC2 report",
    "Migrating from a competitor",
    "Asked about annual prepay",
    "Needs SSO before rollout",
    "Pilot team of 12",
    "Procurement review in progress",
]


def build_rows(rng: random.Random, batch_date: str, count: int) -> list[dict]:
    plan_names = list(PLANS)
    plan_weights = [PLANS[p][1] for p in plan_names]
    rows = []
    for i in range(count):
        first = rng.choice(FIRST)
        last = rng.choice(LAST)
        company = f"{rng.choice(COMPANY_A)} {rng.choice(COMPANY_B)}"
        domain = company.lower().replace(" ", "") + ".example.com"
        plan = rng.choices(plan_names, weights=plan_weights, k=1)[0]
        low, high = PLANS[plan][0]

        note = rng.choice(NOTES)

        rows.append(
            {
                "customer_id": f"C-{rng.randint(1000, 9999)}-{i:03d}",
                "first_name": first,
                "last_name": last,
                "email": f"{first[0].lower()}.{last.lower()}@{domain}",
                "phone": f"+1-{rng.randint(200, 989)}-{rng.randint(200, 989)}-{rng.randint(1000, 9999)}",
                "company": company,
                "plan": plan,
                "mrr_usd": rng.randint(low, high),
                "region": rng.choice(REGIONS),
                "source": rng.choice(SOURCES),
                "signup_date": batch_date,
                "notes": note,
            }
        )
    return rows


FIELDS = [
    "customer_id", "first_name", "last_name", "email", "phone", "company",
    "plan", "mrr_usd", "region", "source", "signup_date", "notes",
]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--date", help="batch date YYYY-MM-DD (default: today, UTC)")
    ap.add_argument("--out", required=True, help="path to write the csv to")
    ap.add_argument("--manifest", help="path to write the sidecar manifest json to")
    ap.add_argument("--rows", type=int, help="force a row count (default: 25-60, by date)")
    ap.add_argument("--cloud", default="", help="manifest only: aws | gcp | azure")
    ap.add_argument("--run-id", default="", help="manifest only: github run id")
    ap.add_argument("--repo", default="", help="manifest only: owner/repo")
    ap.add_argument("--actor", default="", help="manifest only: who/what triggered it")
    ap.add_argument("--written-by", default="", help="manifest only: the JIT identity that wrote it")
    args = ap.parse_args(argv)

    batch_date = args.date or dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    try:
        dt.date.fromisoformat(batch_date)
    except ValueError:
        print(f"error: --date must be YYYY-MM-DD, got {batch_date!r}", file=sys.stderr)
        return 2

    # Seeded from the date alone -> every cloud generates the same bytes.
    rng = random.Random(batch_date)
    count = args.rows if args.rows else rng.randint(25, 60)
    rows = build_rows(rng, batch_date, count)

    # newline="" + \n keeps the bytes identical on any platform, which the
    # cross-cloud sha256 comparison depends on.
    with open(args.out, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS, lineterminator="\n")
        w.writeheader()
        w.writerows(rows)

    with open(args.out, "rb") as fh:
        digest = hashlib.sha256(fh.read()).hexdigest()

    summary = {
        "batch_date": batch_date,
        "rows": len(rows),
        "sha256": digest,
        "total_mrr_usd": sum(r["mrr_usd"] for r in rows),
        "by_plan": {p: sum(1 for r in rows if r["plan"] == p) for p in PLANS},
    }

    if args.manifest:
        manifest = dict(summary)
        manifest.update(
            {
                "cloud": args.cloud,
                "run_id": args.run_id,
                "repo": args.repo,
                "actor": args.actor,
                "written_by": args.written_by,
                "written_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "auth_path": "GitHub OIDC -> Britive (federated SI) -> JIT cloud credential",
                "static_creds": "none",
                "note": (
                    "The csv is seeded from batch_date alone, so all three clouds "
                    "hold byte-identical content written by three independent "
                    "just-in-time identities. This manifest is the only per-cloud part."
                ),
            }
        )
        with open(args.manifest, "w", encoding="utf-8") as fh:
            json.dump(manifest, fh, indent=2)
            fh.write("\n")

    # stdout is the step summary in the workflow log
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
