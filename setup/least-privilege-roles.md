# Replacing the over-privileged pipeline roles

> **AWS STATUS — applied 2026-08-09, account 696226360299**
>
> | Done | Detail |
> |---|---|
> | ✅ `britive-jit-demo-rw` created | `arn:aws:iam::696226360299:role/britive-jit-demo-rw`, trust policy cloned from `cpollock-britive-s3-readonly-role` (SAML provider `cpollock-britive-idp`, incl. `sts:SetSourceIdentity` + `sts:TagSession`) |
> | ✅ Inline policy `jit-demo-bucket-rw` | `s3:ListBucket` on the bucket, `s3:GetObject`+`s3:PutObject` on `/*`. No delete, no control plane. |
> | ✅ Inline policy `jit-demo-bucket-ro` | added to `cpollock-britive-s3-readonly-role` — `ListBucket` + `GetObject` only |
>
> **Read side — verified working.** MCP server (`whoami` in both identity modes,
> `list_batches`, `read_batch`, `read_manifest`) and both PowerShell demos all run green
> against the live batch.
>
> **Write side — Britive profile built and verified 2026-08-09:**
>
> | Done | Detail |
> |---|---|
> | ✅ AWS app rescanned | task `o9otra3173whm0fsgbxeor50`, discovered `britive-jit-demo-rw` as Registered |
> | ✅ Profile `AWS-JIT-Demo-Writer` | papId `1gp1xow8llz0and0i9nm`, scope Environment `0299-CP`, permission `britive-jit-demo-rw` |
> | ✅ Policies | `jit-demo-writer-github` (tag `github-jit-demo`) + `jit-demo-writer-pov-users` (so it can be checked out by hand) |
> | ✅ Verified by checkout | identity `assumed-role/britive-jit-demo-rw/…`; write OK, read OK; **denied**: other bucket, list-all-buckets, create-bucket, delete-object |
> | ✅ Workflow repointed | `AWS_PROFILE_PATH` → `AWS Standalone/0299-CP/AWS-JIT-Demo-Writer`, plus a 6× checkout retry on the AWS job |
>
> Note the trust policy carries `sts:SetSourceIdentity` — that is what makes the
> impersonation demo's CloudTrail `sourceIdentity` work, and the new role inherits it.


The nightly workflow currently writes with grants far broader than it needs. In a demo
whose entire thesis is least privilege, that is the first thing a sharp prospect pokes at.

| Cloud | Profile today | Grant today | Target |
|---|---|---|---|
| AWS | `CP-Admin-Profile` | **account admin** | read+write on one bucket |
| GCP | `cpollock-pov-gcs-admin` | `roles/storage.admin` at **folder** scope | object read+write on one bucket |
| Azure | `cpollock-pov-azure-blob-writer` | Blob Data Contributor at **subscription** scope | same role at **storage account** scope |

The reader side (`AWS-S3-Reader` → `cpollock-britive-s3-readonly-role`) is already
correctly scoped — it just needs the new bucket added, see step A3.

What the workflow actually needs, verified against the job steps:

- `s3:ListBucket` — the "list recent batches" proof step
- `s3:PutObject` — the batch, manifest and run-marker writes
- `s3:GetObject` — Azure reads its blob back; keep it for parity and for `aws s3 cp`
- **No delete, no bucket creation** — the self-create steps were removed on 2026-08-09,
  so the write path never touches the control plane

---

## A. AWS

### A1. Create the role

Britive's AWS integration assumes roles via SAML. **Do not hand-write the trust policy** —
copy it from a role that already works with this tenant, so the SAML provider ARN and
conditions are guaranteed to match:

```bash
aws iam get-role --role-name cpollock-britive-s3-readonly-role \
  --query 'Role.AssumeRolePolicyDocument' > /tmp/britive-trust.json

aws iam create-role --role-name britive-jit-demo-rw \
  --assume-role-policy-document file:///tmp/britive-trust.json \
  --description "Britive JIT demo pipeline - read+write on the demo bucket only"
```

### A2. Attach the scoped permission policy

```bash
aws iam put-role-policy --role-name britive-jit-demo-rw \
  --policy-name jit-demo-bucket-rw \
  --policy-document file://aws-jit-demo-rw-policy.json
```

`aws-jit-demo-rw-policy.json` sits next to this file. Note the two statements use
**different resource ARNs** — `s3:ListBucket` is a bucket-level action (no `/*`) while
the object actions need the `/*`. Getting that wrong is the usual cause of a listing
that returns AccessDenied while writes succeed.

