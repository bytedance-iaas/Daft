#!/usr/bin/env python
"""Register regression sample subsets as datasets in a Curator Daemon (design doc 16 §7.9, ledger F11.5).

Each pick is one sub-dataset - a tos:// address in the sample bucket, or a path under the site's local mount - with an
optional name and note. ``POST {api}/datasets`` preflights it and keeps its file listing (D36); registering the same
address again returns the existing registration, so the script can be run again after a failure (a changed name or
note is patched in). An mcap subset without a confirmed field mapping gets the one the Daemon drafts from its topics,
confirmed as drafted - what the add drawer does when nobody edits it (design doc 18 §6).

The API needs the HTTP Basic login (design doc 08 §5). It is read from CURATOR_USER / CURATOR_PASSWORD or asked for on
the terminal, sent only to the Daemon and never printed or written. ``--no-auth`` for a Daemon run with
CURATOR_AUTH_MODE=none.

    # the Daemon in a cluster, through a port-forward the script opens and closes (kubectl on PATH)
    python tools/regression_samples/register.py --kube dataverse/dataverse-curation-0 --list-credentials
    python tools/regression_samples/register.py --kube dataverse/dataverse-curation-0 \\
        --picks tools/regression_samples/console_picks.json [--credential <access key name>] [--out registered.jsonl]
    # any Daemon you can reach
    python tools/regression_samples/register.py --api http://127.0.0.1:8080/curation/api/v1 --picks ... [--no-auth]

Without --credential the TOS picks use the Daemon's default access key, or its only one. Exit codes: 0 every pick
registered; 1 some failed (the others stay registered); 2 bad input, a refused login or an unreachable Daemon.
"""
from __future__ import annotations

import argparse
import base64
import getpass
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

REGISTER_TIMEOUT_S = 1800        # preflight plus the full listing of a large subset
READY_TIMEOUT_S = 60


class Stop(Exception):
    """Bad input, a refused login or an unreachable Daemon: nothing (more) is registered, exit code 2."""


class Api:
    def __init__(self, base: str, auth: str | None):
        self.base = base.rstrip("/")
        self.auth = auth

    def call(self, method: str, path: str, body=None, timeout: float = 120):
        headers = {"Accept": "application/json"}
        if self.auth:
            headers["Authorization"] = self.auth
        data = None
        if method != "GET":                         # C4: every write carries the JSON content type, a body or not
            headers["Content-Type"] = "application/json"
            headers["Idempotency-Key"] = uuid.uuid4().hex
            data = json.dumps(body if body is not None else {}).encode()
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                payload = r.read()
                return r.status, json.loads(payload) if payload else None
        except urllib.error.HTTPError as e:
            payload = e.read()
            try:
                return e.code, json.loads(payload)
            except ValueError:
                return e.code, {"error": {"code": "http_%d" % e.code, "message": payload[:300].decode("utf-8", "replace")}}
        except (urllib.error.URLError, OSError) as e:
            raise Stop(f"连不上 {self.base}：{e}") from e


def error_text(doc) -> str:
    err = (doc or {}).get("error") if isinstance(doc, dict) else None
    if isinstance(err, dict):
        return f"{err.get('code')}: {err.get('message')}"
    return json.dumps(doc, ensure_ascii=False)[:300]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def port_forward(target: str, pod_port: int, base_path: str):
    """kubectl port-forward to NAMESPACE/POD; returns (process, API base) once healthz answers."""
    ns, _, pod = target.partition("/")
    if not ns or not pod:
        raise Stop("--kube 要写成 NAMESPACE/POD，例如 dataverse/dataverse-curation-0")
    port = free_port()
    log = tempfile.TemporaryFile()                  # not a pipe: an unread pipe would stall kubectl once it fills
    proc = subprocess.Popen(["kubectl", "-n", ns, "port-forward", f"pod/{pod}", f"{port}:{pod_port}"],
                            stdout=subprocess.DEVNULL, stderr=log)
    origin = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + READY_TIMEOUT_S
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            log.seek(0)
            raise Stop("kubectl port-forward 退出了：" + log.read().decode("utf-8", "replace")[-500:])
        try:
            with urllib.request.urlopen(f"{origin}{base_path}/healthz", timeout=5) as r:
                if r.status == 200:
                    return proc, f"{origin}{base_path}/api/v1"
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(1)
    proc.terminate()
    raise Stop(f"{READY_TIMEOUT_S} 秒内 {origin}{base_path}/healthz 没有应答")


def basic_auth(no_auth: bool) -> str | None:
    if no_auth:
        return None
    user = os.environ.get("CURATOR_USER") or input("质检台账号：").strip()
    password = os.environ.get("CURATOR_PASSWORD") or getpass.getpass("密码（不回显）：")
    if not user or not password:
        raise Stop("账号和密码都要给")
    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()


