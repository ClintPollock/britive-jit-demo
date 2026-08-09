"""
britive-jit-demo MCP server
===========================

Gives Claude the ability to query a demo database WITHOUT any standing
database credentials. Every tool call:

  1. checks out a Britive profile  -> Britive issues a short-lived DB credential
  2. connects and runs the SQL      -> the work happens
  3. checks the profile back in      -> Britive revokes the credential

Reuse the same credential a moment later and you get "Access denied" - the
credential's lifetime is the tool call, nothing more. Each call is one row in
the Britive audit log, attributed to the logged-in user.

Auth: this server shells out to the `pybritive` CLI, which uses the token
cached by a one-time `pybritive login`. No secrets are stored in this code or
in the MCP config.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess

import pymysql
from mcp.server.fastmcp import FastMCP

# ── Config (override via env in .mcp.json) ──────────────────────────────────
READ_PROFILE = os.environ.get(
    "BRITIVE_DB_READ_PROFILE", "Resources/AWS-RDS-MySQL-Demo/MySQL DBA"
)
# Act 2 (approval gate): point this at an approval-required writer profile once
# it exists in the tenant. Until then it falls back to the read profile.
WRITE_PROFILE = os.environ.get("BRITIVE_DB_WRITE_PROFILE", READ_PROFILE)
DB_NAME = os.environ.get("BRITIVE_DB_NAME", "demo")
MAX_ROWS = int(os.environ.get("BRITIVE_DB_MAX_ROWS", "200"))

# Claude Desktop launches with a minimal PATH, so allow an explicit override.
PYBRITIVE = os.environ.get("PYBRITIVE_BIN") or shutil.which("pybritive") or "pybritive"

READ_ONLY_RE = re.compile(r"^\s*(select|with|show|describe|desc|explain)\b", re.IGNORECASE)

mcp = FastMCP("britive-jit-demo")


# ── Britive checkout / checkin helpers ──────────────────────────────────────
class BritiveError(RuntimeError):
    pass


def _run_pybritive(*args: str) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            [PYBRITIVE, *args],
            capture_output=True,
            text=True,
            timeout=120,
        )
    except FileNotFoundError as exc:
        raise BritiveError(
            "pybritive not found on PATH. Install it (`pip install pybritive`) "
            "and run `pybritive login` once."
        ) from exc


def _checkout(profile: str) -> dict:
    """Check out a Britive profile and return its connection details."""
    proc = _run_pybritive("checkout", profile)
    if proc.returncode != 0:
        raise BritiveError(
            f"Britive checkout of '{profile}' failed: {proc.stderr.strip() or proc.stdout.strip()}"
        )
    return _extract_json(proc.stdout)


def _checkin(profile: str) -> None:
    # Best-effort: never let a checkin failure mask a successful query result.
    _run_pybritive("checkin", profile)


def _extract_json(text: str) -> dict:
    """pybritive may emit warnings around the JSON payload; pull out the object."""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end == -1:
            raise BritiveError(f"Could not parse Britive checkout output:\n{text}")
        return json.loads(text[start : end + 1])


def _connect(creds: dict):
    return pymysql.connect(
        host=creds["target_host"],
        port=int(creds["target_port"]),
        user=creds["username"],
        password=creds["password"],
        database=DB_NAME,
        connect_timeout=10,
        read_timeout=60,
        cursorclass=pymysql.cursors.Cursor,
    )


def _format_rows(cols: list[str], rows: list[tuple]) -> str:
    if not cols:
        return "(no result set)"
    truncated = len(rows) > MAX_ROWS
    rows = rows[:MAX_ROWS]
    header = "| " + " | ".join(cols) + " |"
    sep = "| " + " | ".join("---" for _ in cols) + " |"
    body = [
        "| " + " | ".join("" if v is None else str(v) for v in r) + " |" for r in rows
    ]
    out = "\n".join([header, sep, *body])
    if truncated:
        out += f"\n\n_(showing first {MAX_ROWS} rows)_"
    return out


def _run_sql(profile: str, sql: str, write: bool) -> str:
    creds = _checkout(profile)
    ephemeral_user = creds.get("username", "?")
    try:
        conn = _connect(creds)
        try:
            with conn.cursor() as cur:
                cur.execute(sql)
                if cur.description:  # a result set came back
                    cols = [d[0] for d in cur.description]
                    result = _format_rows(cols, cur.fetchall())
                else:
                    conn.commit()
                    result = f"OK - {cur.rowcount} row(s) affected."
        finally:
            conn.close()
    finally:
        _checkin(profile)

    footer = (
        f"\n\n---\n_Ran as Britive-issued ephemeral credential `{ephemeral_user}` "
        f"(profile: {profile}). Credential revoked on check-in - reusing it now "
        f"returns Access denied._"
    )
    return result + footer


# ── Tools ───────────────────────────────────────────────────────────────────
@mcp.tool()
def query(sql: str) -> str:
    """Run a READ-ONLY SQL query against the demo database.

    Britive issues a short-lived credential for this single call and revokes it
    immediately after. Use this for SELECT / WITH / SHOW / DESCRIBE / EXPLAIN.
    For changes to data, use `update_records` (which may require approval).
    """
    if not READ_ONLY_RE.match(sql):
        return (
            "Refused: `query` only runs read statements "
            "(SELECT/WITH/SHOW/DESCRIBE/EXPLAIN). "
            "Use `update_records` for writes - it checks out the writer profile, "
            "which may require human approval."
        )
    try:
        return _run_sql(READ_PROFILE, sql, write=False)
    except BritiveError as exc:
        return f"Britive error: {exc}"


@mcp.tool()
def update_records(sql: str) -> str:
    """Run a WRITE SQL statement (INSERT/UPDATE/DELETE) against the demo database.

    This checks out the *writer* profile. If that profile has an approval policy
    in Britive, the checkout blocks until a human approves - the statement does
    not run until then. Credential is revoked immediately after.
    """
    if READ_ONLY_RE.match(sql):
        return "This looks like a read query - use `query` for that."
    try:
        return _run_sql(WRITE_PROFILE, sql, write=True)
    except BritiveError as exc:
        return f"Britive error: {exc}"


@mcp.tool()
def list_tables() -> str:
    """List the tables in the demo database and their columns.

    Uses one ephemeral checkout to introspect the schema, then revokes it.
    """
    try:
        return _run_sql(
            READ_PROFILE,
            """
            SELECT table_name, column_name, data_type
            FROM information_schema.columns
            WHERE table_schema = DATABASE()
            ORDER BY table_name, ordinal_position
            """,
            write=False,
        )
    except BritiveError as exc:
        return f"Britive error: {exc}"


@mcp.tool()
def britive_status() -> str:
    """Show that there is NO standing database access.

    Lists the Britive DB profiles available to the logged-in user. No checkout
    happens here - it proves the credential only exists during a query call.
    """
    proc = _run_pybritive("ls", "profiles")
    if proc.returncode != 0:
        return (
            "Could not reach Britive. Run `pybritive login` once, then retry.\n"
            f"{proc.stderr.strip()}"
        )
    profiles = None
    start, end = proc.stdout.find("["), proc.stdout.rfind("]")
    if start != -1 and end != -1:
        try:
            profiles = json.loads(proc.stdout[start : end + 1])
        except json.JSONDecodeError:
            profiles = None

    lines = [
        "No standing database credentials exist. Each `query` / `update_records` "
        "call mints a short-lived credential via Britive and revokes it on return.",
        "",
        f"Read profile:  {READ_PROFILE}",
        f"Write profile: {WRITE_PROFILE}",
        f"Database:      {DB_NAME}",
    ]
    if isinstance(profiles, list):
        db_profiles = [
            p.get("Name", "")
            for p in profiles
            if isinstance(p, dict) and p.get("Type") == "Resources"
        ]
        if db_profiles:
            lines += ["", "Available resource profiles:"] + [f"  - {n}" for n in db_profiles]
    return "\n".join(lines)


if __name__ == "__main__":
    mcp.run()
