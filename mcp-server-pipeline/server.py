"""
britive-jit-pipeline MCP server  (AWS only)
===========================================

Lets Claude read the nightly intake batch that the GitHub Actions workflow
drops into S3 - with NO standing cloud credentials, and, critically, with a
choice of WHO the read runs as.

Three identities touch one file
-------------------------------
  1. WRITER   GitHub Actions, nightly, unattended. GitHub OIDC -> Britive
              federated service identity -> JIT write credential. No human,
              no static key. (That half lives in .github/workflows/jit-demo.yml.)

  2. READER, AS THE AGENT      as_identity="agent"
              This server checks out the reader profile as the Britive
              SERVICE identity. The cloud session is the bot. Accountability
              lands on the bot, and the bot's own grant bounds what it sees.

  3. READER, AS THE HUMAN      as_identity="user"   <- the default
              Same service-identity token, but the checkout carries
              `X-On-Behalf-Of: <user>`. The cloud session becomes the HUMAN.
              The agent can see this file because THE USER can see it - not
              because the agent holds its own key. Every audit record names
              the person, not the bot.

`whoami` returns the actual cloud identity for either mode, so the contrast is
demonstrable rather than asserted.

Every tool call is checkout -> do the work -> checkin. The credential's
lifetime is the tool call. Each call is a row in the Britive audit log.

SCOPE: AWS only, deliberately. The workflow still writes the same batch to GCS
and Azure Blob, but the live demo reads one cloud for speed and because AWS is
the only one with a proven on-behalf-of path. Restoring the multi-cloud read
means adding the GCS/Blob clients back and granting the AI service identity
those profiles - it currently has neither.

Auth: needs a Britive service-identity token (the "AI" identity) in
BRITIVE_API_TOKEN. On-behalf-of REQUIRES a service identity - a plain user
token cannot impersonate.
"""

from __future__ import annotations

import contextlib
import csv
import hashlib
import io
import json
import os
from collections import Counter
from datetime import datetime, timezone, date as _date

from britive.britive import Britive
from mcp.server.fastmcp import FastMCP

# ── Config (override via env in .mcp.json) ──────────────────────────────────
TENANT = os.environ.get("BRITIVE_TENANT", "cpollock-tenant")
TOKEN = os.environ.get("BRITIVE_API_TOKEN", "")

# The human the agent impersonates when as_identity="user".
OBO_USER = os.environ.get("BRITIVE_OBO_USER", "clint.pollock@jit-zsp.com")

DAILY_PREFIX = os.environ.get("PIPELINE_DAILY_PREFIX", "daily")
PROFILE = os.environ.get("PIPELINE_AWS_PROFILE", "AWS Standalone/0299-CP/AWS-S3-Reader")
BUCKET = os.environ.get("PIPELINE_S3_BUCKET", "britive-jit-demo-696226360299")

mcp = FastMCP("britive-jit-pipeline")


class PipelineError(RuntimeError):
    pass


# ── Britive checkout / checkin ──────────────────────────────────────────────
def _split_profile(path: str) -> tuple[str, str, str]:
    parts = [p.strip() for p in path.split("/")]
    if len(parts) != 3:
        raise PipelineError(
            f"Profile path must be 'Application/Environment/Profile', got {path!r}"
        )
    return parts[0], parts[1], parts[2]


@contextlib.contextmanager
def jit_credentials(as_identity: str):
    """Check out the reader profile, yield credentials, always check back in.

    as_identity="user" adds the X-On-Behalf-Of header, which makes the cloud
    session the HUMAN rather than the service identity.
    """
    if as_identity not in ("agent", "user"):
        raise PipelineError(f"as_identity must be 'agent' or 'user', got {as_identity!r}")
    if not TOKEN:
        raise PipelineError(
            "BRITIVE_API_TOKEN is not set. This server needs a Britive SERVICE "
            "identity token - on-behalf-of impersonation cannot be done with a "
            "plain user token."
        )

    app, env, profile = _split_profile(PROFILE)
    headers = {"X-On-Behalf-Of": OBO_USER} if as_identity == "user" else {}
    br = Britive(tenant=TENANT, token=TOKEN)

    try:
        result = br.my_access.checkout_by_name(
            profile_name=profile,
            environment_name=env,
            application_name=app,
            headers=headers,
            include_credentials=True,
        )
    except Exception as exc:  # noqa: BLE001 - surface Britive's message verbatim
        raise PipelineError(
            f"Britive checkout of '{PROFILE}' as "
            f"{'the human ' + OBO_USER if as_identity == 'user' else 'the agent'} "
            f"failed: {exc}"
        ) from exc

    try:
        yield result.get("credentials", result)
    finally:
        # Never let a checkin failure mask a successful read.
        try:
            br.my_access.checkin_by_name(
                profile_name=profile,
                environment_name=env,
                application_name=app,
                headers=headers,
            )
        except Exception:  # noqa: BLE001
            pass


