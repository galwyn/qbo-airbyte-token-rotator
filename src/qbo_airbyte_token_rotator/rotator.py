"""Rotate the QuickBooks Online refresh token used by an Airbyte Cloud source.

Intuit issues *rolling* refresh tokens: every successful refresh invalidates the
token that was presented and returns a new one. Airbyte's QuickBooks source
refreshes on its own, but if the token it holds ever goes stale (or another
process consumes it) the source breaks and needs a manual re-authorisation.

This tool keeps one source of truth for the refresh token in a secret store
(Google Secret Manager by default; AWS Secrets Manager and Azure Key Vault are
also supported, see stores.py). Each run it:

1. reads the current refresh token (from the store, or from a value you pass),
2. exchanges it at Intuit for a new access + refresh token pair,
3. writes the new refresh token back to the store as a new secret version,
4. patches the Airbyte source configuration with the new tokens.
"""

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone

import requests

from .stores import STORE_KINDS, GcpSecretManagerStore, make_store

AIRBYTE_TOKEN_URL = "https://api.airbyte.com/v1/applications/token"
AIRBYTE_SOURCES_URL = "https://api.airbyte.com/v1/sources"
INTUIT_TOKEN_URL = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"

# Intuit's documented refresh token lifetime (100 days), used when the
# response does not carry x_refresh_token_expires_in.
DEFAULT_REFRESH_TOKEN_EXPIRES_IN = 8640000


def get_secret(project_id, secret_name):
    """
    Fetches the latest version of a secret from Google Secret Manager.
    """
    return GcpSecretManagerStore(project_id).read_secret(secret_name)


def _redact_headers(headers):
    """Returns request headers as a string with credentials removed."""
    safe = {}
    for key, value in dict(headers or {}).items():
        safe[key] = "<redacted>" if key.lower() in ("authorization", "proxy-authorization") else value
    return str(safe)


