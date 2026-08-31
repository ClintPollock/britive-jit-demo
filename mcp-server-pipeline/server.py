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

SCOPE: AWS and GCP. Every read tool takes cloud="aws" (default) or "gcp" and
reads the same nightly batch from S3 or from GCS - two clouds, two JIT
identities, byte-identical content. Azure is still write-only: the workflow
drops the batch there, but no reader profile exists and the Azure app cannot
scope a profile below the subscription anyway.

Impersonation is AWS-only. The GCP reader profile is granted to the human
exactly as the AWS one is, but under X-On-Behalf-Of Britive reports "profile
not found", so cloud="gcp" refuses as_identity="user" rather than quietly
reading as the bot. Verified 2026-08-31.

Auth: needs a Britive service-identity token (the "AI" identity) in
BRITIVE_API_TOKEN. On-behalf-of REQUIRES a service identity - a plain user
token cannot impersonate.
"""

from __future__ import annotations

import atexit
import contextlib
import csv
import hashlib
import io
import json
import os
import time
from collections import Counter
from datetime import datetime, timezone, date as _date

import boto3
from britive.britive import Britive
from mcp.server.fastmcp import FastMCP

# ── Config (override via env in .mcp.json) ──────────────────────────────────
TENANT = os.environ.get("BRITIVE_TENANT", "cpollock-tenant")
TOKEN = os.environ.get("BRITIVE_API_TOKEN", "")

# The human the agent impersonates when as_identity="user".
OBO_USER = os.environ.get("BRITIVE_OBO_USER", "clint.pollock@jit-zsp.com")

DAILY_PREFIX = os.environ.get("PIPELINE_DAILY_PREFIX", "daily")
BUCKET = os.environ.get("PIPELINE_S3_BUCKET", "britive-jit-demo-696226360299")

# One profile per identity mode, because of a measured Britive behaviour:
# checking the SAME profile out again as the SAME identity shortly after a
# checkin blocks while the previous grant is torn down. Measured on this tenant:
#
#     cold, as the human ................  3.9s
#     immediately again, same identity ... 39.7s
#     immediately, different identity ....  3.5s
#     same identity after a 30s settle ... 25.5s
#
# Two consecutive reads as the human - which is exactly what the demo does when
# you ask a question and then ask who it ran as - therefore hit the slow path.
# Point PIPELINE_AWS_AGENT_PROFILE at a SECOND reader profile and the two modes
# stop colliding. Left unset it falls back to the same profile, which still
# works; it is just slower back to back.
PROFILE = os.environ.get("PIPELINE_AWS_PROFILE", "AWS Standalone/0299-CP/AWS-S3-Reader")
AGENT_PROFILE = os.environ.get("PIPELINE_AWS_AGENT_PROFILE", PROFILE)

# The GCP leg. The workflow writes the same batch to GCS; this reads it back.
#
# Two things about this profile that cost time to work out, both verified
# against the live tenant on 2026-08-31:
#
#  * The environment name is "Google Cloud" (the root environment group), NOT
#    "cpollock-poc-core". The Google Cloud app is hierarchical, so my_access
#    surfaces the root group rather than the project the profile is scoped to.
#    Passing the project name gives "profile found but not in environment".
#  * On-behalf-of does NOT work here - see _profile_for(). The profile is
#    granted to clint.pollock@jit-zsp.com exactly as the working AWS one is,
#    but under X-On-Behalf-Of the profile is invisible and checkout raises
#    "profile not found". Left refusing rather than silently reading as the bot.
GCP_PROFILE = os.environ.get(
    "PIPELINE_GCP_PROFILE", "Google Cloud/Google Cloud/GCP-JIT-Demo-Reader"
)
GCS_BUCKET = os.environ.get("PIPELINE_GCS_BUCKET", "britive-jit-demo-1095450080757")

# GCP grants are not usable the instant checkout returns - Britive mints a
# fresh service account and the IAM binding has to propagate. Measured on this
# tenant: ~15s at best, ~75s at worst. AWS needs none of this.
#
# POLL FAST, GIVE UP EARLY. The first version slept 15s between tries for up to
# 210s, which blew past the MCP client's per-tool timeout. Claude then RETRIED
# the tool - and because every checkout mints a *new* service account, each
# retry started a fresh propagation wait. Observed in Claude Desktop: 2m34s,
# then 3m56s, never completing. A tight poll finds the grant within a second or
# two of it landing, and a deadline under the client timeout turns a hang into
# an answer Claude can act on instead of retrying into a worse hole.
GCP_PROPAGATION_DEADLINE = float(os.environ.get("PIPELINE_GCP_PROPAGATION_DEADLINE", "45"))
GCP_PROPAGATION_INTERVAL = float(os.environ.get("PIPELINE_GCP_PROPAGATION_INTERVAL", "2"))

# Which JIT service accounts have already been seen to work. Keyed by the
# ephemeral SA email, so it is empty again on the next checkout - this caches
# "the grant landed", never a credential. Stops _fetch_batch paying the
# propagation wait twice inside one tool call.
_GCP_LIVE: set[str] = set()

# GCP credentials are HELD between calls; AWS ones are not.
#
# Why the asymmetry: a GCP checkout costs ~60s of Google IAM propagation before
# the grant works, which is at or past the MCP client's per-tool timeout. Strict
# per-call checkin means every single GCP read pays that toll and most of them
# time out. So the GCP credential is checked out once, reused for
# PIPELINE_GCP_SESSION_TTL seconds, then released. AWS is untouched and still
# checks in before the tool returns - the stricter story stays available on the
# cloud that can afford it.
#
# What this costs, say it plainly on stage: for GCP the credential lives for the
# conversation, not for the single call. It is still just-in-time and still
# expires on its own; it is not per-question.
GCP_SESSION_TTL = float(os.environ.get("PIPELINE_GCP_SESSION_TTL", "600"))
_GCP_SESSION: dict | None = None
_GCP_ATEXIT = False

CLOUDS = ("aws", "gcp")


def _check_cloud(cloud: str) -> str:
    c = (cloud or "aws").strip().lower()
    if c not in CLOUDS:
        raise PipelineError(f"cloud must be one of {CLOUDS}, got {cloud!r}")
    return c


def _bucket_for(cloud: str) -> str:
    return GCS_BUCKET if cloud == "gcp" else BUCKET


def _bucket_uri(cloud: str) -> str:
    return f"gs://{GCS_BUCKET}" if cloud == "gcp" else f"s3://{BUCKET}"


def _profile_for(as_identity: str, cloud: str = "aws") -> str:
    if cloud == "gcp":
        if as_identity == "user":
            raise PipelineError(
                "GCP reads cannot run on behalf of a human. The GCP reader profile is "
                "granted to the human, but under X-On-Behalf-Of Britive reports "
                "'profile not found'. Impersonation is proven on AWS only - call this "
                "with as_identity='agent' for GCP, or read AWS to see the contrast. "
                "Refusing rather than silently reading as the bot."
            )
        return GCP_PROFILE
    return AGENT_PROFILE if as_identity == "agent" else PROFILE

mcp = FastMCP("britive-jit-pipeline")


class PipelineError(RuntimeError):
    pass


# ── Warm caches (latency, not behaviour) ────────────────────────────────────
# Profiling one read: checkout 3.9s, checkin 0.8s, S3 0.8s, and ~0.65s of pure
# client construction that we were paying on EVERY call — a fresh Britive()
# each time (~0.32s warm) plus a fresh boto3 client (~0.33s, mostly loading the
# S3 service model). Both are reusable. Note this caches CLIENTS, never
# credentials: every tool call still does its own checkout and checkin, which
# is the whole point of the demo.
_BRITIVE: Britive | None = None
_SESSION = boto3.session.Session()


def _britive(fresh: bool = False) -> Britive:
    global _BRITIVE
    if fresh or _BRITIVE is None:
        _BRITIVE = Britive(tenant=TENANT, token=TOKEN)
    return _BRITIVE


# ── Britive checkout / checkin ──────────────────────────────────────────────
def _split_profile(path: str) -> tuple[str, str, str]:
    parts = [p.strip() for p in path.split("/")]
    if len(parts) != 3:
        raise PipelineError(
            f"Profile path must be 'Application/Environment/Profile', got {path!r}"
        )
    return parts[0], parts[1], parts[2]


def _checkout_profile(profile_path: str, headers: dict, as_identity: str) -> dict:
    app, env, profile = _split_profile(profile_path)

    def _go(br: Britive):
        return br.my_access.checkout_by_name(
            profile_name=profile,
            environment_name=env,
            application_name=app,
            headers=headers,
            include_credentials=True,
        )

    br = _britive()
    try:
        return _go(br)
    except Exception:  # noqa: BLE001
        # The cached client may have gone stale between demos. Rebuild once
        # before giving up — cheap insurance against a dead session on stage.
        try:
            return _go(_britive(fresh=True))
        except Exception as exc:  # noqa: BLE001 - surface Britive's message verbatim
            raise PipelineError(
                f"Britive checkout of '{profile_path}' as "
                f"{'the human ' + OBO_USER if as_identity == 'user' else 'the agent'} "
                f"failed: {exc}"
            ) from exc


def _checkin_profile(profile_path: str, headers: dict) -> None:
    """Never let a checkin failure mask a successful read."""
    app, env, profile = _split_profile(profile_path)
    try:
        _britive().my_access.checkin_by_name(
            profile_name=profile,
            environment_name=env,
            application_name=app,
            headers=headers,
        )
    except Exception:  # noqa: BLE001
        pass


def _release_gcp_session() -> None:
    global _GCP_SESSION
    if _GCP_SESSION:
        _checkin_profile(_GCP_SESSION["profile"], _GCP_SESSION["headers"])
        _GCP_LIVE.discard(_GCP_SESSION.get("sa", ""))
        _GCP_SESSION = None


def _gcp_session(profile_path: str, headers: dict) -> dict:
    """Return a live GCP credential, checking one out only when needed.

    The session is cached BEFORE the propagation wait, deliberately. If the
    wait runs past the client's timeout, the credential is still held, so the
    caller's next attempt reuses the same service account - which by then has
    propagated - and returns immediately. Without that, every retry minted a
    fresh account and restarted the wait, which is how this hung for minutes.
    """
    global _GCP_SESSION
    now = time.monotonic()
    if _GCP_SESSION and now < _GCP_SESSION["expires_at"]:
        return _GCP_SESSION["creds"]

    _release_gcp_session()
    result = _checkout_profile(profile_path, headers, "agent")
    creds = result.get("credentials", result)
    _GCP_SESSION = {
        "creds": creds,
        "sa": _gcs_account(creds),
        "profile": profile_path,
        "headers": headers,
        "expires_at": now + GCP_SESSION_TTL,
    }
    global _GCP_ATEXIT
    if not _GCP_ATEXIT:
        atexit.register(_release_gcp_session)
        _GCP_ATEXIT = True
    return creds


@contextlib.contextmanager
def jit_credentials(as_identity: str, cloud: str = "aws"):
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

    cloud = _check_cloud(cloud)
    profile_path = _profile_for(as_identity, cloud)
    app, env, profile = _split_profile(profile_path)
    headers = {"X-On-Behalf-Of": OBO_USER} if as_identity == "user" else {}

    if cloud == "gcp":
        # Held, not per-call. See the GCP_SESSION_TTL note above.
        yield _gcp_session(profile_path, headers)
        return

    result = _checkout_profile(profile_path, headers, as_identity)
    try:
        yield result.get("credentials", result)
    finally:
        _checkin_profile(profile_path, headers)


def _identity_label(as_identity: str) -> str:
    return (
        f"the human {OBO_USER} (impersonated)"
        if as_identity == "user"
        else "the AI agent itself"
    )


def _gcs_client(creds: dict):
    """Build a GCS client from a Britive GCP checkout.

    Britive returns `{"<jit-sa-email>": <service-account key JSON>}` - one
    freshly-minted service account per checkout - so the key has to be
    unwrapped out of that single-entry dict. The workflow's GCP job does the
    same thing in bash.
    """
    from google.cloud import storage
    from google.oauth2 import service_account

    try:
        _sa_email, payload = next(iter(creds.items()))
    except StopIteration as exc:
        raise PipelineError("GCP checkout returned no credential") from exc
    key = payload if isinstance(payload, dict) else json.loads(payload)
    return storage.Client(
        project=key.get("project_id"),
        credentials=service_account.Credentials.from_service_account_info(key),
    )


def _gcs_account(creds: dict) -> str:
    """The JIT service account email Britive minted for this checkout."""
    try:
        return next(iter(creds.keys()))
    except StopIteration:
        return "unknown"


def _gcp_await_propagation(client, bucket: str, sa_email: str = ""):
    """Poll until the fresh IAM binding is live, or give up inside the deadline.

    Not a cosmetic retry: the binding genuinely is not there yet when checkout
    returns, and every call 403s until it lands.
    """
    if sa_email and sa_email in _GCP_LIVE:
        return 0.0

    deadline = time.monotonic() + GCP_PROPAGATION_DEADLINE
    started, last = time.monotonic(), None
    while True:
        try:
            next(iter(client.list_blobs(bucket, max_results=1)), None)
            if sa_email:
                _GCP_LIVE.add(sa_email)
            return round(time.monotonic() - started, 1)
        except Exception as exc:  # noqa: BLE001 - 403 while the grant propagates
            last = exc
            if time.monotonic() + GCP_PROPAGATION_INTERVAL >= deadline:
                break
            time.sleep(GCP_PROPAGATION_INTERVAL)

    waited = round(time.monotonic() - started)
    raise PipelineError(
        f"The GCP grant has not finished propagating after {waited}s - Britive "
        "minted a new service account and Google's IAM binding is still landing. "
        "The credential is HELD, so simply ask again in a few seconds and the "
        "same service account will be reused with no further wait. Tell the user "
        f"it is still warming up rather than reporting a failure. ({last})"
    )


def _client(creds: dict, service: str = "s3"):
    # Reuse the module-level Session so the service model is loaded once, not
    # per call. The credentials are still per-checkout and never cached.
    return _SESSION.client(
        service,
        aws_access_key_id=creds["accessKeyID"],
        aws_secret_access_key=creds["secretAccessKey"],
        aws_session_token=creds["sessionToken"],
    )


# ── Storage (one shape for both clouds) ─────────────────────────────────────
def _list_objects(creds: dict, cloud: str, prefix: str) -> list[tuple[str, int, object]]:
    """Return [(key, size, modified)] under `prefix`, newest-agnostic."""
    if cloud == "gcp":
        client = _gcs_client(creds)
        _gcp_await_propagation(client, GCS_BUCKET, _gcs_account(creds))
        return [
            (b.name, b.size or 0, b.updated)
            for b in client.list_blobs(GCS_BUCKET, prefix=prefix)
        ]
    pages = _client(creds).get_paginator("list_objects_v2").paginate(
        Bucket=BUCKET, Prefix=prefix
    )
    return [
        (o["Key"], o["Size"], o["LastModified"])
        for page in pages
        for o in page.get("Contents", [])
    ]


def _get_bytes(creds: dict, cloud: str, key: str) -> bytes:
    if cloud == "gcp":
        client = _gcs_client(creds)
        _gcp_await_propagation(client, GCS_BUCKET, _gcs_account(creds))
        return client.bucket(GCS_BUCKET).blob(key).download_as_bytes()
    return _client(creds).get_object(Bucket=BUCKET, Key=key)["Body"].read()


def _cloud_identity(creds: dict, cloud: str) -> str | None:
    """The cloud principal this credential actually is."""
    try:
        if cloud == "gcp":
            return _gcs_account(creds)
        return _client(creds, "sts").get_caller_identity()["Arn"]
    except Exception:  # noqa: BLE001 - never fail a read over this
        return None


# ── Analysis ────────────────────────────────────────────────────────────────
def _resolve_date(batch_date: str) -> str | None:
    """YYYY-MM-DD, or None meaning "whatever is newest in the bucket".

    "latest" used to be an alias for today's date, which made the most natural
    opening question in the demo - "what did the pipeline drop?" - fail with a
    404 on any day the workflow had not run. It now means what it says, and is
    resolved against the bucket at read time.
    """
    d = (batch_date or "").strip().lower()
    if d in ("", "latest", "newest", "most recent"):
        return None
    if d == "today":
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")
    try:
        _date.fromisoformat(d)
    except ValueError as exc:
        raise PipelineError(
            f"batch_date must be YYYY-MM-DD, 'today', or 'latest' (the default), "
            f"got {batch_date!r}"
        ) from exc
    return d


def _latest_batch_date(creds: dict, cloud: str) -> str:
    """The newest batch date actually present, from the object keys."""
    dates = set()
    for key, _size, _modified in _list_objects(creds, cloud, f"{DAILY_PREFIX}/"):
        name = key.rsplit("/", 1)[-1]
        if not name.endswith(".csv"):
            continue
        stem = name[: -len(".csv")]
        try:
            _date.fromisoformat(stem)
        except ValueError:
            continue
        dates.add(stem)
    if not dates:
        raise PipelineError(
            f"No batches at all under `{DAILY_PREFIX}/` in {_bucket_uri(cloud)}. "
            "Has the nightly workflow ever run?"
        )
    return max(dates)


def _fetch_batch(
    creds: dict, cloud: str, wanted: str | None, suffix: str
) -> tuple[bytes, str, str]:
    """Fetch a batch file, falling back to the newest one that exists.

    Returns (bytes, key, note). `note` is non-empty only when the caller asked
    for a specific date that is not there - the fallback is always announced,
    never silent, because "today's batch" showing last Friday's numbers would
    be a lie the demo cannot afford.
    """
    if wanted is not None:
        key = f"{DAILY_PREFIX}/{wanted}{suffix}"
        try:
            return _get_bytes(creds, cloud, key), key, ""
        except PipelineError:
            raise
        except Exception:  # noqa: BLE001 - missing object; fall back below
            pass

    newest = _latest_batch_date(creds, cloud)
    key = f"{DAILY_PREFIX}/{newest}{suffix}"
    raw = _get_bytes(creds, cloud, key)
    note = ""
    if wanted is not None and wanted != newest:
        note = (
            f"> There is no batch for **{wanted}** in {_bucket_uri(cloud)} - the "
            f"nightly workflow has not run for that date. Showing the most recent "
            f"batch instead, **{newest}**.\n\n"
        )
    return raw, key, note


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


def _footer(as_identity: str, arn: str | None = None, cloud: str = "aws") -> str:
    # Reporting the ARN here is a latency fix as much as a clarity one. Asking
    # "who did that read run as?" used to trigger a second whoami, and a second
    # checkout of the SAME profile as the SAME identity right after a checkin is
    # the pathological case — measured at 25-49s against ~4s cold. Attaching the
    # identity to the read that actually used it costs one extra STS call on a
    # credential already in hand (~0.3s) and removes that checkout entirely.
    ident = f"\n_Ran as:_ `{arn}`" if arn else ""
    if cloud == "gcp":
        # Honest about the trade-off made for GCP - see GCP_SESSION_TTL.
        lifetime = (
            "using a Britive credential checked out for this conversation and released "
            f"automatically after {int(GCP_SESSION_TTL // 60)} minutes. Held rather than "
            "per-call because a GCP checkout costs ~60s of IAM propagation. No standing "
            "cloud credential exists."
        )
    else:
        lifetime = (
            "using a Britive credential that was checked out for this call and checked "
            "back in before this text was returned. No standing cloud credential exists."
        )
    return f"\n\n---\n_Read as {_identity_label(as_identity)}, {lifetime}_{ident}"


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
        f"Reader profile (as you):    `{PROFILE}`",
        f"Reader profile (as agent):  `{AGENT_PROFILE}`"
        + ("  — same profile; set PIPELINE_AWS_AGENT_PROFILE to a second one "
           "if back-to-back calls feel slow" if AGENT_PROFILE == PROFILE else ""),
        f"AWS bucket:     `s3://{BUCKET}`",
        f"GCS bucket:     `gs://{GCS_BUCKET}`  (profile `{GCP_PROFILE}`)",
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
def whoami(as_identity: str = "user", cloud: str = "aws") -> str:
    """Which cloud identity a read runs as - the impersonation contrast.

    USE THIS for "who did that run as?", "show me the same thing as the agent",
    "run it as yourself instead of me", "whose credentials were used?".

    cloud="aws" (default) or "gcp". On GCP this returns the short-lived service
    account Britive minted for this one call - a different one every time.

    as_identity="user" (default) acts on behalf of the human; "agent" acts as
    the bot's own service identity. Same Britive token either way - only the
    resulting cloud session differs.
    """
    try:
        cloud = _check_cloud(cloud)
        with jit_credentials(as_identity, cloud) as creds:
            if cloud == "gcp":
                principal, account = _gcs_account(creds), "cpollock-poc-core"
            else:
                ident = _client(creds, "sts").get_caller_identity()
                principal, account = ident["Arn"], ident["Account"]
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
        f"**as_identity='{as_identity}'**, cloud='{cloud}' -> "
        f"{_identity_label(as_identity)}\n\n"
        f"- {'Project' if cloud == 'gcp' else 'Account'}: `{account}`\n"
        f"- {'Service account' if cloud == 'gcp' else 'ARN'}: `{principal}`\n\n" + tail
    )


@mcp.tool()
def list_batches(limit: int = 10, as_identity: str = "user", cloud: str = "aws") -> str:
    """History of the nightly pipeline: which batches exist in S3 and when.

    USE THIS for "what batches are there?", "how far back does this go?",
    "has it been running?", "show me recent drops".

    Each entry was written by a GitHub Actions run using a just-in-time
    credential that no longer exists.

    cloud="aws" (default) reads S3; cloud="gcp" reads the GCS copy of the same
    nightly batch. Use it for "is it in GCP too?" or "compare the clouds".

    cloud="gcp" is SLOWER than aws - Britive mints a fresh service account and
    Google's IAM binding takes time to propagate. Expect roughly 10-45s. If it
    returns a "grant had not propagated" message, do NOT call it again straight
    away: a retry checks out a different service account and restarts the wait.
    Say so and move on, or read AWS instead.
    """
    try:
        cloud = _check_cloud(cloud)
        with jit_credentials(as_identity, cloud) as creds:
            objs = _list_objects(creds, cloud, f"{DAILY_PREFIX}/")
    except PipelineError as exc:
        return f"Error: {exc}"
    except Exception as exc:  # noqa: BLE001
        return f"Could not list {_bucket_uri(cloud)}/{DAILY_PREFIX}/: {exc}"

    if not objs:
        return (
            f"No batches under `{DAILY_PREFIX}/` in {_bucket_uri(cloud)}. "
            "Has the nightly workflow run yet?"
        )

    objs.sort(key=lambda o: o[2], reverse=True)
    lines = [
        f"**Recent batches in {_bucket_uri(cloud)}** (newest first)",
        "", "| Modified (UTC) | Size | Object |", "|---|---|---|",
    ]
    for key, size, modified in objs[: max(1, min(limit, 100))]:
        lines.append(f"| {modified.strftime('%Y-%m-%d %H:%M')} | {size} | `{key}` |")
    return "\n".join(lines) + _footer(as_identity, cloud=cloud)


@mcp.tool()
def read_batch(batch_date: str = "latest", as_identity: str = "user", cloud: str = "aws") -> str:
    """What the nightly data pipeline dropped today: customer intake batch from S3.

    USE THIS for questions like "what did the pipeline drop today?", "what came
    in overnight?", "what's in today's batch?", "summarise today's customer
    data", "how much MRR came in?". This is the nightly GitHub Actions pipeline
    that writes a customer intake batch to S3 - not a CI/CD, Snowflake, or
    sales pipeline.

    Returns volume, total MRR, plan and region mix, top accounts, and the AWS
    identity the read actually ran as.

    batch_date defaults to "latest" - the newest batch actually in the bucket,
    which is what you want for "what did the pipeline drop?". Pass "today" for
    strictly today's date, or an explicit YYYY-MM-DD. If the date you ask for
    is not there, this reads the newest one and says so.
    Defaults to reading AS THE HUMAN (as_identity="user") - the agent gets to
    this data through the user's access, not its own.

    cloud="aws" (default) or "gcp" - the same batch, written to two clouds by
    two independent JIT identities, so the sha256 in the output should match
    across both. cloud="gcp" requires as_identity="agent".

    cloud="gcp" is SLOWER than aws - Britive mints a fresh service account and
    Google's IAM binding takes time to propagate. Expect roughly 10-45s. If it
    returns a "grant had not propagated" message, do NOT call it again straight
    away: a retry checks out a different service account and restarts the wait.
    Say so and move on, or read AWS instead.
    """
    try:
        cloud = _check_cloud(cloud)
        wanted = _resolve_date(batch_date)
        with jit_credentials(as_identity, cloud) as creds:
            raw, key, note = _fetch_batch(creds, cloud, wanted, ".csv")
            # Same credential, one extra call — so "who did that run as?" needs
            # no second checkout. See the note in _footer().
            arn = _cloud_identity(creds, cloud)
        return note + _analyze(raw, f"{_bucket_uri(cloud)}/{key}") + _footer(as_identity, arn, cloud)
    except PipelineError as exc:
        return f"Error: {exc}"
    except Exception as exc:  # noqa: BLE001
        return (
            f"Could not read the batch for {batch_date}: {exc}\n\n"
            "If this is a 'no such key'/404 error, the nightly workflow may not have "
            "run for that date yet - try `list_batches` to see what exists."
        )


@mcp.tool()
def read_manifest(batch_date: str = "latest", as_identity: str = "user", cloud: str = "aws") -> str:
    """Read the sidecar manifest: which identity wrote this batch, from which run.

    cloud="aws" (default) or "gcp". The manifests differ between clouds by
    design - each records the JIT identity that wrote that copy.

    batch_date defaults to "latest" - the newest batch present.
    """
    try:
        cloud = _check_cloud(cloud)
        wanted = _resolve_date(batch_date)
        note = ""
        with jit_credentials(as_identity, cloud) as creds:
            raw, _key, note = _fetch_batch(creds, cloud, wanted, ".manifest.json")
        m = json.loads(raw.decode("utf-8"))
    except PipelineError as exc:
        return f"Error: {exc}"
    except Exception as exc:  # noqa: BLE001
        return f"Could not read the manifest for {batch_date}: {exc}"

    lines = [f"{note}**Manifest for {m.get('batch_date', batch_date)}**", ""]
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
    return "\n".join(lines) + _footer(as_identity, cloud=cloud)


if __name__ == "__main__":
    mcp.run()
