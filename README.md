# qbo-airbyte-token-rotator

Keeps an [Airbyte Cloud](https://airbyte.com) QuickBooks Online source alive by rotating Intuit's refresh token and storing it in Google Secret Manager.

## The problem

Intuit's OAuth 2.0 refresh tokens are **rolling**. Every successful refresh invalidates the token you presented and returns a new one. The Airbyte QuickBooks source holds a single refresh token in its configuration. If that token expires, or if any other process uses it once without saving the replacement, every sync fails. Someone then has to re-authorise the app by hand in the Intuit Developer Portal.

## What this tool does

Each run:

1. Reads the current refresh token from a Google Secret Manager secret, or from a value you pass.
2. Exchanges it at Intuit's token endpoint for a new access token and refresh token.
3. Writes the new refresh token back to Secret Manager as a new secret version, if it changed.
4. Reads the Airbyte source configuration and patches in the new tokens, the token expiry date, the QuickBooks client ID and secret, and the realm ID. All other configuration fields are left as they were.

If you run it on a schedule (daily is plenty), the token never ages out, and Secret Manager always holds the token Airbyte is using.

> **Warning: every run consumes the current refresh token.** Don't call Intuit's token endpoint with the same refresh token from anywhere else, and don't run this tool unless it can write the result back. A token that is used without saving its replacement breaks the chain, and you have to re-authorise in the Intuit Developer Portal.

## Requirements

- An Airbyte Cloud workspace with a QuickBooks source, and an Airbyte API application (client ID and client secret) from **Settings → Applications**.
- An Intuit developer app (QuickBooks client ID and client secret) and the realm (company) ID of the connected company.
- A Google Cloud project with Secret Manager enabled. The identity running the tool needs `roles/secretmanager.secretAccessor` and `roles/secretmanager.secretVersionAdder` on the refresh-token secret, and accessor on any other secrets it reads.

## Configuration

Every option can be set as a command-line flag or as an environment variable.

| Environment variable | Flag | Required | Meaning |
|---|---|---|---|
| `AIRBYTE_SOURCE_ID` | `--airbyte_source_id` | yes | ID of the Airbyte QuickBooks source |
| `AIRBYTE_CLIENT_ID` | `--airbyte_client_id` | yes | Airbyte API application client ID |
| `AIRBYTE_CLIENT_SECRET` | `--airbyte_client_secret` | yes, unless the `_NAME` form is set | Airbyte API application client secret |
| `AIRBYTE_CLIENT_SECRET_NAME` | `--airbyte_client_secret_name` | no | Secret Manager secret that holds the Airbyte client secret |
| `QB_CLIENT_ID` | `--qb_client_id` | yes | Intuit app client ID |
| `QB_CLIENT_SECRET` | `--qb_client_secret` | yes, unless the `_NAME` form is set | Intuit app client secret |
| `QB_CLIENT_SECRET_NAME` | `--qb_client_secret_name` | no | Secret Manager secret that holds the Intuit client secret |
| `REALM_ID` | `--realm_id` | yes | QuickBooks realm (company) ID |
| `GCP_PROJECT_ID` | `--gcp_project_id` | with any `*_SECRET_NAME` | Project that holds the secrets |
| `REFRESH_TOKEN_SECRET_NAME` | `--refresh_token_secret_name` | one of these two | Secret holding the refresh token (read, then updated) |
| `REFRESH_TOKEN` | `--refresh_token` | one of these two | Refresh token value, for a one-off manual run (nothing is written to Secret Manager) |

`--dry_run` resolves every secret, prints their lengths, and exits without calling Airbyte or Intuit.

## Install and run locally

```bash
pip install git+https://github.com/galwyn/qbo-airbyte-token-rotator.git
```

```bash
qbo-airbyte-token-rotator --dry_run
```

Logs are written as one JSON object per line with a `severity` field, so Cloud Logging parses them directly.

## First-time setup

1. Create a secret for the refresh token and store the current one. You can get it from the Intuit OAuth Playground, or copy the token Airbyte holds now:

   ```bash
   printf '%s' "<current-refresh-token>" | gcloud secrets create <refresh-token-secret> --data-file=- --project=<project-id>
   ```

2. Store the two client secrets the same way, as `<airbyte-client-secret>` and `<qb-client-secret>`.

## Deploy on Cloud Run Jobs with Cloud Scheduler

All names below are placeholders.

```bash
gcloud builds submit --tag <region>-docker.pkg.dev/<project-id>/<repo>/qbo-airbyte-token-rotator --project=<project-id>
```

```bash
gcloud run jobs create qbo-airbyte-token-rotator \
  --image=<region>-docker.pkg.dev/<project-id>/<repo>/qbo-airbyte-token-rotator \
  --region=<region> --project=<project-id> \
  --service-account=<rotator-sa>@<project-id>.iam.gserviceaccount.com \
  --max-retries=0 \
  --set-env-vars=GCP_PROJECT_ID=<project-id>,AIRBYTE_SOURCE_ID=<airbyte-source-id>,AIRBYTE_CLIENT_ID=<airbyte-client-id>,QB_CLIENT_ID=<qb-client-id>,REALM_ID=<realm-id>,REFRESH_TOKEN_SECRET_NAME=<refresh-token-secret>,AIRBYTE_CLIENT_SECRET_NAME=<airbyte-client-secret>,QB_CLIENT_SECRET_NAME=<qb-client-secret>
```

`--max-retries=0` matters. A retry after a run that refreshed at Intuit but failed later could present a token that has already been consumed.

```bash
gcloud scheduler jobs create http qbo-airbyte-token-rotator-daily \
  --location=<region> --project=<project-id> \
  --schedule="0 6 * * *" \
  --uri="https://run.googleapis.com/v2/projects/<project-id>/locations/<region>/jobs/qbo-airbyte-token-rotator:run" \
  --http-method=POST \
  --oauth-service-account-email=<scheduler-sa>@<project-id>.iam.gserviceaccount.com
```

The scheduler's service account needs `roles/run.invoker` on the job.

## Development

```bash
pip install -e ".[test]"
```

```bash
pytest
```

The tests mock Airbyte, Intuit and Secret Manager, so they never touch a real token.

## License

MIT. See [LICENSE](LICENSE).
