"""The Helm chart (deploy/charts/curator), rendered without a cluster (design doc 09 §2).

The manifests are held against what the code really does rather than against a copy of the
values: the Daemon's settings (daemon.settings), its probe routes and their exemption from
authentication (daemon.routes.system, daemon.auth), master-key loading (daemon.masterkey),
the reasoning-effort table loader, the pipeline config loader and the planner's site config.
Needs the helm binary; skipped without it.
"""
from __future__ import annotations

import base64
import copy
import dataclasses
import re

import pytest
import yaml

from curation.pipeline.config import DEFAULT_CONFIG_PATH, load_config
from curation.planner.limits import SiteConfig
from daemon import app as daemon_app
from daemon import masterkey
from daemon.auth import AuthConfig, NoAuthProvider, build_provider
from daemon.routes import system
from daemon.secrets import effort
from daemon.settings import SECRET_ENVS, ConfigError, Settings, normalize_base_path

from .support import (CHART, chart_values, claim_templates, cli_strings, container,
                      daemon_environ, daemon_strings, dockerfile, dummy_master_key, env_entries,
                      kinds, mounts, only, plain_env, pod_volumes, render, render_error, run_helm)

#: The production mount prefix behind APIG (09 §2.4).
PROD = {"server": {"basePath": "/curation"}}

#: Every optional part switched on.
FULL = {
    "image": {"repository": "cr.example.com/kit/curator", "tag": "2.0.0-rc1"},
    "imagePullSecrets": [{"name": "cr-pull"}],
    "server": {"basePath": "curation/", "publicBaseUrl": "https://kit.example.com",
               "tzOffset": "-05:30", "sseHeartbeatSeconds": 10,
               "tosEndpoint": "tos-cn-beijing.ivolces.com"},
    "logging": {"format": "text", "level": "debug"},
    "auth": {"mode": "basic", "username": "demo", "existingPasswordSecret": "curator-login"},
    "masterKey": {"existingSecret": "kit-master-key"},
    "persistence": {"data": {"storageClass": "ebs-ssd", "size": "50Gi"},
                    "scratch": {"type": "pvc", "size": "80Gi", "storageClass": "ebs-ssd"}},
    "publicDatasets": {"bucket": "hf-cache", "region": "cn-beijing"},
    "localDataRoot": "/mnt/local",
    "reasoningEffortTable": [{"prefix": "glm-4.5", "levels": ["low", "medium", "high"],
                              "default": "medium"}],
    "pipelineConfigOverride": {"verdict": {"soft_threshold": 0.7}},
    "vlm": {"merge": {"enabled": False}, "gates": {"probe": 48}},
    "ingress": {"enabled": True, "className": "nginx", "hosts": [{"host": "kit.example.com"}],
                "tls": [{"secretName": "kit-tls", "hosts": ["kit.example.com"]}]},
    "extraEnv": [{"name": "CURATION_EPHEMERAL_LIMIT_BYTES", "value": "1000000000"}],
}

VALUE_SETS = {"defaults": {}, "prod": PROD, "full": FULL,
              "maintenance": {"maintenance": {"enabled": True}}}


def pod_spec(docs: list[dict]) -> dict:
    return only(docs, "StatefulSet")["spec"]["template"]["spec"]


# ---------------------------------------------------------------------------
# helm itself
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("values", [{}, FULL], ids=["defaults", "full"])
def test_helm_lint(values):
    proc = run_helm("lint", str(CHART), values=values)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "0 chart(s) failed" in proc.stdout


def test_notes_render_and_warn_about_a_generated_master_key():
    def notes(values):
        proc = run_helm("install", "curator", str(CHART), "-n", "curator", "--dry-run=client",
                        values=values)
        if proc.returncode and "unknown flag" in proc.stderr:
            pytest.skip("this helm has no --dry-run=client")
        assert proc.returncode == 0, proc.stderr
        return proc.stdout.split("NOTES:", 1)[1]

    generated = notes(PROD)
    assert "port-forward svc/curator 8080:8080" in generated
    assert "http://127.0.0.1:8080/curation/" in generated
    assert "curator-master-key" in generated and "只适合试用" in generated
    brought = notes({**PROD, "masterKey": {"existingSecret": "kit-master-key"}})
    assert "只适合试用" not in brought


