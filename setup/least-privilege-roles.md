# Replacing the over-privileged pipeline roles

> **AWS STATUS — applied 2026-08-09, account 696226360299**
>
> | Done | Detail |
> |---|---|
> | ✅ `britive-jit-demo-rw` created | `arn:aws:iam::696226360299:role/britive-jit-demo-rw`, trust policy cloned from `cpollock-britive-s3-readonly-role` (SAML provider `cpollock-britive-idp`, incl. `sts:SetSourceIdentity` + `sts:TagSession`) |
> | ✅ Inline policy `jit-demo-bucket-rw` | `s3:ListBucket` on the bucket, `s3:GetObject`+`s3:PutObject` on `/*`. No delete, no control plane. |
> | ✅ Inline policy `jit-demo-bucket-ro` | added to `cpollock-britive-s3-readonly-role` — `ListBucket` + `GetObject` only |
>
> **The read side is now unblocked and needs no Britive change** — the grant lives on the
> IAM role that `AWS-S3-Reader` already assumes, so the MCP server and both PowerShell
> demos should work immediately.
>
> **The write side is NOT live yet.** The workflow still runs on `CP-Admin-Profile` until
> steps A4.1–A4.4 below are done in the Britive tenant.
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

## B. Azure — the easy one

The role is already correct (`Storage Blob Data Contributor`); only the **scope** is
wrong. Re-point the profile's assignment from the subscription to the storage account:

```
/subscriptions/4bbb4f1c-8734-4a04-a295-487d52fa4e7f
  /resourceGroups/britive-jit-demo-rg
  /providers/Microsoft.Storage/storageAccounts/britivejitdemo4bbb4f1c
```

Container-level scope (`.../blobServices/default/containers/jit-demo`) is also possible
and tighter still. Storage-account scope is the safer default — it leaves room for a
second container without another Britive change.

Nothing in the workflow changes. The denied `az group create` proof step keeps working;
in fact it gets stronger, because the grant no longer spans the subscription at all.

A reader profile for the agent needs **Storage Blob Data Reader** at the same scope.

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
