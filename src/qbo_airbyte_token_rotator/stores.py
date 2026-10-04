"""Where the refresh token and client secrets live.

A token store reads the latest value of a named secret and writes a new value
as a new version. The cloud SDK each backend needs is imported only when that
backend is used, so installing one cloud's extra is enough.

| TOKEN_STORE | Backend                 | Extra   | Settings                    |
|-------------|-------------------------|---------|-----------------------------|
| gcp         | Google Secret Manager   | [gcp]   | GCP_PROJECT_ID              |
| aws         | AWS Secrets Manager     | [aws]   | AWS_REGION (optional)       |
| azure       | Azure Key Vault         | [azure] | AZURE_KEY_VAULT_URL         |
"""

STORE_KINDS = ("gcp", "aws", "azure")


def _missing_extra(kind, error):
    return RuntimeError(
        f"The '{kind}' token store needs its SDK: pip install 'qbo-airbyte-token-rotator[{kind}]' ({error})"
    )


class TokenStore:
    label = "token store"

    def read_secret(self, name):
        raise NotImplementedError

    def write_secret(self, name, value):
        raise NotImplementedError


class GcpSecretManagerStore(TokenStore):
    label = "Secret Manager"

    def __init__(self, project_id):
        try:
            from google.cloud import secretmanager
        except ImportError as e:
            raise _missing_extra("gcp", e)
        self._secretmanager = secretmanager
        self.project_id = project_id

    def read_secret(self, name):
        client = self._secretmanager.SecretManagerServiceClient()
        path = f"projects/{self.project_id}/secrets/{name}/versions/latest"
        response = client.access_secret_version(request={"name": path})
        return response.payload.data.decode("UTF-8")

    def write_secret(self, name, value):
        client = self._secretmanager.SecretManagerServiceClient()
        parent = f"projects/{self.project_id}/secrets/{name}"
        client.add_secret_version(
            request={"parent": parent, "payload": {"data": value.encode("UTF-8")}}
        )


class AwsSecretsManagerStore(TokenStore):
    label = "AWS Secrets Manager"

    def __init__(self, region=None):
        try:
            import boto3
        except ImportError as e:
            raise _missing_extra("aws", e)
        self._client = boto3.client("secretsmanager", region_name=region) if region else boto3.client("secretsmanager")

    def read_secret(self, name):
        return self._client.get_secret_value(SecretId=name)["SecretString"]

    def write_secret(self, name, value):
        # put_secret_value creates a new version and moves AWSCURRENT to it.
        self._client.put_secret_value(SecretId=name, SecretString=value)


class AzureKeyVaultStore(TokenStore):
    label = "Azure Key Vault"

    def __init__(self, vault_url):
        try:
            from azure.identity import DefaultAzureCredential
            from azure.keyvault.secrets import SecretClient
        except ImportError as e:
            raise _missing_extra("azure", e)
        self._client = SecretClient(vault_url=vault_url, credential=DefaultAzureCredential())

    def read_secret(self, name):
        return self._client.get_secret(name).value

    def write_secret(self, name, value):
        # set_secret adds a new version; reads without a version return the newest.
        self._client.set_secret(name, value)


def make_store(kind, gcp_project_id=None, aws_region=None, azure_key_vault_url=None):
    """Builds the configured store, or returns None when it is not configured."""
    kind = (kind or "gcp").lower()
    if kind == "gcp":
        return GcpSecretManagerStore(gcp_project_id) if gcp_project_id else None
    if kind == "aws":
        return AwsSecretsManagerStore(aws_region)
    if kind == "azure":
        return AzureKeyVaultStore(azure_key_vault_url) if azure_key_vault_url else None
    raise ValueError(f"Unknown TOKEN_STORE '{kind}'. Use one of: {', '.join(STORE_KINDS)}")