def test_rendering_is_deterministic_apart_from_the_generated_key():
    def without_key(docs):
        docs = copy.deepcopy(docs)
        for d in docs:
            if d["kind"] == "Secret":
                d.pop("data")
        return docs

    assert without_key(render()) == without_key(render())


# ---------------------------------------------------------------------------
# one replica on a block volume (D1, D2)
# ---------------------------------------------------------------------------

def test_one_replica_statefulset_and_its_services():
    docs = render()
    assert kinds(docs) == ["ConfigMap", "Secret", "Service", "Service", "StatefulSet"]
    sts = only(docs, "StatefulSet")
    assert sts["spec"]["replicas"] == 1
    # stop the old pod, then start the new one: never two Daemons on the volume
    assert sts["spec"]["podManagementPolicy"] == "OrderedReady"
    assert sts["spec"]["updateStrategy"]["type"] == "RollingUpdate"
    headless = only(docs, "Service", sts["spec"]["serviceName"])
    assert headless["spec"]["clusterIP"] == "None"
    main = only(docs, "Service", sts["metadata"]["name"])
    assert main["spec"]["ports"] == [{"name": "http", "port": 8080, "targetPort": "http",
                                      "protocol": "TCP"}]


@pytest.mark.parametrize("n", [0, 2, 3])
def test_other_replica_counts_are_refused(n):
    assert "replicaCount" in render_error({"replicaCount": n})


def test_data_volume_is_a_read_write_once_claim_at_the_data_dir(daemon_env_cleared):
    docs = render()
    spec = claim_templates(docs)["data"]["spec"]
    assert spec["accessModes"] == ["ReadWriteOnce"]
    assert spec["resources"]["requests"]["storage"] == "100Gi"
    # an empty storageClassName would mean "no class" (static volumes only), not the default
    assert "storageClassName" not in spec
    assert "data" not in pod_volumes(docs)          # it comes from the claim template
    settings = Settings.from_env(daemon_environ(docs))
    assert str(settings.data_dir) == mounts(docs)["data"]["mountPath"] == "/data"
    # the database and the task work directories are on that volume
    assert settings.db_path.parent == settings.data_dir
    assert settings.work_dir.parent == settings.data_dir


def test_storage_class_and_an_existing_claim():
    docs = render({"persistence": {"data": {"storageClass": "ebs-ssd"}}})
    assert claim_templates(docs)["data"]["spec"]["storageClassName"] == "ebs-ssd"
    docs = render({"persistence": {"data": {"existingClaim": "restored-data"}}})
    assert claim_templates(docs) == {}
    assert "volumeClaimTemplates" not in only(docs, "StatefulSet")["spec"]
    assert pod_volumes(docs)["data"] == {"name": "data",
                                         "persistentVolumeClaim": {"claimName": "restored-data"}}


def test_scratch_volume(daemon_env_cleared):
    docs = render()
    assert pod_volumes(docs)["scratch"]["emptyDir"] == {"sizeLimit": "200Gi"}
    env = plain_env(docs)
    path = mounts(docs)["scratch"]["mountPath"]
    # the Daemon's temp space and the export encoders' scratch (CLI) are the same volume
    assert env["CURATOR_SCRATCH_DIR"] == env["CURATION_EXPORT_SCRATCH"] == path
    assert str(Settings.from_env(daemon_environ(docs)).scratch_dir) == path
    assert pod_volumes(render({"persistence": {"scratch": {"size": ""}}}))["scratch"]["emptyDir"] == {}
    docs = render({"persistence": {"scratch": {"type": "pvc", "storageClass": "ebs-ssd"}}})
    assert "scratch" not in pod_volumes(docs)
    spec = claim_templates(docs)["scratch"]["spec"]
    assert (spec["storageClassName"], spec["resources"]["requests"]["storage"]) == ("ebs-ssd", "200Gi")
    assert set(claim_templates(docs)) == {"data", "scratch"}


