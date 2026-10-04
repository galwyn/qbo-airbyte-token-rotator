import base64
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

from qbo_airbyte_token_rotator import rotator

ENV = {
    "GCP_PROJECT_ID": "example-project",
    "AIRBYTE_SOURCE_ID": "00000000-0000-0000-0000-000000000001",
    "AIRBYTE_CLIENT_ID": "00000000-0000-0000-0000-000000000002",
    "QB_CLIENT_ID": "example-qb-client-id",
    "REALM_ID": "0000000000",
    "REFRESH_TOKEN_SECRET_NAME": "example-refresh-token",
    "AIRBYTE_CLIENT_SECRET": "mock-airbyte-secret",
    "QB_CLIENT_SECRET": "mock-qb-secret",
}


def _response(status_code=200, json_body=None, text=""):
    r = MagicMock()
    r.status_code = status_code
    r.json.return_value = json_body or {}
    r.text = text
    return r


def _run(env, argv=()):
    with patch.dict(os.environ, env, clear=True), patch.object(sys, "argv", ["qbo-airbyte-token-rotator", *argv]):
        return rotator.main()


def test_package_imports():
    import qbo_airbyte_token_rotator

    assert callable(qbo_airbyte_token_rotator.main)


@patch("qbo_airbyte_token_rotator.rotator.secretmanager.SecretManagerServiceClient")
@patch("qbo_airbyte_token_rotator.rotator.requests")
def test_full_rotation_from_env_vars(mock_requests, mock_sm_client):
    sm = mock_sm_client.return_value
    secret = MagicMock()
    secret.payload.data.decode.return_value = "mock-refresh-token"
    sm.access_secret_version.return_value = secret

    mock_requests.post.side_effect = [
        _response(json_body={"access_token": "mock-airbyte-access-token"}),
        _response(json_body={
            "access_token": "new-qb-access-token",
            "refresh_token": "new-qb-refresh-token",
            "x_refresh_token_expires_in": 3600,
        }),
    ]
    mock_requests.get.return_value = _response(json_body={
        "configuration": {"refresh_token": "mock-refresh-token", "client_id": "old-id", "start_date": "2020-01-01"}
    })
    mock_requests.patch.return_value = _response()

    assert _run(ENV) == 0

    sm.access_secret_version.assert_called_with(
        request={"name": "projects/example-project/secrets/example-refresh-token/versions/latest"}
    )
    mock_requests.post.assert_any_call(
        "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer",
        auth=("example-qb-client-id", "mock-qb-secret"),
        data={"grant_type": "refresh_token", "refresh_token": "mock-refresh-token"},
    )
    sm.add_secret_version.assert_called_once_with(
        request={
            "parent": "projects/example-project/secrets/example-refresh-token",
            "payload": {"data": b"new-qb-refresh-token"},
        }
    )
    patched = mock_requests.patch.call_args.kwargs["json"]["configuration"]
    assert patched["refresh_token"] == "new-qb-refresh-token"
    assert patched["access_token"] == "new-qb-access-token"
    assert patched["realm_id"] == "0000000000"
    assert patched["start_date"] == "2020-01-01"


@patch("qbo_airbyte_token_rotator.rotator.secretmanager.SecretManagerServiceClient")
@patch("qbo_airbyte_token_rotator.rotator.requests")
def test_identical_refresh_token_is_not_rewritten(mock_requests, mock_sm_client):
    sm = mock_sm_client.return_value
    secret = MagicMock()
    secret.payload.data.decode.return_value = "same-token"
    sm.access_secret_version.return_value = secret
    mock_requests.post.side_effect = [
        _response(json_body={"access_token": "a"}),
        _response(json_body={"access_token": "qb-a", "refresh_token": "same-token"}),
    ]
    mock_requests.get.return_value = _response(json_body={"configuration": {}})
    mock_requests.patch.return_value = _response()

    assert _run(ENV) == 0
    sm.add_secret_version.assert_not_called()


@patch("qbo_airbyte_token_rotator.rotator.secretmanager.SecretManagerServiceClient")
def test_client_secrets_are_read_from_named_secrets(mock_sm_client):
    values = {
        "projects/example-project/secrets/ab-secret/versions/latest": "ab-value",
        "projects/example-project/secrets/qb-secret/versions/latest": "qb-value",
    }

    def access(request):
        resp = MagicMock()
        resp.payload.data.decode.return_value = values[request["name"]]
        return resp

    mock_sm_client.return_value.access_secret_version.side_effect = access
    env = {k: v for k, v in ENV.items() if k not in ("AIRBYTE_CLIENT_SECRET", "QB_CLIENT_SECRET")}
    env.update(AIRBYTE_CLIENT_SECRET_NAME="ab-secret", QB_CLIENT_SECRET_NAME="qb-secret")

    assert _run(env, ["--dry_run"]) == 0


def test_missing_client_secret_without_secret_name_is_an_error():
    env = {k: v for k, v in ENV.items() if k != "QB_CLIENT_SECRET"}
    with pytest.raises(SystemExit) as exc:
        _run(env)
    assert exc.value.code == 2


@patch("qbo_airbyte_token_rotator.rotator.requests")
def test_failed_refresh_never_logs_the_client_secret(mock_requests, capsys):
    basic = "Basic " + base64.b64encode(b"example-qb-client-id:mock-qb-secret").decode()
    intuit = _response(status_code=400, text='{"error":"invalid_grant"}')
    intuit.headers = {"intuit_tid": "tid-123"}
    intuit.request.headers = {"Authorization": basic, "Content-Type": "application/x-www-form-urlencoded"}
    mock_requests.post.side_effect = [_response(json_body={"access_token": "a"}), intuit]

    env = {k: v for k, v in ENV.items() if k != "REFRESH_TOKEN_SECRET_NAME"}
    with pytest.raises(SystemExit) as exc:
        _run(env, ["--refresh_token", "mock-refresh-token"])
    assert exc.value.code == 1

    out = capsys.readouterr()
    logged = out.out + out.err
    assert "Intuit Rejected Refresh" in logged
    assert "invalid_grant" in logged
    assert "tid-123" in logged
    assert "<redacted>" in logged
    assert basic not in logged
    assert base64.b64encode(b"example-qb-client-id:mock-qb-secret").decode() not in logged
    assert "mock-qb-secret" not in logged
