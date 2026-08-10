# Britive JIT Demo — Claude → Britive → Database (zero standing access)

Ask Claude a business question. Claude has **no database credentials**. To answer,
it calls a Britive MCP tool, which checks out a Britive profile — Britive issues a
**short-lived database credential**, the query runs, and Britive **revokes the
credential** the moment the tool call returns.

Reuse that credential a second later and you get `Access denied`. The credential's
lifetime is the tool call, nothing more. Every call is one row in the Britive audit
log, attributed to the logged-in human.

```
You ──ask──▶ Claude ──MCP tool──▶ Britive checkout ──▶ ephemeral DB cred
                 │                                            │
                 │                                       query runs
                 ▼                                            │
            answer ◀────────────── results ◀──────────────────┘
                                   Britive checkin ──▶ credential revoked
```

This is the same JIT primitive Britive uses for cloud workloads — here pointed at a
database, with Claude as the consumer.

---

## What this proves

- **No secrets in Claude's config.** The MCP server stores nothing; it shells out to
  `pybritive`, which uses a token from a one-time `pybritive login`.
- **Credential lifetime = task lifetime.** Issued on checkout, revoked on checkin.
- **Every action is attributable.** The Britive audit log shows the logged-in user
  checking the profile out and back in, once per question.
- **The agent can't bypass policy.** If the writer profile requires approval, Claude
  waits — there's no workaround, because Britive is the credential issuer.

---

## The demo — three acts

**Act 1 · Read (no approval).** Claude answers a question over the data.

> *"Who are our top 3 customers by spend, are we low on stock for anything, and did
> Marco Bellini's recent orders actually ship?"*

Claude calls `query` (one checkout + checkin per call), returns top spenders, low-stock
SKUs, and Marco's four orders — one cancelled, one placed, one in-transit (UPS), one
delivered (FedEx). Open the Britive audit log: fresh checkout/checkin rows under your
name, credentials already revoked.

**Act 2 · Write (approval gate).** Claude tries something sensitive.

> *"Give everyone in Engineering a 5% raise."*

Claude calls `update_records`, which checks out the **writer** profile. If that profile
has an approval policy, the checkout blocks — a human approves in Slack/Teams/email —
then the statement runs and the credential is revoked. No approval, no access.

**Act 3 · Governance.** The same Claude interface audits itself.

> *"What database access have I used in the last hour?"*

`britive_status` shows there is **no standing credential** — access only existed during
each query — and lists the resource profiles available to you.

---

## Setup

