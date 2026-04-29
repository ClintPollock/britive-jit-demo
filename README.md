# Britive JIT Demo — GitHub OIDC → Britive → AWS + GCP

A reusable demo of **JIT for cloud workloads** using Britive's federated Service Identity model. The repo has zero stored credentials. Each workflow run gets short-lived AWS and GCP creds via Britive, brokered by GitHub's built-in OIDC token.

## What this proves

- Repo `Settings → Secrets and variables` is **empty**. No `AWS_*`, no `GCP_*`, no `BRITIVE_*`.
- Each run authenticates via GitHub's per-run OIDC token (`permissions: id-token: write`)
- Britive trusts that token (via OIDC IdP config), maps the `sub` claim to a federated Service Identity
- That SI has Britive policies granting it specific cloud profile checkouts
- Workflow runs `pybritive checkout`, gets short-lived creds, writes a per-run file to a bucket
- On checkin (or run-end), Britive revokes the temporary creds

## Prerequisites in your Britive tenant

To reuse this against your own Britive tenant:

1. **Create an OIDC Workload Identity Provider**
   `Identity Management → Identity Providers → Add OIDC Workload IdP`
   - Issuer: `https://token.actions.githubusercontent.com`
   - Allowed Audiences: a tag of your choosing (e.g. `britive-acme`) — must match `BRITIVE_FED_PROVIDER` in the workflow
   - Attribute map: `sub` → a custom Britive identity attribute (e.g. `GitHub Subject` of type String). **Custom attribute is required; built-in `Username` does not work for federated SI mapping.**

2. **Create a Federated Service Identity**
   `Identity Management → Service Identities → Add`
   - Bind to the OIDC IdP above
   - External ID / federated subject: `repo:<github-org>/<github-repo>:ref:refs/heads/main`
     (lock to a specific branch; or use any other GitHub OIDC sub claim format)
   - Token duration: 600s (10 min) is plenty for a workflow run

3. **Add the SI to a tag** that has policies granting access to the AWS and/or GCP profiles you want to checkout.

## Settings to swap for your tenant

All in the workflow `env:` block at the top of `.github/workflows/jit-demo.yml`:

| Variable | What it is |
|---|---|
| `BRITIVE_TENANT` | your tenant slug (the `<slug>` in `https://<slug>.britive-app.com`) |
| `BRITIVE_FED_PROVIDER` | `github-<your-allowed-audience>` (e.g. `github-britive-acme`) — used by `pybritive --federation-provider` |
| `AWS_PROFILE_PATH` | run `pybritive ls profiles` and copy the `Name` field of an AWS profile your SI can checkout |
| `GCP_PROFILE` | same, for a GCP profile |
| `S3_BUCKET` | your AWS demo bucket (workflow self-creates if missing) |
| `GCS_BUCKET` | your GCP demo bucket (must already exist) |

## Repo setup (one-time)

```bash
gh repo create <YOUR-ORG>/<YOUR-REPO> --public --clone
cd <YOUR-REPO>
mkdir -p .github/workflows
cp /path/to/jit-demo.yml .github/workflows/
git add . && git commit -m "Initial JIT demo" && git push
```

Then trigger via Actions tab → `britive-jit-demo` → Run workflow.

## What you'll see during a run

| Surface | Change |
|---|---|
| **GitHub Actions log** | `pybritive checkout` succeeds with no Secret in sight |
| **Britive Audit Log** | OIDC federation auth event for your federated SI; checkout/checkin events on each profile |
| **AWS S3** | `s3://<S3_BUCKET>/jit-demo/aws-run-<id>.txt` |
| **GCS** | `gs://<GCS_BUCKET>/jit-demo/gcp-run-<id>.txt` |
| **AWS CloudTrail** | STS AssumeRole-with-SAML event tied to the federated identity |
| **GCP IAM** | Temporary shadow SA bound during run, gone after checkin (when working — see Known Issues) |

## Known issues

**GCP-WIF profile checkout via federated SI does not currently attach roles to the shadow SA.** Britive creates the per-checkout shadow SA and returns a key, but the requested role (e.g., `roles/storage.admin`) is not bound to that SA, so writes return 403. AWS profile checkouts work because they use SAML role assumption rather than the shadow-SA model. The GCP job in this workflow has `continue-on-error: true` while this is being investigated with Britive.

## The narrative

> *"This public repo has no stored credentials. Run any workflow — AWS Secret Manager and GCP Service Account keys are minted at runtime by Britive, scoped to just this repo on this branch, and revoked on checkin. Static-key rotation isn't needed because there are no static keys to rotate."*