def rotate_qb_token(airbyte_source_id: str, airbyte_client_id: str, airbyte_client_secret: str,
                    qb_client_id: str, qb_client_secret: str, realm_id: str,
                    gcp_project_id: str = None, refresh_token_secret_name: str = None, refresh_token: str = None,
                    store=None):
    """
    Rotates the QuickBooks Refresh Token by calling Intuit and updating Airbyte.

    WARNING: INTUIT USES ROLLING REFRESH TOKENS.
    Any successful call to Intuit's token endpoint will immediately invalidate the
    current refresh token and issue a new one. Do not test or execute this code
    unless you are prepared to save the newly generated token back to Secret Manager
    or Airbyte. If a token is consumed without saving the response, the chain is broken
    and a manual reset via the Intuit Developer Portal is required.

    `store` is a TokenStore. When it is omitted and `gcp_project_id` is set,
    Google Secret Manager in that project is used.
    """

    # Setup structured logging
    logging.basicConfig(level=logging.INFO, format='%(message)s')

    def log(message, severity="INFO", **kwargs):
        entry = dict(severity=severity, message=message, **kwargs)
        print(json.dumps(entry))

    try:
        if store is None and gcp_project_id:
            store = GcpSecretManagerStore(gcp_project_id)

        refresh_token_to_use = ""
        # --- Determine which refresh token to use ---
        if refresh_token:
            refresh_token_to_use = refresh_token
        elif store is not None and refresh_token_secret_name:
            log(f"Fetching refresh token from {store.label}: {refresh_token_secret_name}")
            try:
                refresh_token_to_use = store.read_secret(refresh_token_secret_name)
                log(f"Successfully retrieved refresh token from {store.label}.")
            except Exception as e:
                log(f"Failed to retrieve refresh token secret: {e}", severity="ERROR")
                sys.exit(1)
        else:
            log("ERROR: No refresh token source provided.", severity="ERROR")
            sys.exit(1)

        # 1. Get a temporary Airbyte Access Token
        log("Step 1: Getting Airbyte Access Token...")
        r_airbyte_token = None
        try:
            r_airbyte_token = requests.post(
                AIRBYTE_TOKEN_URL,
                json={"client_id": airbyte_client_id, "client_secret": airbyte_client_secret, "grant_type": "client_credentials"}
            )
            r_airbyte_token.raise_for_status()
            airbyte_access_token = r_airbyte_token.json()["access_token"]
        except requests.exceptions.RequestException as e:
            log("Failed to get Airbyte Access Token", severity="ERROR", error=str(e),
                response=r_airbyte_token.text if r_airbyte_token is not None else None)
            raise

        headers = {"Authorization": f"Bearer {airbyte_access_token}", "Accept": "application/json", "Content-Type": "application/json"}
        source_url = f"{AIRBYTE_SOURCES_URL}/{airbyte_source_id}"

        # 2. Call Intuit to Exchange the token for a NEW one
        log("Step 2: Calling Intuit API to refresh tokens...")

        r_intuit = requests.post(
            INTUIT_TOKEN_URL,
            auth=(qb_client_id, qb_client_secret),
            data={"grant_type": "refresh_token", "refresh_token": refresh_token_to_use}
        )

        if r_intuit.status_code != 200:
            log("Intuit Rejected Refresh", severity="ERROR",
                status_code=r_intuit.status_code,
                response_body=r_intuit.text,
                # intuit_tid identifies the request when contacting Intuit support.
                intuit_tid=r_intuit.headers.get("intuit_tid"),
                # The Basic auth header carries the QuickBooks client secret.
                request_headers=_redact_headers(r_intuit.request.headers)
            )
            sys.exit(1)

        new_tokens = r_intuit.json()
        new_access_token = new_tokens["access_token"]
        new_refresh_token = new_tokens["refresh_token"]

        # 3. If in automated mode, write the new refresh token back to the store if it has changed
        if store is not None and refresh_token_secret_name:
            if new_refresh_token != refresh_token_to_use:
                log(f"New refresh token received. Writing to {store.label}...")
                store.write_secret(refresh_token_secret_name, new_refresh_token)
                log(f"Successfully updated secret in {store.label}.")
            else:
                log("Refresh token returned is identical. No update needed.")

        # 4. Patch Airbyte with the NEW Tokens
        log("Step 3: Patching Airbyte Configuration...")
        r_source_pre_patch = requests.get(source_url, headers=headers)
        r_source_pre_patch.raise_for_status()
        config_to_patch = r_source_pre_patch.json()["configuration"]

        # Use refresh token expiry (usually 100 days) for Airbyte configuration
        # This ensures Airbyte keeps using the refresh token to get new access tokens
        refresh_token_expires_in = new_tokens.get("x_refresh_token_expires_in", DEFAULT_REFRESH_TOKEN_EXPIRES_IN)
        new_expiry_datetime = datetime.now(timezone.utc) + timedelta(seconds=refresh_token_expires_in)
        new_expiry_date_str = new_expiry_datetime.strftime('%Y-%m-%dT%H:%M:%SZ')

        config_to_patch["client_id"] = qb_client_id
        config_to_patch["client_secret"] = qb_client_secret
        config_to_patch["refresh_token"] = new_refresh_token
        config_to_patch["access_token"] = new_access_token
        config_to_patch["token_expiry_date"] = new_expiry_date_str
        config_to_patch["realm_id"] = realm_id

        patch_payload = {"configuration": config_to_patch}

        r_patch = None
        try:
            r_patch = requests.patch(source_url, json=patch_payload, headers=headers)
            r_patch.raise_for_status()
        except requests.exceptions.RequestException as e:
            log("Failed to patch Airbyte config", severity="ERROR", error=str(e),
                response=r_patch.text if r_patch is not None else None)
            raise

        log("Token rotated and patched successfully into Airbyte.")
        return 0

    except Exception as e:
        log("CRITICAL: Rotation Failed with unhandled exception", severity="ERROR", error=str(e))
        import traceback
        traceback.print_exc()
        sys.exit(1)