### Prerequisites
- [`uv`](https://docs.astral.sh/uv/) (Python package runner)
- `pip install pybritive` and run **`pybritive login`** once (browser SSO; the token is
  cached, so the rest of the demo is silent)
- Network access to the RDS endpoint (see *Repeatability* below)

### 1. Install the MCP server
```powershell
git clone https://github.com/ClintPollock/britive-jit-demo
cd britive-jit-demo/mcp-server
uv sync
```

### 2. Register it with Claude Desktop
Edit `%APPDATA%\Claude\claude_desktop_config.json` (Windows) or
`~/Library/Application Support/Claude/claude_desktop_config.json` (macOS):

```json
{
  "mcpServers": {
    "britive-jit-demo": {
      "command": "C:\\Users\\CP\\.local\\bin\\uv.exe",
      "args": [
        "run",
        "--directory",
        "C:\\path\\to\\britive-jit-demo\\mcp-server",
        "server.py"
      ],
      "env": {
        "BRITIVE_DB_READ_PROFILE": "Resources/AWS-RDS-MySQL-Demo/MySQL DBA",
        "BRITIVE_DB_WRITE_PROFILE": "Resources/AWS-RDS-MySQL-Demo/MySQL DBA",
        "BRITIVE_DB_NAME": "demo"
      }
    }
  }
}
```
> Use the **full path** to `uv.exe` — Claude Desktop launches with a minimal PATH and
> often can't find a bare `uv`. Restart Claude Desktop after editing. Then run
> `pybritive login` once in a terminal before the demo.

### 3. (Demo day) authenticate once
```powershell
pybritive login
```
Browser pops once; cached after. Claude Desktop never shows a browser during the demo.

---

## MCP tools

| Tool | What it does |
|---|---|
| `query(sql)` | Read-only (SELECT/WITH/SHOW/DESCRIBE/EXPLAIN). Ephemeral checkout → run → checkin. |
| `update_records(sql)` | Writes (INSERT/UPDATE/DELETE) via the **writer** profile — the approval-gated path. |
| `list_tables()` | Schema overview from one ephemeral checkout. |
| `britive_status()` | Proves no standing access; lists available resource profiles. No checkout. |

Config via the `env` block: `BRITIVE_DB_READ_PROFILE`, `BRITIVE_DB_WRITE_PROFILE`,
`BRITIVE_DB_NAME`, `BRITIVE_DB_MAX_ROWS` (default 200).

---

## The data

Single MySQL database `demo` on RDS, seeded by [`seed/rds-mysql-seed.sql`](seed/rds-mysql-seed.sql):

| Table | Rows | Notes |
|---|---|---|
| `customers` | 40 | name, email, region, signup_date |
| `products` | 25 | price + `qty_on_hand` (some low-stock) |
| `orders` | 180 | references customers + products; mixed statuses |
| `shipments` | 129 | carrier, tracking, dates |
| `employees` | 20 | **`salary` is the sensitive column → drives Act 2** |

**Reseeding** (idempotent) — dogfoods Britive even for setup; no master password used:
```powershell
pybritive checkout "Resources/AWS-RDS-MySQL-Demo/MySQL DBA"   # prints host/user/password
$env:MYSQL_PWD='<password-from-checkout>'
Get-Content seed\rds-mysql-seed.sql -Raw | mysql -h <host-from-checkout> -P 3306 -u <user> demo
pybritive checkin "Resources/AWS-RDS-MySQL-Demo/MySQL DBA"
```

---

## What you'll see during the demo

| Surface | Change |
|---|---|
| **Claude Desktop** | Answers the question; tool calls visible, no credentials in sight |
| **Britive Audit Log** | Checkout + checkin events per tool call, attributed to your identity |
| **The proof** | After a checkin, reconnecting with that credential returns `Access denied` — the credential is revoked (this RDS resource rotates the managed user's password, so it's "credential revoked", not "user dropped") |

---

## Britive tenant setup (for Act 2)

The RDS resource currently exposes a single **DBA** profile. For the Act 2 approval gate,
add two profiles on the `AWS-RDS-MySQL-Demo` resource:

1. **MySQL Reader** — `SELECT`-only, no approval. Point `BRITIVE_DB_READ_PROFILE` at it.
2. **MySQL Writer** — `INSERT/UPDATE/DELETE`, **approval required** (Slack/Teams/email).
   Point `BRITIVE_DB_WRITE_PROFILE` at it.

Until then, both env vars point at the DBA profile and Act 2 runs without the approval
pause.

---

## Repeatability

This uses RDS (not a laptop-local DB) so others can run it. To repeat elsewhere:
- The RDS **security group** must allow the public IP wherever Claude Desktop runs.
- The runner needs the Britive **resource + profiles** in their tenant (or shared access
  to this one) and their own `pybritive login`.
- Single-engine (MySQL) by design for portability. RDS Postgres / SQL Server could be
  added later to restore a multi-engine cross-DB story.

---

## The nightly drop — one file, three identities, no standing keys

This closes the loop between the unattended pipeline and the agent that consumes it.

**Every night at 07:00 UTC**, the GitHub Actions workflow generates a synthetic
customer-intake batch and writes it to all three clouds:

```
daily/<YYYY-MM-DD>.csv             the batch
daily/<YYYY-MM-DD>.manifest.json   who wrote it, from which run
```

**Then a human or an agent reads it back** — through PowerShell, or through Claude via
the pipeline MCP server. Three different identities touch that one object, and **none of
them holds a standing credential**:

| # | Identity | When | How it authenticates | What it can do |
|---|---|---|---|---|
| 1 | GitHub Actions **federated SI** | nightly, unattended | GitHub OIDC → Britive | **write** the batch |
| 2 | The **AI agent, as itself** | on demand | Britive service identity | **read** — accountability lands on the bot |
| 3 | The **AI agent, as you** | on demand | same token + `X-On-Behalf-Of` | **read** — bounded by *your* access, audited to *your* name |

That third row is the one worth pausing on. The agent reaches this data **because the
human can**, not because it holds a key of its own. Revoke the human and the agent loses
it too — no separate offboarding step, no orphaned bot credential.

### Why the batch is deterministic

[`scripts/gen-intake.py`](scripts/gen-intake.py) seeds its RNG from the **batch date and
nothing else**. Consequences, all deliberate:

- All three clouds hold **byte-identical** content, though three independent JIT
  identities wrote them. `compare_clouds` diffs the sha256 — a real integrity check.
- Re-running the workflow the same day is **idempotent** — same bytes, not a second batch.
- The numbers still change **every day** — row count, MRR, plan mix, region spread, top
  accounts. A demo is never canned; the agent's summary is genuinely different each
  morning.

The manifest is the only per-cloud part — it records which JIT identity did the write.

### Reading it from PowerShell

Both scripts live in the parent POV folder:

| Script | Reads as | Shows |
|---|---|---|
| `demo-ai-agent.ps1` | the agent itself | full analysis of today's batch, then the guardrails that stop the agent roaming |
| `demo-impersonation.ps1` | the bot, then **you** | the same read under two identities, back to back |

### Reading it from Claude (pipeline MCP server)

```bash
cd mcp-server-pipeline && uv sync              # add --extra gcp --extra azure for those clouds
```

Register it alongside the database server:

```json
{
  "mcpServers": {
    "britive-jit-pipeline": {
      "command": "uv",
      "args": ["--directory", "/abs/path/to/mcp-server-pipeline", "run", "python", "server.py"],
      "env": {
        "BRITIVE_TENANT": "cpollock-tenant",
        "BRITIVE_API_TOKEN": "<AI service identity token>",
        "BRITIVE_OBO_USER": "clint.pollock@jit-zsp.com"
      }
    }
  }
}
```

`BRITIVE_API_TOKEN` must be a **service identity** token — on-behalf-of impersonation
cannot be done with a plain user token.

| Tool | What it does |
|---|---|
| `pipeline_status` | Config only, no checkout — opens the demo, proves no standing credential |
| `whoami` | The actual cloud identity a read runs as. Call it twice (`agent`, then `user`) for the contrast |
| `list_batches` | Recent nightly batches — visible proof the pipeline runs |
| `read_batch` | Reads + analyzes a batch: volume, MRR, plan mix, **PII findings** |
| `read_manifest` | Which JIT identity wrote it, from which run |
| `compare_clouds` | sha256 across all three clouds — three JIT identities, identical bytes |

Reads default to `as_identity="user"` — the agent acts as you unless told otherwise.
Impersonation is wired for **AWS** today; `gcp` and `azure` refuse `as_identity="user"`
rather than silently reading as the bot.

Try: *"What did the pipeline drop today, and is there any PII in it?"* then
*"Now show me who that read actually ran as."*

### Prerequisite

The `AWS-S3-Reader` profile's IAM role needs `s3:ListBucket` + `s3:GetObject` on
`britive-jit-demo-696226360299`. That role was scoped to `customer-data-intake-demo`
only, so **this grant must be added before the read side works**. Both PowerShell
scripts detect the missing grant and say so rather than failing obscurely.

---

## Appendix — the three-cloud GitHub Actions demo

A separate demo lives in [`.github/workflows/jit-demo.yml`](.github/workflows/jit-demo.yml):
same JIT primitive, pointed at cloud storage instead of a database. GitHub Actions
authenticates to Britive with **OIDC** (no GitHub Secrets), then checks out an AWS, a
GCP, and an Azure profile and writes to each: the nightly intake batch (see
[The nightly drop](#the-nightly-drop--one-file-three-identities-no-standing-keys) above)
plus a per-run marker file. Files accumulate, so each job also lists the last 10 — a
visible history of past runs.

**No job creates a bucket.** All three targets are fixed infrastructure, created once out
of band. That is deliberate: the JIT profiles are data-plane grants, and the Azure job's
denied `az group create` is a proof step — self-creating buckets elsewhere would
undercut it.

| Cloud | Profile | Target |
|---|---|---|
| AWS | `AWS Standalone/0299-CP/CP-Admin-Profile` | `s3://britive-jit-demo-696226360299/jit-demo/` |
| GCP | `Google Cloud/Google Cloud/cpollock-pov-gcs-admin` | `gs://britive-jit-demo-1095450080757/jit-demo/` |
| Azure | `Azure BritiveSE/Azure BritiveSE/cpollock-pov-azure-blob-writer` | container `jit-demo` in `britivejitdemo4bbb4f1c` |

Trigger it from the Actions tab → `britive-jit-demo` → Run workflow, or by pushing to `main`.

### What you'll see during a run

| Surface | Change |
|---|---|
| **GitHub Actions log** | `pybritive checkout` succeeds with no Secret in sight |
| **Britive Audit Log** | OIDC federation auth event for the federated SI; checkout/checkin per profile |
| **AWS S3** | `daily/<date>.csv` + `jit-demo/aws-run-<id>.txt` in `<S3_BUCKET>` |
| **GCS** | `daily/<date>.csv` + `jit-demo/gcp-run-<id>.txt` in `<GCS_BUCKET>` |
| **Azure Blob** | `daily/<date>.csv` + `azure-run-<id>.txt` in `<AZURE_STORAGE_ACCOUNT>` |
| **AWS CloudTrail** | STS AssumeRole-with-SAML event tied to the federated identity |
| **Azure** | A service principal client id that is different on every run |

### Reusing this against your own Britive tenant

**1. Create an OIDC Workload Identity Provider**
`Identity Management → Identity Providers → Add OIDC Workload IdP`
- Issuer: `https://token.actions.githubusercontent.com`
- Allowed Audiences: a tag of your choosing (e.g. `britive-acme`) — must match `BRITIVE_FED_PROVIDER`
- Attribute map: `sub` → a custom Britive identity attribute (e.g. `GitHub Subject`, type String).
  **A custom attribute is required; the built-in `Username` does not work for federated SI mapping.**

**2. Create a Federated Service Identity**
`Identity Management → Service Identities → Add`
- Bind it to the OIDC IdP above
- External ID / federated subject: `repo:<github-org>/<github-repo>:ref:refs/heads/main`
- Token duration: 600s is plenty for a workflow run

**3. Add the SI to a tag** whose policies grant checkout on the profiles you want. In this
tenant that tag is `github-jit-demo` — see the Azure note below, it is easy to miss.

Then swap the workflow `env:` block:

| Variable | What it is |
|---|---|
| `BRITIVE_TENANT` | your tenant slug (the `<slug>` in `https://<slug>.britive-app.com`) |
| `BRITIVE_FED_PROVIDER` | `github-<your-allowed-audience>` |
| `AWS_PROFILE_PATH` / `GCP_PROFILE` / `AZURE_PROFILE_PATH` | run `pybritive ls profiles` and copy the `Name` field |
| `S3_BUCKET` / `GCS_BUCKET` | AWS and GCP demo buckets — **must already exist**, nothing self-creates |
| `AZURE_STORAGE_ACCOUNT` / `AZURE_CONTAINER` | Azure target (must already exist — see below) |
| `RETAIN` | how many batch dates / run markers each cloud keeps (default 10) |

All three targets are fixed infrastructure on purpose: the JIT profiles are data-plane
grants, and the Azure job's deliberately-denied `az group create` is a proof step that
self-creating buckets elsewhere would undercut.

### Azure setup (already done — recorded here so it can be rebuilt)

The Azure profile grants **only `Storage Blob Data Contributor`** at subscription scope,
so it can write and read blobs but cannot create the storage account. That account is
fixed infrastructure, created once:

```
resource group    britive-jit-demo-rg        (eastus2)
storage account   britivejitdemo4bbb4f1c     (StorageV2, Standard_LRS, no public blob access)
container         jit-demo
```

Two things worth knowing if you rebuild this:

- **The profile needs the `github-jit-demo` tag**, not just `POV-Users`. That tag is how
  the federated GitHub identity gets access — a profile granted only to `POV-Users` works
  for you interactively and fails in Actions.
- **A newly created Britive profile returns `TransactionNotFound` on checkout for the
  first couple of minutes.** It is a propagation delay, not a permissions problem. Wait
  and retry.

### Azure timing

Britive mints a **new app registration per checkout**, so the service principal client id
differs on every run — nothing is long-lived. Two lags follow from that, both handled with
retry loops rather than fixed sleeps:

- **On checkout**, ARM takes ~30–45s to honour the new role assignment; until then
  `az login` fails with `No subscriptions found`.
- **On checkin**, the role assignment is removed ~30–45s later. The workflow proves this
  by re-running `az login` with the same credential until it loses access.

**Two caveats — don't overclaim the Azure revoke on stage.** Both were verified by hand:

1. The secret may still authenticate to Entra. What disappears is the **role assignment**.
   The accurate line is *"the grant is revoked"*, not *"the identity is deleted"*.
2. **Azure does not invalidate access tokens that were already issued.** A token minted
   before checkin keeps working until it expires (~60–90 min), so a blob write with a
   cached token still succeeds after checkin. The workflow wipes `~/.azure` before the
   proof step for exactly this reason — what it demonstrates is that a *fresh*
   authentication gets nothing.

This is weaker than the database story in the main demo, where the credential is dropped
server-side and dies instantly. Azure JIT bounds how long access *can be obtained*, not
how long an already-issued token lives. Worth saying plainly rather than being asked.

> The GCP job has a known issue (see the comment in the workflow): Britive's federated-SI
> checkout for GCP-WIF profiles does not attach the requested role to the shadow SA, so
> writes can 403.