def test_selector_labels_are_stable_and_shared():
    docs = render()
    sts = only(docs, "StatefulSet")
    selector = sts["spec"]["selector"]["matchLabels"]
    assert selector.items() <= sts["spec"]["template"]["metadata"]["labels"].items()
    # the selector and the claim templates are immutable: nothing that changes per release
    assert not {"helm.sh/chart", "app.kubernetes.io/version"} & set(selector)
    assert only(docs, "Service", sts["metadata"]["name"])["spec"]["selector"] == selector
    for template in claim_templates(render({"persistence": {"scratch": {"type": "pvc"}}})).values():
        assert template["metadata"]["labels"] == selector


# ---------------------------------------------------------------------------
# probes (09 §2.2)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("base", ["", "/curation", "curation/"])
def test_probes_hit_the_daemons_auth_exempt_paths(base):
    docs = render({"server": {"basePath": base}})
    c = container(docs)
    exempt = system.probe_paths(normalize_base_path(base))   # what AuthMiddleware lets through
    ports = {p["name"]: p["containerPort"] for p in c["ports"]}
    for probe, path in (("startupProbe", "/healthz"), ("livenessProbe", "/healthz"),
                        ("readinessProbe", "/readyz")):
        get = c[probe]["httpGet"]
        assert get["path"] == path and path in system.PROBES and path in exempt
        assert get["port"] == "http" and "http" in ports
    # the gateway's health checks go through the prefix; those paths are exempt too
    if base:
        assert {"/curation/healthz", "/curation/readyz"} <= exempt


def test_probe_timings_follow_the_design():
    c = container(render())
    assert (c["livenessProbe"]["periodSeconds"], c["livenessProbe"]["failureThreshold"]) == (10, 6)
    assert c["readinessProbe"]["periodSeconds"] == 5
    startup = c["startupProbe"]
    # five minutes for schema migration and startup reconciliation
    assert startup["periodSeconds"] * startup["failureThreshold"] >= 300
    # /readyz waits up to PROBE_TIMEOUT_S for its write through the single writer
    assert c["readinessProbe"]["timeoutSeconds"] > daemon_app.PROBE_TIMEOUT_S


def test_container_port_is_the_daemons_port(daemon_env_cleared):
    for values in ({}, {"server": {"port": 9090}}):
        docs = render(values)
        [port] = container(docs)["ports"]
        assert Settings.from_env(daemon_environ(docs)).port == port["containerPort"]


# ---------------------------------------------------------------------------
# graceful stop (09 §2.3)
# ---------------------------------------------------------------------------

def test_grace_period_and_prestop():
    docs = render()
    assert pod_spec(docs)["terminationGracePeriodSeconds"] == 120
    assert container(docs)["lifecycle"]["preStop"] == {"exec": {"command": ["sleep", "5"]}}


@pytest.mark.parametrize("grace,pre,ok", [(120, 20, True), (100, 0, True), (120, 21, False),
                                          (60, 0, False), (30, 5, False)])
def test_a_grace_period_too_short_for_the_system_pause_is_refused(grace, pre, ok):
    values = {"terminationGracePeriodSeconds": grace, "preStopSleepSeconds": pre}
    if ok:
        assert pod_spec(render(values))["terminationGracePeriodSeconds"] == grace
    else:
        assert "terminationGracePeriodSeconds" in render_error(values)


def test_no_prestop_when_it_is_zero():
    assert "lifecycle" not in container(render({"preStopSleepSeconds": 0}))


def test_the_images_entry_point_stays_in_charge():
    c = container(render())
    # no command override: tini stays PID 1 and forwards SIGTERM to the Daemon
    assert "command" not in c
    assert c["args"] == ["curator-daemon"]


