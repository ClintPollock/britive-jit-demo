# Claude Desktop demo — three prompts

**Before you start:** fully quit Claude Desktop (including the system tray icon) and
reopen, so it picks up the current config.

---

### 1. *"What did the pipeline drop today, and is there any PII in it?"*

Claude reads this morning's batch and reports volume, MRR, and **records with an SSN
sitting in a free-text `notes` field**. The numbers and the PII count change every
weekday, so it is visibly live.

> That file was written overnight by a GitHub Actions job — no human, no static key.
> Claude just read it with a credential that was created for this question and destroyed
> before it answered.

---

### 2. *"Who did that read actually run as?"*

```
…assumed-role/cpollock-britive-s3-readonly-role/clint.pollock@jit-zsp.com
```

---

### 3. *"Now show me the same thing, as the agent itself."*

```
…assumed-role/cpollock-britive-s3-readonly-role/<id>@iam.serviceaccount.com
```

> Same AI, same token, same role — two different identities in the cloud audit log. By
> default this agent acts **as me**. It can see this data because **I** can, not because
> it holds a key of its own. Revoke me and the AI loses it in the same instant; there is
> no separate bot credential to hunt down at offboarding.

**This is the beat that lands. Slow down here.**

---

### Close

> Three identities touched one file today: the pipeline that wrote it, the agent that
> read it, and me — through the agent. Not one of them holds a standing credential, and
> every one of those actions is its own row in the audit log.

---

<details>
<summary>If they dig (don't volunteer these)</summary>

- **"Which identity wrote the file?"** → ask for the manifest; it names the JIT identity
  and the CI run.
- **"Can it write?"** → Claude has no write tool, but that alone is weak ("you just
  didn't build one"). The real guardrail is that the reader role has no `s3:PutObject`.
  For a live denial, run `demo-ai-agent.ps1` step 6.
- **A database, not a bucket** → the `britive-jit-demo` server does the same thing
  against MySQL, one ephemeral DB credential per question. Currently the bridge-routed
  Docker DB, which only has `customers` (10 rows).
- **Other clouds** → the same batch lands in GCS and Azure Blob, written by two more
  independent JIT identities.

**Do not ask on stage:**
- *"Did the same batch land in all three clouds?"* — GCP and Azure return
  `profile not found`; the AI service identity was never granted those profiles.
- Anything touching the RDS MySQL profile — persistent `TransactionNotFound`.

</details>