def _identity_label(as_identity: str) -> str:
    return (
        f"the human {OBO_USER} (impersonated)"
        if as_identity == "user"
        else "the AI agent itself"
    )


def _client(creds: dict, service: str = "s3"):
    import boto3

    return boto3.client(
        service,
        aws_access_key_id=creds["accessKeyID"],
        aws_secret_access_key=creds["secretAccessKey"],
        aws_session_token=creds["sessionToken"],
    )


# ── Analysis ────────────────────────────────────────────────────────────────
def _resolve_date(batch_date: str) -> str:
    d = (batch_date or "").strip().lower()
    if d in ("", "today", "latest"):
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")
    try:
        _date.fromisoformat(d)
    except ValueError as exc:
        raise PipelineError(
            f"batch_date must be YYYY-MM-DD (or 'today'), got {batch_date!r}"
        ) from exc
    return d


def _analyze(raw: bytes, key: str) -> str:
    rows = list(csv.DictReader(io.StringIO(raw.decode("utf-8"))))
    if not rows:
        return f"`{key}` parsed to zero rows."

    digest = hashlib.sha256(raw).hexdigest()
    mrr = sum(int(r.get("mrr_usd") or 0) for r in rows)
    plans = Counter(r.get("plan", "?") for r in rows)
    regions = Counter(r.get("region", "?") for r in rows)

    # Named so the demo can point at it: these are the columns that make the
    # batch worth controlling access to in the first place.
    personal = [c for c in ("first_name", "last_name", "email", "phone") if c in rows[0]]

    lines = [
        f"**{key}** - {len(rows)} records, sha256 `{digest[:16]}...`",
        "",
        f"- Total MRR: **${mrr:,}**",
        "- By plan: " + ", ".join(f"{p} {n}" for p, n in plans.most_common()),
        "- By region: " + ", ".join(f"{r} {n}" for r, n in regions.most_common()),
        f"- Personal data in this batch: {', '.join(personal)}",
        "",
        "Top 5 by MRR:",
    ]
    for r in sorted(rows, key=lambda r: int(r.get("mrr_usd") or 0), reverse=True)[:5]:
        lines.append(
            f"  - {r.get('company')} ({r.get('plan')}) "
            f"${int(r.get('mrr_usd') or 0):,} - {r.get('region')}"
        )
    return "\n".join(lines)


def _footer(as_identity: str) -> str:
    return (
        f"\n\n---\n_Read as {_identity_label(as_identity)}, using a Britive credential "
        f"that was checked out for this call and checked back in before this text was "
        f"returned. No standing cloud credential exists._"
    )


# ── Tools ───────────────────────────────────────────────────────────────────
@mcp.tool()
def pipeline_status() -> str:
    """Show how the nightly pipeline is wired and prove there is no standing access.

    No checkout happens here - this is configuration only. Use it to open the
    demo: it names the three identities that touch the data and shows that this
    server holds no cloud credential at rest.
    """
    lines = [
        "**Nightly JIT pipeline - configuration**",
        "",
        f"Britive tenant: `{TENANT}`",
        f"Reader profile: `{PROFILE}`",
        f"Bucket:         `s3://{BUCKET}`",
        f"Batch layout:   `{DAILY_PREFIX}/<YYYY-MM-DD>.csv` (+ `.manifest.json`)",
        f"Impersonates:   `{OBO_USER}` when a tool is called with as_identity='user'",
        "",
        "**Three identities touch one file:**",
        "1. GitHub Actions (federated service identity) WRITES it nightly - unattended, "
        "no static key.",
        "2. `as_identity='agent'` READS it as the bot - the bot's own grant bounds what it sees.",
        f"3. `as_identity='user'` READS it as {OBO_USER} - the agent sees it *because the "
        "human can*, and the audit log names the human.",
        "",
        "This server stores no cloud credential. Each tool call checks the profile "
        "out, does one thing, and checks it back in.",
    ]
    if not TOKEN:
        lines += ["", "WARNING: BRITIVE_API_TOKEN is not set - every checkout will fail."]
    return "\n".join(lines)