def test_maintenance_mode_runs_no_daemon_on_the_same_volumes():
    normal, maint = render(), render({"maintenance": {"enabled": True}})
    c = container(maint)
    assert c["args"] == ["sleep", "infinity"]
    assert not {"startupProbe", "livenessProbe", "readinessProbe", "lifecycle"} & set(c)
    assert mounts(maint) == mounts(normal)
    assert claim_templates(maint) == claim_templates(normal)
    # kubectl exec gets the same configuration (rotation, --check-config, restores)
    assert env_entries(maint) == env_entries(normal)


# ---------------------------------------------------------------------------
# environment and settings
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("values", VALUE_SETS.values(), ids=VALUE_SETS.keys())
def test_every_variable_the_chart_sets_is_read_by_the_code(values):
    extra = {e["name"] for e in values.get("extraEnv", [])}
    for name in set(env_entries(render(values))) - extra:
        if name.startswith("CURATOR_"):
            assert name in daemon_strings(), f"{name}: the Daemon does not read it"
        elif name.startswith("CURATION_"):
            assert name in cli_strings(), f"{name}: the CLI does not read it"
        else:
            assert name in daemon_strings() | cli_strings(), f"{name}: nothing reads it"


@pytest.mark.parametrize("values", [{}, PROD, FULL], ids=["defaults", "prod", "full"])
def test_the_daemon_accepts_the_rendered_environment(values, daemon_env_cleared):
    docs = render(values)
    env = daemon_environ(docs)
    s = Settings.from_env(env)
    server = values.get("server", {})
    assert s.base_path == normalize_base_path(server.get("basePath"))
    assert s.public_base_url == server.get("publicBaseUrl", "")
    assert s.tz_offset_minutes == {"-05:30": -330}.get(server.get("tzOffset"), 480)
    assert s.sse_heartbeat_s == float(server.get("sseHeartbeatSeconds", 15))
    assert (s.log_format, s.log_level) == (values.get("logging", {}).get("format", "json"),
                                          values.get("logging", {}).get("level", "INFO").upper())
    assert str(s.local_data_root or "") == values.get("localDataRoot", "")
    assert (s.master_key.version, s.master_key.next_key) == (1, None)
    mode = values.get("auth", {}).get("mode", "htpasswd")
    assert s.auth.mode == mode
    if mode == "htpasswd":
        [item] = pod_volumes(docs)["auth"]["secret"]["items"]
        assert s.auth.htpasswd_file == f"{mounts(docs)['auth']['mountPath']}/{item['path']}"
    else:
        assert (s.auth.user, s.auth.password) == ("demo", "not-a-real-secret")
    assert env.get("TOS_ENDPOINT", "") == server.get("tosEndpoint", "")


@pytest.mark.parametrize("values", [{}, FULL], ids=["htpasswd", "basic"])
def test_authentication_is_always_pinned_never_off(values):
    env = daemon_environ(render(values))
    assert env["CURATOR_AUTH_MODE"] in ("htpasswd", "basic")
    # whatever else goes wrong (no file, no account), the Daemon refuses rather than opens up
    assert not isinstance(build_provider(AuthConfig.from_env(env)), NoAuthProvider)


def test_htpasswd_table_is_mounted_where_the_daemon_reads_it():
    docs = render({"auth": {"htpasswdSecret": "viewer-users", "htpasswdKey": "users"}})
    volume = pod_volumes(docs)["auth"]["secret"]
    assert volume["secretName"] == "viewer-users"
    assert volume["items"] == [{"key": "users", "path": "htpasswd"}]
    mount = mounts(docs)["auth"]
    assert mount["readOnly"] is True
    assert plain_env(docs)["CURATOR_HTPASSWD_FILE"] == f"{mount['mountPath']}/htpasswd"
    assert "auth" not in pod_volumes(render(FULL))             # basic mode: nothing to mount


def test_service_links_are_off_because_they_would_break_curator_port(daemon_env_cleared):
    assert pod_spec(render())["enableServiceLinks"] is False
    # what Kubernetes injects for a Service named "curator" when links are on
    with pytest.raises(ConfigError):
        Settings.from_env({"CURATOR_MASTER_KEY": dummy_master_key(),
                           "CURATOR_PORT": "tcp://10.96.0.12:8080"})