def load_picks(args) -> tuple[list[dict], str | None]:
    picks: list[dict] = []
    region = None
    if args.picks:
        try:
            with open(args.picks, encoding="utf-8") as fh:
                doc = json.load(fh)
        except (OSError, ValueError) as e:
            raise Stop(f"读不了 {args.picks}：{e}") from e
        items = doc.get("picks", []) if isinstance(doc, dict) else doc
        region = doc.get("region") if isinstance(doc, dict) else None
        for it in items:
            picks.append(it if isinstance(it, dict) else {"uri": it})
    picks += [{"uri": u} for u in args.uris]
    for p in picks:
        if not p.get("uri"):
            raise Stop(f"这一项没有 uri：{p}")
    return picks, region


def pick_credential(api: Api, wanted: str | None) -> str:
    st, doc = api.call("GET", "/credentials")
    if st == 401:
        raise Stop("登录被拒（401），检查账号和密码")
    if st != 200:
        raise Stop("读访问密钥失败：" + error_text(doc))
    items = doc.get("items") or []
    names = [c["name"] for c in items]
    if wanted:
        if wanted not in names:
            raise Stop(f"没有叫 {wanted} 的访问密钥；现有：{', '.join(names) or '（无）'}")
        return wanted
    defaults = [c["name"] for c in items if c.get("is_default")]
    if len(defaults) == 1:
        return defaults[0]
    if len(names) == 1:
        return names[0]
    raise Stop("请用 --credential 指定访问密钥；现有：" + (", ".join(names) or "（无，先在「系统和资源配置」里添加）"))


def list_credentials(api: Api) -> None:
    st, doc = api.call("GET", "/credentials")
    if st == 401:
        raise Stop("登录被拒（401），检查账号和密码")
    if st != 200:
        raise Stop("读访问密钥失败：" + error_text(doc))
    for c in doc.get("items") or []:
        meta = c.get("meta") or {}
        print(f"{c['name']}\tregion={meta.get('region')}\tkey=…{meta.get('access_key_id_hint') or '?'}"
              f"\tverify={c.get('verify_state')}\tdefault={c.get('is_default')}")


def input_of(uri: str, region: str | None, credential: str | None) -> dict:
    if uri.startswith("tos://"):
        return {"source": "tos", "uri": uri, "region": region, "credential": credential}
    return {"source": "local", "uri": uri}


def check_access(api: Api, uri: str, region: str, credential: str) -> None:
    """List the parent prefix of the first TOS pick: a wrong key or bucket fails here, before anything is registered."""
    parent = uri.rstrip("/").rsplit("/", 1)[0] + "/"
    q = urllib.parse.urlencode({"source": "tos", "uri": parent, "region": region, "credential": credential, "limit": 1})
    st, doc = api.call("GET", "/datasets/browse?" + q, timeout=300)
    if st != 200:
        raise Stop(f"访问密钥 {credential} 列不出 {parent}：" + error_text(doc))


def confirm_mapping(api: Api, ds: dict) -> str:
    st, probe = api.call("POST", "/viz/mcap-probe", {"input": {"dataset_id": ds["id"]}}, timeout=REGISTER_TIMEOUT_S)
    if st != 200:
        return "探测失败 " + error_text(probe)
    st, doc = api.call("PUT", f"/datasets/{ds['id']}/mapping", {"mapping": probe["draft"]}, timeout=REGISTER_TIMEOUT_S)
    if st != 200:
        return "确认失败 " + error_text(doc)
    matched = (probe.get("matched") or {}).get("name") or probe["draft"].get("name")
    return f"已确认（{matched}，第 {doc.get('version')} 版）"


