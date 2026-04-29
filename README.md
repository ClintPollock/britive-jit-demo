# OpenLoop JIT Demo — GitHub Actions OIDC → Britive → [GCP, AWS]

Demonstrates **JIT for cloud workloads** with zero static credentials in the GitHub repo.

## What's wired in Britive (openloop-poc tenant)

- **OIDC IdP**: `GitHub` (id `1`)
  - issuer: `https://token.actions.githubusercontent.com`
  - allowed audiences: `britive-openloop-poc`
  - attribute map: `sub` → custom attribute `GitHub Subject` (id `dwnnahey385tip66lmgi`)
- **Federated Service Identity**: `github-openloop-jit-demo` (userId `d6849rdvd5eyj8yb0x8h`)
  - Bound to GitHub IdP with subject value: `repo:clintpollock/openloop-jit-demo:ref:refs/heads/main`
  - Token duration: 600s (10 min)
  - Member of `POC Group` tag → has policies for both GCP storage-admin and AWS admin profiles

## Repo setup

1. Create **public** repo `clintpollock/openloop-jit-demo` (matches the SI subject)
2. Drop `jit-demo.yml` into `.github/workflows/`
3. Commit + push to `main`
4. Trigger via Actions tab → `openloop-jit-demo` → Run workflow (or any push to main)

The workflow self-creates the S3 bucket on first run, so no AWS-side prep needed.

## What you'll see during the demo

| Surface | What changes |
|---|---|
| GitHub Actions log | Two jobs run in parallel: `gcp-jit-write`, `aws-jit-write`. Each does `pybritive checkout` with no Secret in sight |
| Britive Audit Log | Federation auth event for SI `github-openloop-jit-demo`; checkout/checkin on each profile |
| GCS | `gs://openloop-poc-demo-585077634997/jit-demo/gcp-run-<id>.txt` |
| S3 | `s3://openloop-poc-jit-demo-080147880424/jit-demo/aws-run-<id>.txt` |
| GCP IAM | Temporary shadow SA bound during run, gone after checkin |
| AWS CloudTrail | STS AssumeRole events tied to the federated identity |

## Repo settings (zero secrets!)

`Settings → Secrets and variables → Actions` should be **completely empty**. No `AWS_ACCESS_KEY_ID`, no `GCP_SA_KEY`, no `BRITIVE_TOKEN`. The only requirement is workflow `permissions: id-token: write`, which is in the YAML itself.

## If you change the repo name

Update the SI subject:
```bash
pybritive api identity_management.workload.service_identities.assign \
  --service-identity-id d6849rdvd5eyj8yb0x8h \
  --idp-id 1 \
  --federated-attributes '{"GitHub Subject":"repo:YOUR-ORG/YOUR-REPO:ref:refs/heads/main"}'
```
Or via UI: `Identity Management → Service Identities → github-openloop-jit-demo → Federation`.

## The narrative

> *"This public repo has zero stored credentials. No AWS Secret, no GCP key, no Britive token. Each workflow run gets short-lived creds for both clouds — minted by Britive, delivered via GitHub's built-in OIDC token, scoped to just this repo on this branch. Nothing to leak. Nothing to rotate. The same model works for any CI/CD pipeline you have."*
