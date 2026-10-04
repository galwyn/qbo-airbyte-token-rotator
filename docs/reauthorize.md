# Re-authorising QuickBooks by hand

The rotator can only keep a refresh-token chain alive. It can't start a new chain. If the chain breaks, a person has to get a fresh refresh token from Intuit once and put it in the token store. The next run then picks it up.

## When you need this

- The rotator logs `Intuit Rejected Refresh` with `invalid_grant` in the response body, and running it again gives the same result.
- The rotator hasn't run successfully for about 100 days, so the refresh token expired. Intuit's refresh tokens live for about 100 days from when they were issued.
- Something else used the refresh token without saving the replacement. That could be a second rotator, a manual test, or another tool sharing the Intuit app. Intuit then retired the token the store still holds.
- Someone disconnected the app from the QuickBooks company, or changed the Intuit app's client secret.

If the rotator fails for any other reason (Airbyte credentials, network, permissions on the store), fix that instead. Re-authorising won't help, and it starts a new chain you don't need.

## Step 1: get a new refresh token

You need to sign in to the Intuit Developer Portal with an account that can manage the Intuit app, and you need admin access to the QuickBooks company.

### Option A: the Intuit OAuth 2.0 Playground (simplest)

1. Sign in at https://developer.intuit.com and open the OAuth 2.0 Playground. In the current portal it's under **Tools**.
2. In **Select app**, choose the **production** app whose client ID and secret the rotator uses. A token issued to a different app, or to the sandbox keys, won't work with the rotator's client secret.
3. Under **Select scopes**, tick only `com.intuit.quickbooks.accounting`.
4. Click **Get authorization code**. Sign in if asked, choose the QuickBooks company, and approve the connection.
5. Back in the Playground, note the **Realm ID** it shows and check that it matches the rotator's `REALM_ID`.
6. Click **Get tokens**. In the response, copy the value of `refresh_token`. It's a long string.

If Intuit rejects the redirect, add the Playground's redirect URI to the app's **Redirect URIs** under its production keys, then try again.

### Option B: your own authorization-code flow

If your app already has an OAuth redirect, run the standard OAuth 2.0 authorization-code flow against `https://appcenter.intuit.com/connect/oauth2` with scope `com.intuit.quickbooks.accounting`. Then exchange the code at `https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer`, and keep the `refresh_token` from the response.

Treat the refresh token like a password: don't paste it into chat, tickets or shell history. The commands below read it from your clipboard or prompt for it.

## Step 2: store it

Store it as a new version of the secret named by `REFRESH_TOKEN_SECRET_NAME`, in the store the rotator uses.

**Google Secret Manager** (macOS clipboard shown; on Linux use `xclip -o` or paste into `read -s`):

```bash
pbpaste | gcloud secrets versions add <refresh-token-secret> --project=<project-id> --data-file=-
```

**AWS Secrets Manager:**

```bash
read -rs TOKEN && aws secretsmanager put-secret-value --secret-id <refresh-token-secret> --secret-string "$TOKEN" && unset TOKEN
```

**Azure Key Vault:**

```bash
read -rs TOKEN && az keyvault secret set --vault-name <vault-name> --name <refresh-token-secret> --value "$TOKEN" --output none && unset TOKEN
```

## Step 3: run the rotator once

Run the rotator now rather than waiting for the schedule. It exchanges the new token, writes the replacement back, and puts fresh credentials into Airbyte.

```bash
gcloud run jobs execute <job-name> --region=<region> --project=<project-id> --wait
```

On another platform, trigger your job or function the same way you would by hand. A successful run ends with `Token rotated and patched successfully into Airbyte.`

## Step 4: sync within the hour

The access token the rotator gives Airbyte lasts one hour (see "How the access token is handled" in the README). Start a sync in Airbyte within that hour to confirm data flows again.

## Avoiding it next time

- Only one thing may use the refresh token: this rotator, on one schedule. Don't point a second environment (staging, a laptop) at the same secret and the same Intuit app.
- Keep the job's retries at zero. A retry after a run that refreshed at Intuit but failed afterwards presents a token that's already been used.
- Alert on the rotator's failures. A daily schedule leaves about 100 days to notice before the chain expires.