@pytest.mark.parametrize("raw", ["", "curation", "/curation", "/curation/", "kit/curation"])
def test_base_path_is_normalized_like_the_daemon_does(raw):
    env = plain_env(render({"server": {"basePath": raw}}))
    assert env.get("CURATOR_BASE_PATH", "") == normalize_base_path(raw)


@pytest.mark.parametrize("raw", ["../x", "a b", "cur?x", "a//b", "/./x", "a%2f"])
def test_bad_base_paths_are_refused_before_the_daemon_would(raw):
    assert "server.basePath" in render_error({"server": {"basePath": raw}})
    with pytest.raises(ConfigError):
        normalize_base_path(raw)


@pytest.mark.parametrize("values,needle", [
    ({"server": {"maxRunningTasks": 2}}, "maxRunningTasks"),
    ({"backup": {"enabled": True}}, "backup.enabled"),
    ({"auth": {"mode": "none"}}, "auth.mode"),
    ({"auth": {"mode": "basic", "username": "demo"}}, "existingPasswordSecret"),
    ({"auth": {"htpasswdSecret": ""}}, "htpasswdSecret"),
    ({"masterKey": {"key": ""}}, "masterKey.key"),
    ({"persistence": {"scratch": {"type": "nas"}}}, "scratch.type"),
    ({"persistence": {"scratch": {"type": "pvc", "size": ""}}}, "scratch.size"),
    ({"logging": {"format": "xml"}}, "logging.format"),
    ({"server": {"publicBaseUrl": "kit.example.com"}}, "publicBaseUrl"),
], ids=lambda v: v if isinstance(v, str) else None)
def test_values_the_daemon_cannot_honour_are_refused(values, needle):
    assert needle in render_error(values)


# ---------------------------------------------------------------------------
# secrets (08 §2, §5)
# ---------------------------------------------------------------------------

def test_values_have_no_place_for_a_secret():
    values = chart_values()
    # names of Secrets and of their keys only; a plaintext password field would fail here
    assert set(values["auth"]) == {"mode", "htpasswdSecret", "htpasswdKey", "username",
                                   "existingPasswordSecret", "passwordKey"}
    assert set(values["masterKey"]) == {"existingSecret", "key", "nextKey", "versionKey"}
    for path in sorted(CHART.rglob("*")):
        if path.is_file():
            text = path.read_text(encoding="utf-8")
            assert not re.search(r"[A-Za-z0-9+/]{40,}={0,2}", text), f"{path}: a key-like blob"
            assert "PRIVATE KEY" not in text, path


@pytest.mark.parametrize("values", [{}, FULL], ids=["defaults", "full"])
def test_secret_variables_only_come_from_secret_references(values):
    docs = render(values)
    for name, entry in env_entries(docs).items():
        if name in SECRET_ENVS or name.startswith(masterkey.KEY_ENV):
            assert "value" not in entry, name
            assert set(entry["valueFrom"]) == {"secretKeyRef"}, name
    for doc in docs:
        if doc["kind"] == "ConfigMap":
            assert not re.search(r"(?i)password|secret_key|api_key", yaml.safe_dump(doc))


def test_the_generated_master_key_is_random_kept_and_valid():
    first, second = (only(render(), "Secret") for _ in range(2))
    assert first["metadata"]["name"] == "curator-master-key"
    # losing it makes every stored secret unreadable: helm uninstall leaves it alone
    assert first["metadata"]["annotations"]["helm.sh/resource-policy"] == "keep"
    keys = [base64.b64decode(s["data"]["masterKey"]).decode() for s in (first, second)]
    assert keys[0] != keys[1], "the key must be generated at install time, not baked into the chart"
    for key in keys:
        assert len(masterkey.load({masterkey.KEY_ENV: key}).key) == masterkey.KEY_BYTES


def test_nothing_secret_is_rendered_when_the_operator_brings_the_secrets():
    docs = render(FULL)
    assert "Secret" not in kinds(docs)
    refs = {n: e["valueFrom"]["secretKeyRef"] for n, e in env_entries(docs).items() if "valueFrom" in e}
    assert refs["CURATOR_AUTH_PASSWORD"] == {"name": "curator-login", "key": "password"}
    assert refs[masterkey.KEY_ENV] == {"name": "kit-master-key", "key": "masterKey"}