def build_parser():
    parser = argparse.ArgumentParser(
        prog="qbo-airbyte-token-rotator",
        description="Rotate the QuickBooks Online refresh token used by an Airbyte Cloud source.",
    )
    group = parser.add_mutually_exclusive_group(required=False)  # not required: values may come from env vars
    group.add_argument("--refresh_token", help="The QuickBooks refresh token value itself.", default=os.environ.get("REFRESH_TOKEN"))
    group.add_argument("--refresh_token_secret_name", help="Name of the stored secret containing the refresh token.", default=os.environ.get("REFRESH_TOKEN_SECRET_NAME"))

    parser.add_argument("--token_store", choices=STORE_KINDS, default=os.environ.get("TOKEN_STORE", "gcp"), help="Where secrets live: gcp (default), aws or azure.")
    parser.add_argument("--gcp_project_id", help="Google Cloud project ID that holds the secrets (token_store=gcp).", default=os.environ.get("GCP_PROJECT_ID"))
    parser.add_argument("--aws_region", help="AWS region of the secrets (token_store=aws). Defaults to the SDK's region.", default=os.environ.get("AWS_REGION"))
    parser.add_argument("--azure_key_vault_url", help="Key Vault URL, e.g. https://<vault>.vault.azure.net (token_store=azure).", default=os.environ.get("AZURE_KEY_VAULT_URL"))
    parser.add_argument("--airbyte_source_id", default=os.environ.get("AIRBYTE_SOURCE_ID"), help="Airbyte Source ID for QuickBooks.")
    parser.add_argument("--airbyte_client_id", default=os.environ.get("AIRBYTE_CLIENT_ID"), help="Airbyte API Client ID.")
    parser.add_argument("--airbyte_client_secret", default=os.environ.get("AIRBYTE_CLIENT_SECRET"), help="Airbyte API Client Secret. If not provided, read from the secret named by --airbyte_client_secret_name.")
    parser.add_argument("--airbyte_client_secret_name", default=os.environ.get("AIRBYTE_CLIENT_SECRET_NAME"), help="Name of the stored secret holding the Airbyte API Client Secret.")
    parser.add_argument("--qb_client_id", default=os.environ.get("QB_CLIENT_ID"), help="QuickBooks OAuth Client ID.")
    parser.add_argument("--qb_client_secret", default=os.environ.get("QB_CLIENT_SECRET"), help="QuickBooks OAuth Client Secret. If not provided, read from the secret named by --qb_client_secret_name.")
    parser.add_argument("--qb_client_secret_name", default=os.environ.get("QB_CLIENT_SECRET_NAME"), help="Name of the stored secret holding the QuickBooks OAuth Client Secret.")
    parser.add_argument("--realm_id", default=os.environ.get("REALM_ID"), help="QuickBooks Realm (company) ID.")

    parser.add_argument("--dry_run", action="store_true", help="Fetch secrets but do not call external APIs (for testing).")
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)

    # A store is only needed when some value is read from it by name.
    store = None
    if args.refresh_token_secret_name or args.airbyte_client_secret_name or args.qb_client_secret_name:
        try:
            store = make_store(args.token_store, gcp_project_id=args.gcp_project_id,
                               aws_region=args.aws_region, azure_key_vault_url=args.azure_key_vault_url)
        except (RuntimeError, ValueError) as e:
            parser.error(str(e))

    # --- Fetch Secrets from the token store if missing ---
    if store is not None:
        if not args.airbyte_client_secret and args.airbyte_client_secret_name:
            try:
                print(f"Fetching airbyte_client_secret from {store.label}...")
                args.airbyte_client_secret = store.read_secret(args.airbyte_client_secret_name)
            except Exception as e:
                print(f"Warning: Failed to fetch {args.airbyte_client_secret_name}: {e}")

        if not args.qb_client_secret and args.qb_client_secret_name:
            try:
                print(f"Fetching qb_client_secret from {store.label}...")
                args.qb_client_secret = store.read_secret(args.qb_client_secret_name)
            except Exception as e:
                print(f"Warning: Failed to fetch {args.qb_client_secret_name}: {e}")

    # Validation: Ensure all required values are present
    missing_args = []
    if not args.airbyte_source_id: missing_args.append("airbyte_source_id")
    if not args.airbyte_client_id: missing_args.append("airbyte_client_id")
    if not args.airbyte_client_secret: missing_args.append("airbyte_client_secret")
    if not args.qb_client_id: missing_args.append("qb_client_id")
    if not args.qb_client_secret: missing_args.append("qb_client_secret")
    if not args.realm_id: missing_args.append("realm_id")

    if missing_args:
        parser.error(f"Missing required arguments (or environment variables) and failed to fetch from the token store: {', '.join(missing_args)}")

    if args.refresh_token_secret_name and store is None:
        if args.token_store == "azure":
            parser.error("--azure_key_vault_url is required when using --refresh_token_secret_name with token_store=azure.")
        parser.error("--gcp_project_id is required when using --refresh_token_secret_name.")

    # Validation: Must have either a token or a secret name
    if not args.refresh_token and not args.refresh_token_secret_name:
        parser.error("One of --refresh_token or --refresh_token_secret_name (or REFRESH_TOKEN/REFRESH_TOKEN_SECRET_NAME env vars) is required.")

    # --- DRY RUN CHECK ---
    if args.dry_run:
        print("DRY RUN ENABLED: Secrets fetched successfully.")
        print(f"  - airbyte_client_secret length: {len(args.airbyte_client_secret)}")
        print(f"  - qb_client_secret length: {len(args.qb_client_secret)}")
        print("Exiting without calling external APIs.")
        return 0

    return rotate_qb_token(
        gcp_project_id=args.gcp_project_id,
        airbyte_source_id=args.airbyte_source_id,
        airbyte_client_id=args.airbyte_client_id,
        airbyte_client_secret=args.airbyte_client_secret,
        qb_client_id=args.qb_client_id,
        qb_client_secret=args.qb_client_secret,
        refresh_token_secret_name=args.refresh_token_secret_name,
        refresh_token=args.refresh_token,
        realm_id=args.realm_id,
        store=store,
    )


def cli():
    sys.exit(main())


if __name__ == "__main__":
    cli()
