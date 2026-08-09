# Claude Desktop demo — what to ask, in order

Every prompt below was run against the live tenant on 2026-08-09. Anything not
verified is called out explicitly rather than left for you to discover on stage.

## Before you start

1. **Fully quit Claude Desktop** — including the system tray icon — and reopen it.
   The config changed today; a running instance is still on the old one.
2. Confirm the tools loaded: the 🔌 / tools icon should list **britive-jit-pipeline**
   (6 tools) and **britive-jit-demo** (4 tools).

If the pipeline server is missing, its log is in `%APPDATA%\Claude\logs\`.

---

## Act 0 — "prove there's nothing standing"

> **Ask:** *"What's the setup for the nightly pipeline? Don't access anything yet."*

Runs `pipeline_status`, which does **no checkout at all**. It prints the three cloud
targets and the three identities that touch the batch.

**Say:** *"Nothing has been accessed yet. There is no cloud credential sitting in this
laptop, in the MCP config, or in the agent. Watch what it takes to get one."*

---

## Act 1 — "what did the robot leave us this morning?"

> **Ask:** *"What did the pipeline drop today, and is there any PII in it?"*

Runs `list_batches` then `read_batch`. Expect volume, total MRR, plan mix, and — the
part that lands — **records with an SSN sitting in a free-text `notes` field**.

**Say:** *"That file was written overnight by a GitHub Actions job. No human, no static
key. Claude just read it with a credential that was created for this question and
destroyed before it answered."*

The numbers change every weekday, and the PII count with them, so this is visibly live
rather than canned. On 2026-08-09 it was 48 records, $36,663 MRR, 4 flagged rows.

> **Follow-up:** *"Which identity actually wrote that file, and from which CI run?"*

Runs `read_manifest` — names the JIT identity and the GitHub run id.

---

## Act 2 — the impersonation beat (**the one to slow down for**)

> **Ask:** *"Who did that read actually run as?"*

Runs `whoami` with the default `as_identity="user"`:

```
…assumed-role/cpollock-britive-s3-readonly-role/clint.pollock@jit-zsp.com
```

> **Then ask:** *"Now show me the same thing, but as the agent itself."*

Runs `whoami` with `as_identity="agent"`:

```
…assumed-role/cpollock-britive-s3-readonly-role/<id>@iam.serviceaccount.com
```

**Say:** *"Same AI, same token, same role — two different identities in CloudTrail. By
default this agent acts **as me**. It can see this data because **I** can, not because
it holds a key of its own. Revoke me, and the AI loses it in the same instant — there is
no separate bot credential to go hunting for at offboarding."*

That contrast is the strongest thing in the whole demo. Don't rush it.

---

## Act 3 — the guardrail

> **Ask:** *"Can you overwrite today's batch with different numbers?"*

**Be precise about what this proves.** Claude will say it has no tool for that — which
is true but weak on its own ("you just didn't build one"). The real guardrail is
underneath: the reader profile's IAM role has **no `s3:PutObject`**, so even a
compromised or prompt-injected agent gets `AccessDenied`.

If a prospect pushes, show it live in the terminal — `demo-ai-agent.ps1` step 6 attempts
the write and gets denied by policy, from the same identity.

*(Wanting this provable inside the MCP demo itself is reasonable — it needs a tool that
deliberately attempts the write and surfaces the denial. Not built yet.)*

---

## Act 4 — a completely different resource, same primitive

> **Ask:** *"What tables are in the demo database, and how many customers are there?"*

Switches to the **britive-jit-demo** server: `list_tables`, then `query`. Each call
checks out a Britive profile, gets an ephemeral DB credential, runs the SQL, and checks
it back in. The footer names the credential that ran it.

**Say:** *"Different resource, same primitive. Object storage a moment ago, a database
now — and in both cases the credential existed only for the length of one question."*

**Caveat:** this currently points at the bridge-routed Docker MySQL, whose `demo`
database has only a `customers` table with 10 rows. The richer schema — orders,
shipments, `employees.salary` for the approval gate — is in `seed/rds-mysql-seed.sql`
and lives on RDS. Seed the Docker DB before demoing anything beyond a simple count.

---

## Do NOT ask yet

- **"Did the same batch land in all three clouds?"** (`compare_clouds`) — AWS answers,
  but GCP and Azure return `profile not found`: the AI service identity was never
  granted those profiles. It will look broken. Grant them first.
- Anything about the RDS MySQL profile — it currently throws `TransactionNotFound`.

---

## The closing line

*"Three identities touched one file today. The pipeline that wrote it, the agent that
read it, and me — through the agent. Not one of them holds a standing credential, and
every one of those actions is a separate row in the audit log."*