def test_key_version_and_next_key_live_next_to_the_key(daemon_env_cleared):
    """08 §2.1: one Secret edit switches key and version together; the rest is optional."""
    refs = {n: e["valueFrom"]["secretKeyRef"] for n, e in env_entries(render(PROD)).items()
            if n.startswith(masterkey.KEY_ENV)}
    assert refs == {
        masterkey.KEY_ENV: {"name": "curator-master-key", "key": "masterKey"},
        masterkey.NEXT_KEY_ENV: {"name": "curator-master-key", "key": "masterKeyNext",
                                 "optional": True},
        masterkey.VERSION_ENV: {"name": "curator-master-key", "key": "masterKeyVersion",
                                "optional": True},
    }
    old, new = dummy_master_key(), base64.b64encode(bytes(range(40, 72))).decode()
    # before a rotation: only the key -> version 1, nothing to rotate to
    assert (masterkey.load({masterkey.KEY_ENV: old}).version, masterkey.load(
        {masterkey.KEY_ENV: old}).next_key) == (1, None)
    # during: the next key is loaded too (values written with a trailing newline, as files are)
    during = masterkey.load({masterkey.KEY_ENV: old + "\n", masterkey.NEXT_KEY_ENV: new + "\n"})
    assert during.next_key is not None and during.version == 1
    # after: the new key as version 2
    assert masterkey.load({masterkey.KEY_ENV: new, masterkey.VERSION_ENV: "2\n"}).version == 2


# ---------------------------------------------------------------------------
# site config (09 §3), ingress, image, pod security
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("values", [{}, FULL], ids=["defaults", "full"])
def test_site_config_is_what_the_cli_and_the_planner_accept(values, tmp_path, daemon_env_cleared):
    docs = render(values)
    cm = only(docs, "ConfigMap")
    mount = mounts(docs)["site-config"]
    assert mount["readOnly"] is True
    assert pod_volumes(docs)["site-config"]["configMap"]["name"] == cm["metadata"]["name"]
    assert plain_env(docs)["CURATION_CONFIG"] == f"{mount['mountPath']}/site.yaml"
    site_file = tmp_path / "site.yaml"
    site_file.write_text(cm["data"]["site.yaml"], encoding="utf-8")
    merged = load_config(str(site_file))            # over default.yaml, validated like the CLI does
    site = yaml.safe_load(cm["data"]["site.yaml"])
    planner = SiteConfig.from_mapping(site)
    want = chart_values()
    for key in ("concurrency", "vlm"):
        want[key] = {**want[key], **values.get(key, {})}
    assert (planner.cpu_concurrency, planner.cpu_concurrency_max) == (want["concurrency"]["cpu"],
                                                                      want["concurrency"]["cpuMax"])
    assert (planner.vlm_parallelism, planner.vlm_parallelism_max) == (
        want["concurrency"]["vlmParallelism"], want["concurrency"]["vlmParallelismMax"])
    assert planner.merge_enabled is want["vlm"]["merge"]["enabled"]
    assert dict(planner.gate_overrides) == want["vlm"]["gates"]
    with open(DEFAULT_CONFIG_PATH, encoding="utf-8") as fh:
        factory = yaml.safe_load(fh)
    if values is FULL:
        assert merged["verdict"]["soft_threshold"] == 0.7
        public = site["public_datasets"]
        assert set(public) <= set(factory["public_datasets"])
        assert (public["bucket"], public["region"]) == ("hf-cache", "cn-beijing")
    else:
        assert "public_datasets" not in site          # no bucket: the source stays switched off
        assert merged["verdict"] == factory["verdict"]


def test_dedicated_values_win_over_the_pipeline_override():
    site = yaml.safe_load(only(render({"pipelineConfigOverride": {"concurrency": {"cpu": 99}}}),
                               "ConfigMap")["data"]["site.yaml"])
    assert site["concurrency"]["cpu"] == chart_values()["concurrency"]["cpu"]