def register(api: Api, pick: dict, region: str | None, credential: str | None, confirm: bool) -> dict:
    body: dict = {"input": input_of(pick["uri"], region, credential)}
    for k in ("name", "note"):
        if pick.get(k):
            body[k] = pick[k]
    rec: dict = {"uri": pick["uri"], "ok": False}
    st, ds = api.call("POST", "/datasets", body, timeout=REGISTER_TIMEOUT_S)
    if st not in (200, 201):
        rec["error"] = error_text(ds)
        return rec
    rec["created"] = st == 201
    patch = {k: pick[k] for k in ("name", "note") if pick.get(k) and pick[k] != ds.get(k)}
    if not rec["created"] and patch:
        st, doc = api.call("PATCH", f"/datasets/{ds['id']}", patch)
        if st == 200:
            ds = doc
    pf = ds.get("preflight") or {}
    fmt = pf.get("format") or {}
    rec.update(id=ds["id"], name=ds.get("name"), format=ds.get("format"), episodes=ds.get("episode_count"),
               robot_type=ds.get("robot_type"), supported=bool(fmt.get("supported")),
               validation=pf.get("validation") or [], detail=fmt.get("detail"),
               objects=(ds.get("listing") or {}).get("objects"))
    mapping = ds.get("viz_mapping") or {}
    if confirm and mapping.get("state") == "none":
        rec["mapping"] = confirm_mapping(api, ds)
    else:
        rec["mapping"] = mapping.get("state") if mapping else None
    rec["ok"] = True
    return rec


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    where = ap.add_mutually_exclusive_group(required=True)
    where.add_argument("--api", help="the Daemon's API base, e.g. http://127.0.0.1:8080/curation/api/v1")
    where.add_argument("--kube", metavar="NAMESPACE/POD", help="port-forward to this pod first (kubectl)")
    ap.add_argument("--base-path", default="/curation", help="the Daemon's mount prefix (CURATOR_BASE_PATH), with --kube")
    ap.add_argument("--pod-port", type=int, default=8080, help="the Daemon's port in the pod, with --kube")
    ap.add_argument("--no-auth", action="store_true", help="the Daemon runs with CURATOR_AUTH_MODE=none")
    ap.add_argument("--list-credentials", action="store_true", help="print the access keys (names only) and stop")
    ap.add_argument("--picks", help="JSON: {region, picks: [{uri, name?, note?}]} or a list of them")
    ap.add_argument("--credential", help="access key name for tos:// picks (default: the Daemon's default key)")
    ap.add_argument("--region", help="region of tos:// picks (default: the picks file's, else cn-beijing)")
    ap.add_argument("--no-confirm-mapping", action="store_true", help="leave mcap mappings unconfirmed")
    ap.add_argument("--out", help="write one JSON line per pick here")
    ap.add_argument("--dry-run", action="store_true", help="print the request bodies, call nothing")
    ap.add_argument("uris", nargs="*", help="more subset addresses")
    args = ap.parse_args(argv)

    proc = None
    try:
        picks, file_region = load_picks(args)
        region = args.region or file_region or "cn-beijing"
        if not picks and not args.list_credentials:
            raise Stop("没有要登记的子集：给 --picks 或地址")
        if args.dry_run:
            for p in picks:
                body = {"input": input_of(p["uri"], region, args.credential or "<default key>")}
                body.update({k: p[k] for k in ("name", "note") if p.get(k)})
                print(json.dumps(body, ensure_ascii=False))
            return 0
        if args.kube:
            proc, base = port_forward(args.kube, args.pod_port, args.base_path.rstrip("/"))
        else:
            base = args.api
        api = Api(base, basic_auth(args.no_auth))
        if args.list_credentials:
            list_credentials(api)
            return 0
        credential = None
        tos = [p for p in picks if p["uri"].startswith("tos://")]
        if tos:
            credential = pick_credential(api, args.credential)
            check_access(api, tos[0]["uri"], region, credential)
            print(f"访问密钥 {credential}，区域 {region}；{len(picks)} 个子集", flush=True)
        recs = []
        out = open(args.out, "w", encoding="utf-8") if args.out else None
        try:
            for i, p in enumerate(picks, 1):
                t0 = time.monotonic()
                rec = register(api, p, region, credential, not args.no_confirm_mapping)
                rec["seconds"] = round(time.monotonic() - t0, 1)
                recs.append(rec)
                if out:
                    out.write(json.dumps(rec, ensure_ascii=False) + "\n")
                    out.flush()
                if rec["ok"]:
                    state = "新登记" if rec["created"] else "已登记"
                    if rec["supported"] and not rec["validation"]:
                        pre = "预检通过"
                    else:
                        pre = "预检有问题：" + ("；".join(rec["validation"]) or rec.get("detail") or "格式不支持")[:200]
                    print(f"[{i}/{len(picks)}] {state} {rec['id']} {rec['name']} | {rec['format']} {rec['episodes']} 条 | {pre}"
                          + (f" | 映射 {rec['mapping']}" if rec.get("mapping") not in (None, "not_needed") else ""), flush=True)
                else:
                    print(f"[{i}/{len(picks)}] 失败 {p['uri']} | {rec['error']}", flush=True)
        finally:
            if out:
                out.close()
        failed = [r for r in recs if not r["ok"]]
        print(f"完成：{len(recs) - len(failed)} 个登记成功，{len(failed)} 个失败")
        return 1 if failed else 0
    except Stop as e:
        print(e, file=sys.stderr)
        return 2
    finally:
        if proc is not None:
            proc.terminate()


if __name__ == "__main__":
    sys.exit(main())
