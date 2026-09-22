"""The CLI subprocess environment: each role gets its own key, nothing inherited leaks in.

The last test reads the environment back with the CLI's own code (``curation.cli.creds``), so
the variable names cannot drift apart from W3's side.
"""
from __future__ import annotations

import pytest

from curation.cli import creds as cli_creds
from daemon.secrets import Unavailable, build_env, cli_environment
from daemon.secrets.tos import TosKey

from .conftest import API, JSON, add_access_key, add_backend, runtime, seed_task, service
from .fakes import AK, AK2, API_KEY, SK, SK2

INHERITED = {
    "PATH": "/usr/bin", "HOME": "/home/curator", "PYTHONPATH": "/app/backend",
    "TOS_ACCESS_KEY": "AKLTinheritedDeploymentKey", "TOS_SECRET_KEY": "inherited-deploy-secret",
    "TOS_SESSION_TOKEN": "inherited-token", "TOS_REGION": "cn-shanghai",
    "TOS_ENDPOINT": "https://tos-cn-shanghai.ivolces.com",
    "CURATION_INPUT_TOS_ACCESS_KEY": "stale-input", "CURATION_INPUT_TOS_SECRET_KEY": "stale-in-sk",
    "CURATION_OUTPUT_TOS_SESSION_TOKEN": "stale-token", "ARK_API_KEY": "inherited-ark-key",
    "CURATION_VLM_API_KEY_ENV": "SOME_OTHER_VAR", "CURATOR_MASTER_KEY": "should-be-gone-anyway",
    "CURATOR_AUTH_PASSWORD": "admin-password",
}
INHERITED_SECRETS = ("AKLTinheritedDeploymentKey", "inherited-deploy-secret", "inherited-token",
                     "stale-input", "stale-in-sk", "stale-token", "inherited-ark-key",
                     "should-be-gone-anyway", "admin-password")


def _key(ak, sk, token=None, name="k") -> TosKey:
    return TosKey(ak, sk, token, "cred_x", name, "cn-beijing")


def test_build_env_drops_everything_inherited_and_sets_exactly_the_given_keys():
    env = build_env(INHERITED, input_key=_key(AK, SK), output_key=_key(AK2, SK2, "tok-2"),
                    vlm_api_key=API_KEY)
    assert env["PATH"] == "/usr/bin" and env["PYTHONPATH"] == "/app/backend"
    assert (env["CURATION_INPUT_TOS_ACCESS_KEY"], env["CURATION_INPUT_TOS_SECRET_KEY"]) == (AK, SK)
    assert "CURATION_INPUT_TOS_SESSION_TOKEN" not in env
    assert (env["CURATION_OUTPUT_TOS_ACCESS_KEY"], env["CURATION_OUTPUT_TOS_SECRET_KEY"],
            env["CURATION_OUTPUT_TOS_SESSION_TOKEN"]) == (AK2, SK2, "tok-2")
    assert env["ARK_API_KEY"] == API_KEY
    assert not any(k.startswith("TOS_") for k in env)           # no endpoint given: none at all
    assert "CURATION_VLM_API_KEY_ENV" not in env and "CURATOR_MASTER_KEY" not in env
    for value in INHERITED_SECRETS:
        assert value not in env.values()


def test_build_env_with_nothing_to_give():
    env = build_env(INHERITED)
    assert not any(k.startswith(("TOS_", "CURATION_INPUT_TOS", "CURATION_OUTPUT_TOS")) for k in env)
    assert "ARK_API_KEY" not in env
    env = build_env(INHERITED, tos_endpoint="https://tos-cn-beijing.ivolces.com")
    assert env["TOS_ENDPOINT"] == "https://tos-cn-beijing.ivolces.com"   # set on purpose


def test_a_half_empty_key_is_refused():
    with pytest.raises(ValueError):
        build_env({}, input_key=_key(AK, ""))