@mcp.tool()
def whoami(as_identity: str = "user") -> str:
    """Show the actual AWS identity a read would run as - the impersonation proof.

    Call it twice, once with as_identity="agent" and once with "user", to see
    the same Britive service identity produce two different cloud sessions:
    the bot, and the human it is acting for.
    """
    try:
        with jit_credentials(as_identity) as creds:
            ident = _client(creds, "sts").get_caller_identity()
    except PipelineError as exc:
        return f"Error: {exc}"

    tail = (
        "_The session name is the HUMAN. Britive issued this credential to the AI "
        "service identity acting on behalf of that person, so every AWS API call "
        "here is attributed to them._"
        if as_identity == "user"
        else "_The session name is the BOT's own service identity - the AI acting as "
        "itself, bounded by its own grant._"
    )
    return (
        f"**as_identity='{as_identity}'** -> {_identity_label(as_identity)}\n\n"
        f"- Account: `{ident['Account']}`\n"
        f"- ARN: `{ident['Arn']}`\n\n" + tail
    )


@mcp.tool()
def list_batches(limit: int = 10, as_identity: str = "user") -> str:
    """List the most recent nightly batches - proof the pipeline runs.

    Each entry was written by a GitHub Actions run using a just-in-time
    credential that no longer exists.
    """
    try:
        with jit_credentials(as_identity) as creds:
            pages = _client(creds).get_paginator("list_objects_v2").paginate(
                Bucket=BUCKET, Prefix=f"{DAILY_PREFIX}/"
            )
            objs = [o for page in pages for o in page.get("Contents", [])]
    except PipelineError as exc:
        return f"Error: {exc}"
    except Exception as exc:  # noqa: BLE001
        return f"Could not list s3://{BUCKET}/{DAILY_PREFIX}/: {exc}"

    if not objs:
        return (
            f"No batches under `{DAILY_PREFIX}/` in s3://{BUCKET}. "
            "Has the nightly workflow run yet?"
        )

    objs.sort(key=lambda o: o["LastModified"], reverse=True)
    lines = ["**Recent batches** (newest first)", "", "| Modified (UTC) | Size | Object |", "|---|---|---|"]
    for o in objs[: max(1, min(limit, 100))]:
        lines.append(
            f"| {o['LastModified'].strftime('%Y-%m-%d %H:%M')} | {o['Size']} | `{o['Key']}` |"
        )
    return "\n".join(lines) + _footer(as_identity)


@mcp.tool()
def read_batch(batch_date: str = "today", as_identity: str = "user") -> str:
    """Read and analyze a nightly intake batch: volume, MRR, plan and region mix.

    batch_date is YYYY-MM-DD, or "today" for the most recent nightly drop.
    Defaults to reading AS THE HUMAN (as_identity="user") - the agent gets to
    this data through the user's access, not its own.
    """
    try:
        key = f"{DAILY_PREFIX}/{_resolve_date(batch_date)}.csv"
        with jit_credentials(as_identity) as creds:
            raw = _client(creds).get_object(Bucket=BUCKET, Key=key)["Body"].read()
        return _analyze(raw, key) + _footer(as_identity)
    except PipelineError as exc:
        return f"Error: {exc}"
    except Exception as exc:  # noqa: BLE001
        return (
            f"Could not read the batch for {batch_date}: {exc}\n\n"
            "If this is a 'no such key' error, the nightly workflow may not have run "
            "for that date yet - try `list_batches` to see what exists."
        )


@mcp.tool()
def read_manifest(batch_date: str = "today", as_identity: str = "user") -> str:
    """Read the sidecar manifest: which identity wrote this batch, from which run."""
    try:
        key = f"{DAILY_PREFIX}/{_resolve_date(batch_date)}.manifest.json"
        with jit_credentials(as_identity) as creds:
            raw = _client(creds).get_object(Bucket=BUCKET, Key=key)["Body"].read()
        m = json.loads(raw.decode("utf-8"))
    except PipelineError as exc:
        return f"Error: {exc}"
    except Exception as exc:  # noqa: BLE001
        return f"Could not read the manifest for {batch_date}: {exc}"

    lines = [f"**Manifest for {m.get('batch_date', batch_date)}**", ""]
    for label, field in [
        ("Written at", "written_at"),
        ("Written by", "written_by"),
        ("Auth path", "auth_path"),
        ("Static creds", "static_creds"),
        ("GitHub run", "run_id"),
        ("Repo", "repo"),
        ("Triggered by", "actor"),
        ("Rows", "rows"),
        ("sha256", "sha256"),
    ]:
        if m.get(field) not in (None, ""):
            lines.append(f"- {label}: `{m[field]}`")
    return "\n".join(lines) + _footer(as_identity)


if __name__ == "__main__":
    mcp.run()
