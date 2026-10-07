# qbo-airbyte-token-rotator

Keeps an [Airbyte Cloud](https://airbyte.com) QuickBooks Online source alive by rotating Intuit's refresh token and keeping it in a cloud secret store. Google Secret Manager is the default. AWS Secrets Manager and Azure Key Vault are also supported.

## The problem

Intuit's OAuth 2.0 refresh tokens are **rolling**. Every successful refresh invalidates the token you presented and returns a new one. The Airbyte QuickBooks source holds a single refresh token in its configuration. If that token expires, or if any other process uses it once without saving the replacement, every sync fails. Someone then has to re-authorise the app by hand; the steps are in [docs/reauthorize.md](docs/reauthorize.md).

## What this tool does

Each run:

1. Reads the current refresh token from your secret store, or from a value you pass.
2. Exchanges it at Intuit's token endpoint for a new access token and refresh token.
3. Writes the new refresh token back to the store as a new secret version, if it changed.
4. Reads the Airbyte source configuration and patches in the new tokens, the token expiry date, the QuickBooks client ID and secret, and the realm ID. All other configuration fields are left as they were.

> **Warning: every run consumes the current refresh token.** Don't call Intuit's token endpoint with the same refresh token from anywhere else, and don't run this tool unless it can write the result back. A token that is used without saving its replacement breaks the chain, and you have to re-authorise by hand ([docs/reauthorize.md](docs/reauthorize.md)).

## How the access token is handled: schedule the rotation just before the sync

The tool writes `token_expiry_date` on the Airbyte source as the **refresh token's** expiry, about 100 days away, not the access token's (one hour). Airbyte therefore keeps using the access token the rotator gave it and doesn't renew it on its own. That keeps exactly one holder of the refresh token, this tool, so Airbyte and the secret store never disagree about which refresh token is current.

The cost is that **each sync must start and finish within one hour of a rotation**. Schedule the rotator a few minutes before your Airbyte sync, for example rotation at 00:45 and sync at 01:00, every day. If a sync fails because it ran past the hour, run the rotator again and start the sync within the hour.

Making Airbyte renew access tokens itself would remove the one-hour window. But Airbyte might then receive a new refresh token from Intuit that the secret store never sees, and the next rotation would push a stale one back. That trade hasn't been tested, so the tool doesn't make it.

## Connector version

The tool needs `airbyte/source-quickbooks` **4.0.0 or later**. It has been run against the 4.x line on Airbyte Cloud, and 4.2.0 is the latest version at the time of writing.

Version 4.0.0 moved the credential fields (`client_id`, `client_secret`, `refresh_token`, `access_token`, `token_expiry_date`, `realm_id`) from a nested `credentials` object to the top level of the source configuration. The tool writes the top-level fields only. On a 3.x source it would report success while the connector kept reading the old token from `credentials`, and syncs would fail.

To check your version, open the source in Airbyte. Its settings page shows the connector version. A source created before 4.0.0 may still hold a nested `credentials` object. Version 4.2.0 migrates it to the top level automatically at the start of a sync. On earlier 4.x versions, re-enter the credentials in the source settings before you start using the tool.

The tool calls the Airbyte public API (`https://api.airbyte.com/v1`) and Intuit's OAuth 2.0 token endpoint. Neither has a version to choose.

## Requirements

- An Airbyte Cloud workspace with a QuickBooks source, and an Airbyte API application (client ID and client secret) from **Settings → Applications**.
- The Airbyte QuickBooks source connector (`airbyte/source-quickbooks`) at **version 4.0.0 or later**. See [Connector version](#connector-version).
- An Intuit developer app (QuickBooks client ID and client secret) and the realm (company) ID of the connected company.
- A secret store, and an identity for the job that can read the secrets and add a new version of the refresh-token secret:

  | Store | Read | Write a new version |
  |---|---|---|
  | Google Secret Manager | `roles/secretmanager.secretAccessor` | `roles/secretmanager.secretVersionAdder` |
  | AWS Secrets Manager | `secretsmanager:GetSecretValue` | `secretsmanager:PutSecretValue` |
  | Azure Key Vault | `Key Vault Secrets User` | `Key Vault Secrets Officer` |

The Google Secret Manager backend is what the author runs in production. The AWS and Azure backends are covered by unit tests with mocked SDKs and haven't been run against real accounts yet. Reports and fixes are welcome.

## Install

Install the extra for your cloud:

```bash
pip install "qbo-airbyte-token-rotator[gcp] @ git+https://github.com/galwyn/qbo-airbyte-token-rotator.git"
```

Replace `[gcp]` with `[aws]` or `[azure]` as needed. For the container image, pass the same choice at build time:

```bash
docker build --build-arg TOKEN_STORE_EXTRA=aws -t qbo-airbyte-token-rotator .
```

## Configuration

Every option can be set as a command-line flag or as an environment variable.

| Environment variable | Flag | Required | Meaning |
|---|---|---|---|
| `TOKEN_STORE` | `--token_store` | no | `gcp` (default), `aws` or `azure` |
| `GCP_PROJECT_ID` | `--gcp_project_id` | for `gcp` | Project that holds the secrets |
| `AWS_REGION` | `--aws_region` | no | Region of the secrets; defaults to the AWS SDK's own region setting |
| `AZURE_KEY_VAULT_URL` | `--azure_key_vault_url` | for `azure` | For example `https://<vault-name>.vault.azure.net` |
| `AIRBYTE_SOURCE_ID` | `--airbyte_source_id` | yes | ID of the Airbyte QuickBooks source |
| `AIRBYTE_CLIENT_ID` | `--airbyte_client_id` | yes | Airbyte API application client ID |
| `AIRBYTE_CLIENT_SECRET` | `--airbyte_client_secret` | yes, unless the `_NAME` form is set | Airbyte API application client secret |
| `AIRBYTE_CLIENT_SECRET_NAME` | `--airbyte_client_secret_name` | no | Stored secret that holds the Airbyte client secret |
| `QB_CLIENT_ID` | `--qb_client_id` | yes | Intuit app client ID |
| `QB_CLIENT_SECRET` | `--qb_client_secret` | yes, unless the `_NAME` form is set | Intuit app client secret |
| `QB_CLIENT_SECRET_NAME` | `--qb_client_secret_name` | no | Stored secret that holds the Intuit client secret |
| `REALM_ID` | `--realm_id` | yes | QuickBooks realm (company) ID |
| `REFRESH_TOKEN_SECRET_NAME` | `--refresh_token_secret_name` | one of these two | Stored secret holding the refresh token (read, then updated) |
| `REFRESH_TOKEN` | `--refresh_token` | one of these two | Refresh token value, for a one-off manual run (nothing is written to the store) |

On AWS and Azure the SDK finds credentials the usual way: an IAM role or managed identity when running in the cloud, your CLI login when running locally.

`--dry_run` resolves every secret, prints their lengths, and exits without calling Airbyte or Intuit.

Logs are written as one JSON object per line with a `severity` field, which Cloud Logging, CloudWatch and Azure Monitor can all parse. A failed Intuit refresh logs the HTTP status, Intuit's error body and its `intuit_tid` request ID, with the `Authorization` header redacted.

## First-time setup

1. Get a refresh token for your Intuit app and company, following Step 1 of [docs/reauthorize.md](docs/reauthorize.md). Alternatively, copy the token Airbyte holds now if the source is already working.
2. Create three secrets in your store: the refresh token, the Airbyte client secret and the Intuit client secret. Step 2 of [docs/reauthorize.md](docs/reauthorize.md) shows the command for each store.
3. Run `qbo-airbyte-token-rotator --dry_run` with your configuration to check that every secret resolves.
4. Deploy it on a schedule, as below, with **no automatic retries**. A retry after a run that refreshed at Intuit but failed later could present a token that has already been consumed.

## Scheduling examples

All names below are placeholders. Each example runs daily at 00:45 UTC, so an Airbyte sync at 01:00 falls inside the one-hour window.

### Google Cloud: Cloud Run Job and Cloud Scheduler (production-tested)

```bash
gcloud builds submit --tag <region>-docker.pkg.dev/<project-id>/<repo>/qbo-airbyte-token-rotator --project=<project-id>
```

```bash
gcloud run jobs create qbo-airbyte-token-rotator \
  --image=<region>-docker.pkg.dev/<project-id>/<repo>/qbo-airbyte-token-rotator \
  --region=<region> --project=<project-id> \
  --service-account=<rotator-sa>@<project-id>.iam.gserviceaccount.com \
  --max-retries=0 \
  --set-env-vars=TOKEN_STORE=gcp,GCP_PROJECT_ID=<project-id>,AIRBYTE_SOURCE_ID=<airbyte-source-id>,AIRBYTE_CLIENT_ID=<airbyte-client-id>,QB_CLIENT_ID=<qb-client-id>,REALM_ID=<realm-id>,REFRESH_TOKEN_SECRET_NAME=<refresh-token-secret>,AIRBYTE_CLIENT_SECRET_NAME=<airbyte-client-secret>,QB_CLIENT_SECRET_NAME=<qb-client-secret>
```

```bash
gcloud scheduler jobs create http qbo-airbyte-token-rotator-daily \
  --location=<region> --project=<project-id> \
  --schedule="45 0 * * *" --time-zone=UTC \
  --uri="https://run.googleapis.com/v2/projects/<project-id>/locations/<region>/jobs/qbo-airbyte-token-rotator:run" \
  --http-method=POST \
  --oauth-service-account-email=<scheduler-sa>@<project-id>.iam.gserviceaccount.com
```

The scheduler's service account needs `roles/run.invoker` on the job.

### AWS: ECS Fargate task and EventBridge Scheduler (unit-tested only)

Build the image with `--build-arg TOKEN_STORE_EXTRA=aws` and push it to ECR. Register a Fargate task definition that runs it with:

- the environment variables from the table, with `TOKEN_STORE=aws`;
- a task role allowed to read the three secrets and `PutSecretValue` on the refresh-token secret.

Then schedule the task:

```bash
aws scheduler create-schedule --name qbo-airbyte-token-rotator-daily \
  --schedule-expression "cron(45 0 * * ? *)" --schedule-expression-timezone UTC \
  --flexible-time-window Mode=OFF \
  --target '{"Arn":"arn:aws:ecs:<region>:<account-id>:cluster/<cluster>","RoleArn":"arn:aws:iam::<account-id>:role/<scheduler-role>","RetryPolicy":{"MaximumRetryAttempts":0},"EcsParameters":{"TaskDefinitionArn":"arn:aws:ecs:<region>:<account-id>:task-definition/<task-def>","LaunchType":"FARGATE","NetworkConfiguration":{"awsvpcConfiguration":{"Subnets":["<subnet-id>"],"AssignPublicIp":"ENABLED"}}}}'
```

A Lambda function works too. Package the library with the `[aws]` extra, call `qbo_airbyte_token_rotator.main([])` from the handler, and give the function the same permissions and a maximum of zero retries for asynchronous invocation.

### Azure: Container Apps Job on a schedule (unit-tested only)

Build the image with `--build-arg TOKEN_STORE_EXTRA=azure` and push it to your registry. Give the job a managed identity with **Key Vault Secrets Officer** on the vault, then create the job:

```bash
az containerapp job create --name qbo-airbyte-token-rotator --resource-group <resource-group> \
  --environment <container-apps-environment> \
  --trigger-type Schedule --cron-expression "45 0 * * *" \
  --replica-retry-limit 0 --replica-timeout 600 \
  --image <registry>/qbo-airbyte-token-rotator:latest \
  --mi-system-assigned \
  --env-vars TOKEN_STORE=azure AZURE_KEY_VAULT_URL=https://<vault-name>.vault.azure.net AIRBYTE_SOURCE_ID=<airbyte-source-id> AIRBYTE_CLIENT_ID=<airbyte-client-id> QB_CLIENT_ID=<qb-client-id> REALM_ID=<realm-id> REFRESH_TOKEN_SECRET_NAME=<refresh-token-secret> AIRBYTE_CLIENT_SECRET_NAME=<airbyte-client-secret> QB_CLIENT_SECRET_NAME=<qb-client-secret>
```

Container Apps job schedules are in UTC. A timer-triggered Azure Function calling `qbo_airbyte_token_rotator.main([])` is an alternative.

## When it breaks

See [docs/reauthorize.md](docs/reauthorize.md). It covers how to tell a broken token chain from other failures, how to get a new refresh token from Intuit, and how to put it back.

## Development

```bash
pip install -e ".[test]"
```

```bash
pytest
```

The tests mock Airbyte, Intuit and all three secret stores, so they never touch a real token.

## License

MIT. See [LICENSE](LICENSE).
