"""
britive-jit-pipeline MCP server
===============================

Lets Claude read the nightly intake batch that the GitHub Actions workflow
drops into S3 / GCS / Azure Blob - with NO standing cloud credentials, and,
critically, with a choice of WHO the read runs as.

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
              because the agent holds its own key. Every CloudTrail record
              names the person, not the bot.

`whoami` returns the actual cloud identity for either mode, so the contrast
is demonstrable rather than asserted.

Every tool call is checkout -> do the work -> checkin. The credential's
lifetime is the tool call. Each call is a row in the Britive audit log.

Auth: this server needs a Britive service-identity token (the "AI" identity)
in BRITIVE_API_TOKEN. On-behalf-of REQUIRES a service identity - a plain user
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

# One entry per cloud: the Britive profile to check out, and where the batch
# lands. Profile paths are "Application/Environment/Profile".
CLOUDS = {
    "aws": {
        "profile": os.environ.get(
            "PIPELINE_AWS_PROFILE", "AWS Standalone/0299-CP/AWS-S3-Reader"
        ),
        "bucket": os.environ.get("PIPELINE_S3_BUCKET", "britive-jit-demo-696226360299"),
    },
    "gcp": {
        "profile": os.environ.get(
            "PIPELINE_GCP_PROFILE", "Google Cloud/Google Cloud/cpollock-pov-gcs-admin"
        ),
        "bucket": os.environ.get("PIPELINE_GCS_BUCKET", "britive-jit-demo-1095450080757"),
    },
    "azure": {
        "profile": os.environ.get(
            "PIPELINE_AZURE_PROFILE",
            "Azure BritiveSE/Azure BritiveSE/cpollock-pov-azure-blob-writer",
        ),
        "account": os.environ.get("PIPELINE_AZURE_ACCOUNT", "britivejitdemo4bbb4f1c"),
        "container": os.environ.get("PIPELINE_AZURE_CONTAINER", "jit-demo"),
    },
}

# Impersonation is wired for AWS only today. The AWS-S3-Reader profile is the
# one with a proven on-behalf-of path (see obo-checkout.py in the POV folder).
# Rather than silently reading as the bot when the caller asked for the human,
# the GCP/Azure paths refuse as_identity="user" outright.
OBO_SUPPORTED = {"aws"}

mcp = FastMCP("britive-jit-pipeline")


class PipelineError(RuntimeError):
    pass


# ── Britive checkout / checkin ──────────────────────────────────────────────
def _client() -> Britive:
    if not TOKEN:
        raise PipelineError(
            "BRITIVE_API_TOKEN is not set. This server needs a Britive SERVICE "
            "identity token - on-behalf-of impersonation cannot be done with a "
            "plain user token."
        )
    return Britive(tenant=TENANT, token=TOKEN)


def _split_profile(path: str) -> tuple[str, str, str]:
    parts = [p.strip() for p in path.split("/")]
    if len(parts) != 3:
        raise PipelineError(
            f"Profile path must be 'Application/Environment/Profile', got {path!r}"
        )
    app, env, profile = parts
    return app, env, profile


@contextlib.contextmanager
def jit_credentials(cloud: str, as_identity: str):
    """Check out a Britive profile, yield its credentials, always check back in.

    as_identity="user" adds the X-On-Behalf-Of header, which makes the cloud
    session the HUMAN rather than the service identity.
    """
    if cloud not in CLOUDS:
        raise PipelineError(f"Unknown cloud {cloud!r}. Use one of: {', '.join(CLOUDS)}")
    if as_identity not in ("agent", "user"):
        raise PipelineError(f"as_identity must be 'agent' or 'user', got {as_identity!r}")
    if as_identity == "user" and cloud not in OBO_SUPPORTED:
        raise PipelineError(
            f"Impersonation is only wired for {'/'.join(sorted(OBO_SUPPORTED))} today. "
            f"Call {cloud} with as_identity='agent', or add an on-behalf-of capable "
            f"profile for {cloud} in Britive."
        )

    app, env, profile = _split_profile(CLOUDS[cloud]["profile"])
    headers = {"X-On-Behalf-Of": OBO_USER} if as_identity == "user" else {}
    br = _client()

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
            f"Britive checkout of '{CLOUDS[cloud]['profile']}' "
            f"as {'the human ' + OBO_USER if as_identity == 'user' else 'the agent'} "
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
    return f"the human {OBO_USER} (impersonated)" if as_identity == "user" else "the AI agent itself"


# ── Per-cloud object access ─────────────────────────────────────────────────
def _aws_client(creds: dict, service: str = "s3"):
    import boto3

    return boto3.client(
        service,
        aws_access_key_id=creds["accessKeyID"],
        aws_secret_access_key=creds["secretAccessKey"],
        aws_session_token=creds["sessionToken"],
    )


def _gcp_bucket(creds: dict):
    from google.cloud import storage
    from google.oauth2 import service_account

    # GCP checkouts return {sa_email: "<service account key json string>"}.
    raw = next(iter(creds.values())) if isinstance(creds, dict) else creds
    key = json.loads(raw) if isinstance(raw, str) else raw
    sa = service_account.Credentials.from_service_account_info(key)
    client = storage.Client(project=key.get("project_id"), credentials=sa)
    return client.bucket(CLOUDS["gcp"]["bucket"])


def _azure_container(creds: dict):
    from azure.identity import ClientSecretCredential
    from azure.storage.blob import ContainerClient

    # Field names differ by path: pybritive -m json gives TenantId/ClientId/
    # ClientSecret; the SDK returns tenantId/appId/secretText, sometimes as a
    # json string under transactionId. Accept all of it.
    if isinstance(creds, dict) and "transactionId" in creds:
        with contextlib.suppress(Exception):
            creds = json.loads(creds["transactionId"])

    def pick(*names):
        for n in names:
            if n in creds:
                return creds[n]
        raise PipelineError(f"Azure credential missing any of {names}: got {list(creds)}")

    cred = ClientSecretCredential(
        tenant_id=pick("TenantId", "tenantId"),
        client_id=pick("ClientId", "appId", "clientId"),
        client_secret=pick("ClientSecret", "secretText", "clientSecret"),
    )
    account = CLOUDS["azure"]["account"]
    return ContainerClient(
        account_url=f"https://{account}.blob.core.windows.net",
        container_name=CLOUDS["azure"]["container"],
        credential=cred,
    )


def _list_objects(cloud: str, creds: dict, limit: int) -> list[dict]:
    prefix = f"{DAILY_PREFIX}/"
    out: list[dict] = []
    if cloud == "aws":
        s3 = _aws_client(creds)
        pages = s3.get_paginator("list_objects_v2").paginate(
            Bucket=CLOUDS["aws"]["bucket"], Prefix=prefix
        )
        for page in pages:
            for o in page.get("Contents", []):
                out.append({"key": o["Key"], "size": o["Size"], "modified": o["LastModified"]})
    elif cloud == "gcp":
        for b in _gcp_bucket(creds).list_blobs(prefix=prefix):
            out.append({"key": b.name, "size": b.size, "modified": b.updated})
    else:
        for b in _azure_container(creds).list_blobs(name_starts_with=prefix):
            out.append({"key": b.name, "size": b.size, "modified": b.last_modified})
    out.sort(key=lambda r: r["modified"], reverse=True)
    return out[:limit]


def _get_object(cloud: str, creds: dict, key: str) -> bytes:
    if cloud == "aws":
        s3 = _aws_client(creds)
        return s3.get_object(Bucket=CLOUDS["aws"]["bucket"], Key=key)["Body"].read()
    if cloud == "gcp":
        return _gcp_bucket(creds).blob(key).download_as_bytes()
    return _azure_container(creds).download_blob(key).readall()


# ── Analysis ────────────────────────────────────────────────────────────────
def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _resolve_date(batch_date: str) -> str:
    d = (batch_date or "").strip().lower()
    if d in ("", "today", "latest"):
        return _today()
    try:
        _date.fromisoformat(d)
    except ValueError as exc:
        raise PipelineError(f"batch_date must be YYYY-MM-DD (or 'today'), got {batch_date!r}") from exc
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
    pii_cols = [c for c in ("first_name", "last_name", "email", "phone") if c in rows[0]]

    lines = [
        f"**{key}** - {len(rows)} records, sha256 `{digest[:16]}...`",
        "",
        f"- Total MRR: **${mrr:,}**",
        "- By plan: " + ", ".join(f"{p} {n}" for p, n in plans.most_common()),
        "- By region: " + ", ".join(f"{r} {n}" for r, n in regions.most_common()),
        f"- Personal data in this batch: {', '.join(pii_cols)}",
    ]

    top = sorted(rows, key=lambda r: int(r.get("mrr_usd") or 0), reverse=True)[:5]
    lines += ["", "Top 5 by MRR:"]
    for r in top:
        lines.append(
            f"  - {r.get('company')} ({r.get('plan')}) ${int(r.get('mrr_usd') or 0):,} - {r.get('region')}"
        )
    return "\n".join(lines)


def _footer(cloud: str, as_identity: str) -> str:
    return (
        f"\n\n---\n_Read from **{cloud}** as {_identity_label(as_identity)}, using a "
        f"Britive credential that was checked out for this call and checked back in "
        f"before this text was returned. No standing cloud credential exists._"
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
        f"Impersonates:   `{OBO_USER}` when a tool is called with as_identity='user'",
        f"Batch layout:   `{DAILY_PREFIX}/<YYYY-MM-DD>.csv` (+ `.manifest.json`)",
        "",
        "| Cloud | Britive profile | Target |",
        "|---|---|---|",
        f"| aws | `{CLOUDS['aws']['profile']}` | `s3://{CLOUDS['aws']['bucket']}` |",
        f"| gcp | `{CLOUDS['gcp']['profile']}` | `gs://{CLOUDS['gcp']['bucket']}` |",
        f"| azure | `{CLOUDS['azure']['profile']}` | "
        f"`{CLOUDS['azure']['account']}/{CLOUDS['azure']['container']}` |",
        "",
        "**Three identities touch one file:**",
        "1. GitHub Actions (federated service identity) WRITES it nightly - unattended, no static key.",
        "2. `as_identity='agent'` READS it as the bot - the bot's own grant bounds what it sees.",
        f"3. `as_identity='user'` READS it as {OBO_USER} - the agent sees it *because the human can*, "
        "and the cloud audit log names the human.",
        "",
        f"On-behalf-of is wired for: **{', '.join(sorted(OBO_SUPPORTED))}**.",
        "",
        "This server stores no cloud credential. Each tool call checks a profile "
        "out, does one thing, and checks it back in.",
    ]
    if not TOKEN:
        lines += ["", "WARNING: BRITIVE_API_TOKEN is not set - every checkout will fail."]
    return "\n".join(lines)


@mcp.tool()
def whoami(as_identity: str = "user", cloud: str = "aws") -> str:
    """Show the actual cloud identity a read would run as - the impersonation proof.

    Call it twice, once with as_identity="agent" and once with "user", to see
    the same Britive service identity produce two different cloud sessions:
    the bot, and the human it is acting for.
    """
    try:
        with jit_credentials(cloud, as_identity) as creds:
            if cloud != "aws":
                return f"`whoami` is AWS-only right now (asked for {cloud})."
            ident = _aws_client(creds, "sts").get_caller_identity()
        return (
            f"**as_identity='{as_identity}'** -> {_identity_label(as_identity)}\n\n"
            f"- Account: `{ident['Account']}`\n"
            f"- ARN: `{ident['Arn']}`\n\n"
            + (
                "_The session name is the HUMAN. Britive issued this credential to the "
                "AI service identity acting on behalf of that person, so every AWS API "
                "call here is attributed to them._"
                if as_identity == "user"
                else "_The session name is the BOT's own service identity - the AI acting "
                "as itself, bounded by its own grant._"
            )
        )
    except PipelineError as exc:
        return f"Error: {exc}"


@mcp.tool()
def list_batches(cloud: str = "aws", limit: int = 10, as_identity: str = "user") -> str:
    """List the most recent nightly batches in a cloud - proof the pipeline runs.

    Each entry was written by a GitHub Actions run using a just-in-time
    credential that no longer exists.
    """
    try:
        with jit_credentials(cloud, as_identity) as creds:
            objs = _list_objects(cloud, creds, max(1, min(limit, 100)))
    except PipelineError as exc:
        return f"Error: {exc}"
    except Exception as exc:  # noqa: BLE001
        return f"Could not list {cloud}: {exc}"

    if not objs:
        return f"No batches found under `{DAILY_PREFIX}/` in {cloud}. Has the nightly workflow run yet?"

    lines = [f"**Recent batches in {cloud}** (newest first)", "", "| Modified (UTC) | Size | Object |", "|---|---|---|"]
    for o in objs:
        ts = o["modified"].strftime("%Y-%m-%d %H:%M") if hasattr(o["modified"], "strftime") else o["modified"]
        lines.append(f"| {ts} | {o['size']} | `{o['key']}` |")
    return "\n".join(lines) + _footer(cloud, as_identity)


@mcp.tool()
def read_batch(batch_date: str = "today", cloud: str = "aws", as_identity: str = "user") -> str:
    """Read and analyze a nightly intake batch: volume, MRR, plan mix, and PII findings.

    batch_date is YYYY-MM-DD, or "today" for the most recent nightly drop.
    Defaults to reading AS THE HUMAN (as_identity="user") - the agent gets to
    this data through the user's access, not its own.
    """
    try:
        d = _resolve_date(batch_date)
        key = f"{DAILY_PREFIX}/{d}.csv"
        with jit_credentials(cloud, as_identity) as creds:
            raw = _get_object(cloud, creds, key)
        return _analyze(raw, key) + _footer(cloud, as_identity)
    except PipelineError as exc:
        return f"Error: {exc}"
    except Exception as exc:  # noqa: BLE001
        return (
            f"Could not read `{DAILY_PREFIX}/{batch_date}.csv` from {cloud}: {exc}\n\n"
            "If this is a 'no such key' error, the nightly workflow may not have run "
            "for that date yet - try `list_batches` to see what exists."
        )


@mcp.tool()
def read_manifest(batch_date: str = "today", cloud: str = "aws", as_identity: str = "user") -> str:
    """Read the sidecar manifest: which identity wrote this batch, from which run.

    The csv is byte-identical in all three clouds; the manifest is the only
    per-cloud part, and it records the JIT identity that did the write.
    """
    try:
        d = _resolve_date(batch_date)
        key = f"{DAILY_PREFIX}/{d}.manifest.json"
        with jit_credentials(cloud, as_identity) as creds:
            raw = _get_object(cloud, creds, key)
        m = json.loads(raw.decode("utf-8"))
        lines = [f"**Manifest for {d} ({cloud})**", ""]
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
        return "\n".join(lines) + _footer(cloud, as_identity)
    except PipelineError as exc:
        return f"Error: {exc}"
    except Exception as exc:  # noqa: BLE001
        return f"Could not read the manifest for {batch_date} from {cloud}: {exc}"


@mcp.tool()
def compare_clouds(batch_date: str = "today") -> str:
    """Verify the same batch landed intact in all three clouds.

    The csv is generated deterministically from the batch date, so all three
    copies must be byte-identical even though three independent just-in-time
    identities wrote them. Runs as the agent (each cloud is checked out and
    checked back in separately - three audit entries).
    """
    try:
        d = _resolve_date(batch_date)
    except PipelineError as exc:
        return f"Error: {exc}"

    key = f"{DAILY_PREFIX}/{d}.csv"
    results: dict[str, str] = {}
    for cloud in CLOUDS:
        try:
            with jit_credentials(cloud, "agent") as creds:
                results[cloud] = hashlib.sha256(_get_object(cloud, creds, key)).hexdigest()
        except Exception as exc:  # noqa: BLE001
            results[cloud] = f"ERROR: {exc}"

    lines = [f"**Cross-cloud integrity check for `{key}`**", "", "| Cloud | sha256 |", "|---|---|"]
    for cloud, digest in results.items():
        shown = digest if digest.startswith("ERROR") else f"`{digest[:32]}...`"
        lines.append(f"| {cloud} | {shown} |")

    good = {v for v in results.values() if not v.startswith("ERROR")}
    lines.append("")
    if len(good) == 1 and len(results) == len(good):
        lines.append(
            "**All three match.** Three separate just-in-time identities, in three "
            "different clouds, wrote byte-identical content - and none of them still exists."
        )
    elif len(good) == 1:
        lines.append(
            f"The clouds that responded agree, but {len(results) - len(good)} could not be read "
            "(see the errors above)."
        )
    elif good:
        lines.append("**Mismatch.** The copies differ - investigate before using this batch.")
    else:
        lines.append("No cloud could be read - check credentials and whether the batch exists.")
    return "\n".join(lines)


if __name__ == "__main__":
    mcp.run()
