# Deployment

## Target architecture on Google Cloud

```
 users ──HTTPS──> Load balancer + IAP ──> Cloud Run: sapkit web (FastAPI + agent)
                  (company SSO;             │  identity from IAP header → RBAC
                   only listed groups)       ├── Claude on Vertex AI (Google IAM, no Anthropic key)
                                             ├── data: DuckDB file on a Cloud Storage volume (pilot)
                                             │         └─ BigQuery raw/mm/ops datasets (production)
                                             └── Gmail / Outlook drafts (per-buyer consent)
 SAP ──extract──> BigQuery sap raw dataset ──> curated views (generated) + row access policies (generated)
```

Everything is defined in `deploy/terraform`:
- APIs, and a least-privilege service account;
- BigQuery raw / mm / ops datasets with dataset-level IAM;
- Artifact Registry;
- a versioned, private data bucket;
- a Secret Manager entry for the content-filter URL;
- Cloud Run (internal ingress, one instance for the pilot);
- IAP access per group.

`terraform validate` passes; it hasn't been applied to a real project from here.

## Steps

1. **Project and image**
   ```bash
   gcloud builds submit --tag <region>-docker.pkg.dev/<project>/sapkit-<env>/sapkit:<version>
   ```
2. **Infrastructure**
   ```bash
   cd deploy/terraform
   cp envs/stage.tfvars.example envs/stage.tfvars   # edit project, image, IAP groups
   terraform init && terraform apply -var-file=envs/stage.tfvars
   ```
   Then put the HTTPS load balancer with IAP in front of the Cloud Run service
   (serverless NEG). The client's network team usually owns the domain and
   certificate.
3. **Data**
   - Load the SAP extract into the raw dataset, through SAP Data Services or `bq load` of the contract CSVs.
   - Generate and apply the curated views and the row access policies:
     ```bash
     python -m sapkit bigquery --project <project> --raw sapkit_raw --curated sapkit_mm \
         --service-account <app SA> --user admin@...
     bq query --use_legacy_sql=false < out/bigquery/10_curated_views.sql
     bq query --use_legacy_sql=false < out/bigquery/20_row_access_policies.sql
     ```
   - For the pilot, also upload the DuckDB file built by `sapkit load` to the data bucket.
4. **Model.** Enable Claude in Vertex AI Model Garden for the project. The app
   runs with `SAPKIT_LLM_PROVIDER=vertex` and uses the service account; no
   Anthropic key exists anywhere.
5. **Users.** Add people to `config/rbac.yaml` (reviewed), and their group to
   `iap_members`.
6. **Mail.** Each buyer consents once to the Gmail or Outlook adapter
   (draft-only scopes); tokens live in Secret Manager or Key Vault.
7. **Check.**
   - Run `sapkit eval --runner api` against the deployed configuration.
   - Run the Promptfoo red team.
   - Have an auditor confirm `/api/audit` shows the test traffic.

## Pilot vs production

| Concern | Pilot (as built) | Production |
|---|---|---|
| Rule engine | DuckDB over the loaded extract | Same SQL, generated as BigQuery views (`sapkit bigquery`) |
| Ops store (audit, drafts, replies, traces) | DuckDB file on a Cloud Storage volume; **one** Cloud Run instance | BigQuery ops dataset; Cloud Run can scale out |
| Row-level security | Scoped views per session | BigQuery row access policies generated from rbac.yaml, plus scoped views |
| Model | Claude on Vertex AI | Same |
| Auth | IAP | IAP |

The production column's runtime backend (the agent querying BigQuery directly
and writing ops rows to BigQuery) is the next build step. The SQL and policies
it needs are already generated and parse-checked as BigQuery SQL.

## Configuration reference

| Variable | Meaning |
|---|---|
| `SAPKIT_AUTH` | `iap` (production) or `dev` (local only) |
| `SAPKIT_LLM_PROVIDER` | `anthropic` or `vertex` |
| `SAPKIT_VERTEX_PROJECT`, `SAPKIT_VERTEX_REGION` | Claude on Vertex AI |
| `ANTHROPIC_API_KEY` | Only for `anthropic` provider |
| `SAPKIT_DB` | Path to the DuckDB data file |
| `SAPKIT_CONTENT_FILTER_URL` | External input/output screening (fails closed) |
| `SAPKIT_LOG_JSON=1` | One JSON log line per question to stderr (Cloud Logging) |
| `OPIK_URL_OVERRIDE` / `OPIK_API_KEY` | Send traces to Opik |
| `SAPKIT_USER`, `SAPKIT_PLANTS` | MCP server identity and optional narrowing |