def _task(c, stub, *, selected=("timestamp_check", "task_success"), source="tos"):
    rt = runtime(c)
    inp = add_access_key(c, name="in", ak=AK, sk=SK)
    out = add_access_key(c, name="out", ak=AK2, sk=SK2)
    backend = add_backend(c, stub, kind="custom", api_key=API_KEY)
    model = c.post(f"{API}/vlm-backends/{backend['id']}/models", json={"model_name": "m1"},
                   headers=JSON).json()
    task = seed_task(rt.repo, selected=selected, input_cred_id=inp["id"],
                     output_cred_id=out["id"], vlm_model_id=model["id"])
    if source != "tos":
        rt.repo.update_task_fields(task.id, if_updated_at=None, input_source=source,
                                   input_uri="tos://public-mirror/lerobot/pusht",
                                   input_cred_id=None, input_region=None)
    return rt.repo.get_task(task.id)


def test_a_tasks_environment_as_the_cli_reads_it(secret_client, vlm_stub):
    c = secret_client()
    task = _task(c, vlm_stub)
    cli = cli_environment(service(c), task, base=INHERITED)
    assert (cli.input_region, cli.output_region, cli.vlm_api_key_env) == \
        ("cn-beijing", "cn-beijing", "ARK_API_KEY")
    assert set(cli.secret_names) == {"CURATION_INPUT_TOS_ACCESS_KEY",
                                     "CURATION_INPUT_TOS_SECRET_KEY",
                                     "CURATION_OUTPUT_TOS_ACCESS_KEY",
                                     "CURATION_OUTPUT_TOS_SECRET_KEY", "ARK_API_KEY"}
    for secret in (AK, SK, AK2, SK2, API_KEY):
        assert secret not in repr(cli)
    # W3's reader: two independent roles, never the inherited deployment key
    got_in = cli_creds.tos_credentials("input", cli.env)
    got_out = cli_creds.tos_credentials("output", cli.env)
    assert (got_in.access_key, got_in.secret_key) == (AK, SK)
    assert (got_out.access_key, got_out.secret_key) == (AK2, SK2)
    assert cli.env[cli.vlm_api_key_env] == API_KEY


def test_no_vlm_key_without_a_vlm_module_and_no_input_key_for_the_public_bucket(
        secret_client, vlm_stub):
    c = secret_client()
    task = _task(c, vlm_stub, selected=("timestamp_check",), source="public")
    cli = cli_environment(service(c), task, base=INHERITED)
    assert "ARK_API_KEY" not in cli.env and cli.vlm_api_key_env is None
    # the public input reads anonymously: the CLI must not fall back to an inherited key
    assert cli_creds.tos_credentials("input", cli.env) is None
    assert cli_creds.tos_credentials("output", cli.env).access_key == AK2
    only_output = cli_environment(service(c), task, need_input=False, need_vlm=False,
                                  base=INHERITED)
    assert "CURATION_INPUT_TOS_ACCESS_KEY" not in only_output.env


def test_the_daemons_internal_endpoint_is_passed_on_deliberately(secret_client, vlm_stub,
                                                                  monkeypatch):
    monkeypatch.setenv("TOS_ENDPOINT", "tos-cn-beijing.ivolces.com")
    c = secret_client()
    task = _task(c, vlm_stub)
    cli = cli_environment(service(c), task, base=INHERITED)
    assert cli.env["TOS_ENDPOINT"] == "https://tos-cn-beijing.ivolces.com"
    assert "TOS_REGION" not in cli.env and "TOS_ACCESS_KEY" not in cli.env


def test_a_deleted_key_or_backend_stops_the_command(secret_client, vlm_stub):
    c = secret_client()
    task = _task(c, vlm_stub)
    rt = runtime(c)
    rt.repo.update_task_fields(task.id, if_updated_at=None, output_cred_id=None)
    with pytest.raises(Unavailable) as err:
        cli_environment(service(c), rt.repo.get_task(task.id), base={})
    assert err.value.code == "credential_missing" and "交付目录" in err.value.message_zh
    rt.repo.freeze_task_inputs(task.id, run_id="r", preflight={}, source_fingerprint={},
                               vlm_snapshot={"backend_id": "vb_gone", "backend": "b",
                                             "model": "m1"})
    with pytest.raises(Unavailable) as err:
        cli_environment(service(c), rt.repo.get_task(task.id), need_output=False, base={})
    assert err.value.code == "backend_missing"