def test_reasoning_effort_table_reaches_the_daemon(tmp_path):
    docs = render(FULL)
    path = plain_env(docs)[effort.TABLE_ENV]
    assert path == f"{mounts(docs)['site-config']['mountPath']}/reasoning-effort.json"
    local = tmp_path / "reasoning-effort.json"
    local.write_text(only(docs, "ConfigMap")["data"]["reasoning-effort.json"], encoding="utf-8")
    table = effort.load_table({effort.TABLE_ENV: str(local)})
    assert table.overrides, "the Daemon ignored the rendered table"
    assert table.levels_for("glm-4.5-air") == ("low", "medium", "high")
    assert effort.TABLE_ENV not in env_entries(render())      # no table, no variable


def test_site_config_changes_roll_the_pod():
    def checksum(values):
        return only(render(values), "StatefulSet")["spec"]["template"]["metadata"][
            "annotations"]["checksum/site-config"]

    base = checksum({})
    assert checksum({"vlm": {"merge": {"enabled": False}}}) != base
    assert checksum({"resources": {"requests": {"cpu": "4"}}}) == base


def test_no_ingress_by_default():
    assert "Ingress" not in kinds(render())


@pytest.mark.parametrize("base,path", [("/curation", "/curation"), ("", "/")])
def test_ingress_routes_the_prefix_without_rewriting_it(base, path):
    docs = render({"server": {"basePath": base},
                   "ingress": {"enabled": True, "hosts": [{"host": "kit.example.com"}]}})
    ingress = only(docs, "Ingress")
    [rule] = ingress["spec"]["rules"]
    assert rule["host"] == "kit.example.com"
    service = only(docs, "StatefulSet")["metadata"]["name"]
    assert rule["http"]["paths"] == [{"path": path, "pathType": "Prefix", "backend": {
        "service": {"name": service, "port": {"name": "http"}}}}]
    assert not any("rewrite" in k for k in (ingress["metadata"].get("annotations") or {}))


def test_ingress_without_hosts_has_one_rule_for_any_host():
    [rule] = only(render({"ingress": {"enabled": True}}), "Ingress")["spec"]["rules"]
    assert "host" not in rule


def test_image_reference_and_pull_secrets():
    app_version = yaml.safe_load((CHART / "Chart.yaml").read_text(encoding="utf-8"))["appVersion"]
    repo = {"repository": "cr.example.com/kit/curator"}
    assert container(render({"image": repo}))["image"] == f"cr.example.com/kit/curator:{app_version}"
    assert container(render({"image": {**repo, "tag": "2.0.1"}}))["image"].endswith(":2.0.1")
    digest = "sha256:" + "0" * 64
    assert container(render({"image": {**repo, "digest": digest}}))["image"] == \
        f"cr.example.com/kit/curator@{digest}"
    assert pod_spec(render(FULL))["imagePullSecrets"] == [{"name": "cr-pull"}]


def test_pod_runs_as_the_images_user_without_privileges():
    [user] = [i.args for i in dockerfile() if i.op == "USER"]
    uid, gid = (int(x) for x in user.split(":"))
    pod = pod_spec(render())
    sc = pod["securityContext"]
    assert (sc["runAsUser"], sc["runAsGroup"], sc["fsGroup"], sc["runAsNonRoot"]) == (uid, gid, gid, True)
    csc = container(render())["securityContext"]
    assert csc["allowPrivilegeEscalation"] is False and csc["capabilities"]["drop"] == ["ALL"]
    # the Daemon does not call the Kubernetes API
    assert pod["automountServiceAccountToken"] is False


def test_settings_defaults_the_chart_relies_on():
    """Values the chart leaves to the Daemon's defaults, pinned here so a change shows up."""
    fields = {f.name: f.default for f in dataclasses.fields(Settings)}
    assert fields["host"] == "0.0.0.0"          # the chart does not set CURATOR_HOST
    assert fields["port"] == chart_values()["server"]["port"]
