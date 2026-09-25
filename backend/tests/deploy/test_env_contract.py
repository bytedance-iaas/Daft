"""The deployment's side of the Daemon's configuration (D53): design doc 09 §2.1 lists the
environment the dataverse chart (rerun repository) gives the Daemon container. The chart is
written against that table, so the code has to keep accepting it: every variable is read by
something, the literal values are ones the Daemon takes, secrets only come by reference.
Renaming or dropping a setting fails here first - then change the table and the chart."""
from __future__ import annotations

import base64
import pathlib

from curation import tos_store
from daemon.orchestr.config import OrchestratorConfig
from daemon.settings import SECRET_ENVS, Settings, normalize_base_path

from .support import cli_strings, daemon_strings, deployment_env


def environ(tmp_path: pathlib.Path) -> dict[str, str]:
    """The table's literal values, a test master key, and real files where a path must exist."""
    env = {name: row.literal for name, row in deployment_env().items()
           if row.literal is not None and "<" not in row.literal}
    env["CURATOR_MASTER_KEY"] = base64.b64encode(bytes(range(1, 33))).decode()
    site = tmp_path / "site.yaml"
    site.write_text("concurrency: {cpu: 8, cpuMax: 16, vlmParallelism: 64, vlmParallelismMax: 128}\n",
                    encoding="utf-8")
    env["CURATION_CONFIG"] = str(site)
    return env


def test_the_table_covers_what_the_daemon_needs():
    rows = deployment_env()
    for name in ("CURATOR_BASE_PATH", "CURATOR_DATA_DIR", "CURATOR_SCRATCH_DIR", "CURATION_CONFIG",
                 "CURATOR_AUTH_MODE", "CURATOR_HTPASSWD_FILE", "CURATOR_MASTER_KEY", "TOS_ENDPOINT"):
        assert name in rows, f"design doc 09 §2.1 lost {name}"


def test_every_variable_is_read_by_the_code():
    for name in deployment_env():
        if name.startswith("CURATOR_"):
            assert name in daemon_strings(), f"{name}: the Daemon does not read it"
        elif name.startswith("CURATION_"):
            assert name in cli_strings(), f"{name}: the CLI does not read it"
        else:
            assert name in daemon_strings() | cli_strings(), f"{name}: nothing reads it"


def test_secrets_only_come_by_reference():
    rows = deployment_env()
    for name, row in rows.items():
        if name in SECRET_ENVS:
            assert row.secret and row.literal is None, f"{name} must be a secretKeyRef"
        # every other row states its value: a literal, or a literal with a <placeholder>
        assert row.secret or row.literal is not None, f"{name}: no value in the table"
    assert rows["CURATOR_MASTER_KEY"].secret


def test_the_daemon_accepts_the_deployment_environment(tmp_path, daemon_env_cleared):
    rows = deployment_env()
    env = environ(tmp_path)
    s = Settings.from_env(env)
    assert s.base_path == normalize_base_path(rows["CURATOR_BASE_PATH"].literal) == "/curation"
    assert s.data_dir == pathlib.Path(rows["CURATOR_DATA_DIR"].literal)
    assert s.scratch_dir == pathlib.Path(rows["CURATOR_SCRATCH_DIR"].literal)
    assert s.auth.mode == "htpasswd"
    assert s.auth.htpasswd_file == rows["CURATOR_HTPASSWD_FILE"].literal
    assert (s.master_key.version, s.master_key.next_key) == (1, None)
    assert OrchestratorConfig.from_env(env).max_running == 1   # the chart leaves it to the Daemon
    # the one other mode the chart sets (web.basicAuth off)
    assert "`none`" in rows["CURATOR_AUTH_MODE"].source
    assert Settings.from_env({**env, "CURATOR_AUTH_MODE": "none"}).auth.mode == "none"


def test_the_derived_tos_endpoint_keeps_in_region_traffic_internal():
    endpoint = deployment_env()["TOS_ENDPOINT"].literal.replace("<region>", "cn-beijing")
    assert tos_store.region_from_endpoint(endpoint) == "cn-beijing"
    assert tos_store.endpoint_for_region("cn-beijing", endpoint) == "https://tos-cn-beijing.ivolces.com"
    assert tos_store.endpoint_for_region("cn-shanghai", endpoint) == "https://tos-cn-shanghai.volces.com"