### A3. Add the demo bucket to the READER role

This is what unblocks the MCP server and both PowerShell demos. The readonly role is
currently scoped to `customer-data-intake-demo` only:

```bash
aws iam put-role-policy --role-name cpollock-britive-s3-readonly-role \
  --policy-name jit-demo-bucket-ro \
  --policy-document '{
    "Version": "2012-10-17",
    "Statement": [
      {"Effect":"Allow","Action":["s3:ListBucket"],
       "Resource":"arn:aws:s3:::britive-jit-demo-696226360299"},
      {"Effect":"Allow","Action":["s3:GetObject"],
       "Resource":"arn:aws:s3:::britive-jit-demo-696226360299/*"}
    ]}'
```

**Do not add PutObject here.** The agent being unable to write is a demo beat, not an
oversight — it is what proves the guardrail is policy rather than missing code.

### A4. Britive + workflow

1. Scan the AWS application so Britive discovers `britive-jit-demo-rw`.
2. Create a profile (suggested: `AWS-JIT-Demo-Writer`) granting that role.
3. **Tag it `github-jit-demo`** (id `kpmmuy6qiku1xbcy74ad`). This is how the federated
   GitHub identity gets access — `POV-Users` alone is not enough and the job will fail.
4. Update `AWS_PROFILE_PATH` in `.github/workflows/jit-demo.yml`.

Expect `TransactionNotFound` on the first checkout of a newly created profile for ~1–2
minutes. That is eventual consistency, not permissions — the workflow already retries 6×.

---

## B. Azure — NOT POSSIBLE as originally planned

**Corrected 2026-08-09.** An earlier version of this document said Azure was "the easy
one — just repoint the profile's scope at the storage account." That is wrong. Verified
against the live tenant:

```
profile scope:  [{"type": "Environment", "value": "4bbb4f1c-8734-4a04-a295-487d52fa4e7f"}]
constraints:    400 - E1003 - App does not support profile permission constraints
                (appContainerId=29)
```

For Azure, **Britive's "environment" IS the subscription**, and the role assignment is
made at environment scope. There is no per-permission constraint mechanism on this app to
narrow it to a storage account or container. (The AWS app returns the same
`E1003` — constraints are not enabled there either. AWS was fixable only because the
scope lives in the *IAM policy*, which is a different lever.)

Realistic options:

1. **Accept it, and say so precisely on stage.** `Azure subscription 1` contains almost
   nothing — `NetworkWatcherRG` plus the demo storage account — so Blob Data Contributor
   at subscription scope has a small *actual* blast radius, and the denied
   `az group create` still proves it cannot touch the control plane. **Recommended.**
2. **A custom Azure role** with `dataActions` limited to blob read/write, still assigned
   at subscription scope. Narrows *what*, not *where*. Watch out: setting the custom
   role's `assignableScopes` to just the storage account makes Britive's
   subscription-scope assignment **fail**.

Do not claim "scoped to the storage account" for Azure. The honest line is: *"on Azure
the JIT grant is bounded to blob data operations; the scope is the subscription because
that is the boundary this integration assigns at."*

A reader profile for the agent would use **Storage Blob Data Reader**, subject to the
same scope limitation.

---

## C. GCP — verify before promising

Britive currently attaches `roles/storage.admin` at **folder** scope. The target is
object read+write on one bucket:

- `roles/storage.objectUser` (create/read/delete objects), or
- `roles/storage.objectCreator` + `roles/storage.objectViewer` for a tighter split

**Open question that needs testing against the tenant:** GCS supports bucket-level IAM
bindings, but whether *Britive's GCP profile model* can target a single bucket as a scope
— rather than project / folder / org — is unconfirmed. If it cannot, the realistic
fallback is `roles/storage.objectUser` at **project** scope on `cpollock-poc-core`, which
is still a large improvement over `storage.admin` at folder scope: no bucket
creation/deletion, no IAM policy changes, and blast radius limited to one project.

Do not claim bucket-scoped GCP on stage until this is verified.

A reader profile for the agent needs `roles/storage.objectViewer` at whatever scope
turns out to be supported.

---

## Why this is worth doing

It converts a weak spot into a demo beat. Once the pipeline role is scoped:

- The pipeline can write the batch and **cannot** read the sensitive bucket.
- The agent can read the batch and **cannot** write it.
- Neither can touch the control plane.

Three identities, three different blast radii, on one object — and you can show each
denial live rather than asserting it.
