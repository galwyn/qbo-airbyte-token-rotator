import os
import sys
import types
from unittest.mock import MagicMock, patch

import pytest

from qbo_airbyte_token_rotator import rotator, stores

ENV = {
    "AIRBYTE_SOURCE_ID": "00000000-0000-0000-0000-000000000001",
    "AIRBYTE_CLIENT_ID": "00000000-0000-0000-0000-000000000002",
    "QB_CLIENT_ID": "example-qb-client-id",
    "REALM_ID": "0000000000",
    "REFRESH_TOKEN_SECRET_NAME": "example-refresh-token",
    "AIRBYTE_CLIENT_SECRET_NAME": "example-airbyte-secret",
    "QB_CLIENT_SECRET_NAME": "example-qb-secret",
}


def _response(json_body=None, status_code=200):
    r = MagicMock()
    r.status_code = status_code
    r.json.return_value = json_body or {}
    return r


def _mock_happy_path(mock_requests, new_refresh_token="new-refresh-token"):
    mock_requests.post.side_effect = [
        _response({"access_token": "airbyte-access"}),
        _response({"access_token": "qb-access", "refresh_token": new_refresh_token}),
    ]
    mock_requests.get.return_value = _response({"configuration": {}})
    mock_requests.patch.return_value = _response()


@pytest.fixture
def fake_boto3():
    client = MagicMock()
    values = {
        "example-refresh-token": "old-refresh-token",
        "example-airbyte-secret": "airbyte-secret-value",
        "example-qb-secret": "qb-secret-value",
    }
    client.get_secret_value.side_effect = lambda SecretId: {"SecretString": values[SecretId]}
    module = types.ModuleType("boto3")
    module.client = MagicMock(return_value=client)
    with patch.dict(sys.modules, {"boto3": module}):
        yield module, client


@pytest.fixture
def fake_azure():
    client = MagicMock()
    values = {
        "example-refresh-token": "old-refresh-token",
        "example-airbyte-secret": "airbyte-secret-value",
        "example-qb-secret": "qb-secret-value",
    }
    client.get_secret.side_effect = lambda name: types.SimpleNamespace(value=values[name])
    identity = types.ModuleType("azure.identity")
    identity.DefaultAzureCredential = MagicMock(return_value="credential")
    secrets = types.ModuleType("azure.keyvault.secrets")
    secrets.SecretClient = MagicMock(return_value=client)
    modules = {
        "azure": types.ModuleType("azure"),
        "azure.identity": identity,
        "azure.keyvault": types.ModuleType("azure.keyvault"),
        "azure.keyvault.secrets": secrets,
    }
    with patch.dict(sys.modules, modules):
        yield secrets, client


def _run(env, argv=()):
    with patch.dict(os.environ, env, clear=True), patch.object(sys, "argv", ["qbo-airbyte-token-rotator", *argv]):
        return rotator.main()


@patch("qbo_airbyte_token_rotator.rotator.requests")
def test_aws_rotation_reads_and_writes_secrets_manager(mock_requests, fake_boto3):
    boto3, client = fake_boto3
    _mock_happy_path(mock_requests)

    assert _run({**ENV, "TOKEN_STORE": "aws", "AWS_REGION": "eu-west-1"}) == 0

    boto3.client.assert_called_once_with("secretsmanager", region_name="eu-west-1")
    mock_requests.post.assert_any_call(
        "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer",
        auth=("example-qb-client-id", "qb-secret-value"),
        data={"grant_type": "refresh_token", "refresh_token": "old-refresh-token"},
    )
    client.put_secret_value.assert_called_once_with(SecretId="example-refresh-token", SecretString="new-refresh-token")


@patch("qbo_airbyte_token_rotator.rotator.requests")
def test_azure_rotation_reads_and_writes_key_vault(mock_requests, fake_azure):
    secrets, client = fake_azure
    _mock_happy_path(mock_requests)

    env = {**ENV, "TOKEN_STORE": "azure", "AZURE_KEY_VAULT_URL": "https://example.vault.azure.net"}
    assert _run(env) == 0

    secrets.SecretClient.assert_called_once_with(vault_url="https://example.vault.azure.net", credential="credential")
    client.set_secret.assert_called_once_with("example-refresh-token", "new-refresh-token")


@patch("qbo_airbyte_token_rotator.rotator.requests")
def test_unchanged_refresh_token_is_not_rewritten_on_aws(mock_requests, fake_boto3):
    _, client = fake_boto3
    _mock_happy_path(mock_requests, new_refresh_token="old-refresh-token")

    assert _run({**ENV, "TOKEN_STORE": "aws"}) == 0
    client.put_secret_value.assert_not_called()


def test_azure_without_vault_url_is_an_error(fake_azure):
    with pytest.raises(SystemExit) as exc:
        _run({**ENV, "TOKEN_STORE": "azure"})
    assert exc.value.code == 2


def test_missing_sdk_names_the_extra_to_install(capsys):
    with patch.dict(sys.modules, {"boto3": None}):
        with pytest.raises(SystemExit) as exc:
            _run({**ENV, "TOKEN_STORE": "aws"})
    assert exc.value.code == 2
    assert "qbo-airbyte-token-rotator[aws]" in capsys.readouterr().err


def test_unknown_store_is_rejected():
    with pytest.raises(ValueError):
        stores.make_store("dropbox")


@patch("google.cloud.secretmanager.SecretManagerServiceClient")
def test_gcp_store_uses_the_same_requests_as_before(mock_client):
    sm = mock_client.return_value
    sm.access_secret_version.return_value.payload.data.decode.return_value = "value"
    store = stores.GcpSecretManagerStore("example-project")

    assert store.read_secret("name") == "value"
    store.write_secret("name", "new")

    sm.access_secret_version.assert_called_once_with(
        request={"name": "projects/example-project/secrets/name/versions/latest"}
    )
    sm.add_secret_version.assert_called_once_with(
        request={"parent": "projects/example-project/secrets/name", "payload": {"data": b"new"}}
    )
