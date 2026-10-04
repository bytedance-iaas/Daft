#!/usr/bin/env python
"""Acceptance of the data visualizer on a sample set (F13.8, design doc 18 §9.6).

Every subset is opened through a running Daemon's HTTP API the way the 可视化 page opens it: register it
(a local mount, or a tos:// address with --credential; an existing registration is reused), confirm an
mcap subset's drafted mapping as is, then
the dataset model, the episode list and, for the first and the last episode, the episode record, every
camera's first frame through the URL the player loads, and every curve group at the player's point count.

- A file or a presigned URL (access direct / local) is opened and decoded at the episode's `from_ts`
  (a LeRobot v3 file holds many episodes; ranged reads, as a browser seeks); remux / transcode are the
  Daemon's fragmented mp4, polled through 202 until ready, then read from the front until the first
  frame decodes (as a browser streams it); a frame pack is its index and the first JPEG.
- A camera browsers cannot decode (not H.264 / AV1 / VP9 / HEVC) plays through the platform transcode,
  which is what is timed; an HEVC camera's transcode fallback (browsers without HEVC) is timed once per
  subset.
- Times are wall clock at the client with a cold cache; the first episode is then opened again (warm).

Writes one JSON line per subset to --out and prints a summary. Usage, on the host with the samples (the
Daemon there with CURATOR_LOCAL_DATA_ROOT above them; --cache-dir is its CURATOR_VIZ_CACHE_DIR):

  python scripts/viz_sample_check.py --api http://127.0.0.1:18091/curation/api/v1 \\
      --cache-dir /data/viz-cache --out /tmp/viz_check.jsonl /data/out/anchor/*/* /data/out/anchor-nc/*/*

TOS subsets (their cameras are presigned URLs the browser reads directly): pass tos:// addresses with
--credential <a key registered in the Daemon> [--region cn-beijing].
"""
from __future__ import annotations

import argparse
import io
import json
import os
import statistics
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import uuid

BROWSER = {"h264", "avc1", "av1", "vp9", "vp8", "hevc", "h265"}
HEVC = {"hevc", "h265"}
POINTS = 1200            # a curve cell about 440 px wide (pointsFor in the frontend)
READY_TIMEOUT_S = 1800


class Api:
    def __init__(self, api: str):
        self.api = api.rstrip("/")
        p = urllib.parse.urlsplit(self.api)
        self.origin = f"{p.scheme}://{p.netloc}"
        self.prefix = p.path                     # /curation/api/v1: the Daemon's camera URLs carry it

    def url(self, path: str) -> str:
        if path.startswith(("http://", "https://")):
            return path
        return (self.origin if path.startswith(self.prefix + "/") else self.api) + path

    def call(self, method: str, path: str, body=None, headers=None, raw=False, timeout=900):
        h = {"Accept": "application/json"}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            h["Content-Type"] = "application/json"
        if method in ("POST", "PUT", "DELETE"):
            h["Idempotency-Key"] = uuid.uuid4().hex
        h.update(headers or {})
        req = urllib.request.Request(self.url(path), data=data, method=method, headers=h)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                payload = r.read()
                return r.status, payload if raw else (json.loads(payload) if payload else None)
        except urllib.error.HTTPError as e:
            payload = e.read()
            try:
                return e.code, json.loads(payload)
            except ValueError:
                return e.code, {"raw": payload[:300].decode("utf-8", "replace")}


def timed(fn, *a, **kw):
    t = time.perf_counter()
    out = fn(*a, **kw)
    return time.perf_counter() - t, out


def ok(status: int, doc, what: str):
    if status not in (200, 201, 206):
        err = (doc or {}).get("error", doc) if isinstance(doc, dict) else doc
        raise RuntimeError(f"{what}: HTTP {status} {json.dumps(err, ensure_ascii=False)[:400]}")
    return doc


def dir_bytes(path: str | None) -> dict:
    if not path or not os.path.isdir(path):
        return {}
    out: dict = {}
    for top in os.listdir(path):
        total = 0
        for root, _, files in os.walk(os.path.join(path, top)):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(root, f))
                except OSError:
                    pass
        out[top] = total
    return out


def wait_ready(api: Api, url: str) -> float:
    """Seconds until the Daemon's mp4 is there (202 while a remux / transcode prepares it)."""
    t = time.perf_counter()
    while True:
        st, body = api.call("GET", url, headers={"Range": "bytes=0-0"}, raw=True)
        if st in (200, 206):
            return time.perf_counter() - t
        if st != 202:
            raise RuntimeError(f"camera {url}: HTTP {st} {body if isinstance(body, dict) else ''}")
        if time.perf_counter() - t > READY_TIMEOUT_S:
            raise TimeoutError(f"camera {url}: not ready after {READY_TIMEOUT_S} s")
        time.sleep(0.25)


def decode_first(url: str, from_ts: float | None) -> dict:
    """Open a video by URL and decode the first frame at or after `from_ts`."""
    import av

    start = float(from_ts or 0.0)
    with av.open(url, options={"rw_timeout": "120000000"}) as c:
        s = c.streams.video[0]
        if start > 0 and s.time_base:
            c.seek(int(start / float(s.time_base)), stream=s, backward=True)
        for f in c.decode(s):
            if f.time is None or f.time + 1e-3 >= start:
                return {"width": f.width, "height": f.height, "t": f.time, "codec": s.codec_context.name}
    raise RuntimeError("no frame decoded")


class _Front:
    """A response read front to back: no seek, so the demuxer streams a fragmented mp4 as a browser does."""

    def __init__(self, resp):
        self.resp = resp
        self.bytes = 0

    def read(self, n: int = -1) -> bytes:
        data = self.resp.read(65536 if n is None or n < 0 else n)
        self.bytes += len(data)
        return data


def decode_first_streamed(url: str) -> dict:
    """The first frame of a fragmented mp4 read from the front (moov first, then the first fragment)."""
    import av

    with urllib.request.urlopen(urllib.request.Request(url), timeout=600) as resp:
        front = _Front(resp)
        with av.open(front, format="mp4") as c:
            for f in c.decode(video=0):
                return {"width": f.width, "height": f.height, "t": f.time, "read_bytes": front.bytes}
    raise RuntimeError("no frame decoded")


def frame_pack_first(api: Api, cam: dict) -> dict:
    wait_ready(api, cam["index_url"])
    st, idx = api.call("GET", cam["index_url"])
    ok(st, idx, "frame index")
    if not idx.get("count"):
        raise RuntimeError("empty frame pack")
    off, size = idx["offset"][0], idx["size"][0]
    st, data = api.call("GET", cam["url"], headers={"Range": f"bytes={off}-{off + size - 1}"}, raw=True)
    ok(st, None, "frame pack")
    try:
        from PIL import Image

        w, h = Image.open(io.BytesIO(data)).size
    except ImportError:
        import av

        with av.open(io.BytesIO(data), format="mjpeg") as c:
            f = next(c.decode(video=0))
            w, h = f.width, f.height
    return {"width": w, "height": h, "frames": idx["count"]}


def open_camera(api: Api, cam: dict, model_cam: dict, *, transcode: bool = False) -> dict:
    """The camera as the player shows it; `transcode` takes the platform transcode instead."""
    codec = (model_cam or {}).get("codec") or ""
    access = cam["access"]
    out = {"key": cam["key"], "access": "transcode" if transcode else access, "codec": codec}
    t = time.perf_counter()
    try:
        if transcode:
            url = cam.get("transcode_url") or (cam["url"] if access == "transcode" else None)
            if not url:
                raise RuntimeError("no transcode url")
            out["ready_s"] = round(wait_ready(api, url), 3)
            out.update(decode_first_streamed(api.url(url)))       # the copy holds this episode only
        elif access == "frames":
            out.update(frame_pack_first(api, cam))
        elif access in ("remux", "transcode"):
            out["ready_s"] = round(wait_ready(api, cam["url"]), 3)
            out.update(decode_first_streamed(api.url(cam["url"])))
        elif access in ("direct", "local"):
            # a v3 file holds many episodes: the player seeks to from_ts; the Daemon's local
            # route serves the same file, so from_ts applies to both
            out.update(decode_first(api.url(cam["url"]), cam.get("from_ts")))
        else:
            raise RuntimeError(f"not playable: {cam.get('reason') or access}")
        out["first_s"] = round(time.perf_counter() - t, 3)
        out["ok"] = True
    except Exception as exc:  # noqa: BLE001 - reported per camera
        out["ok"] = False
        out["error"] = f"{type(exc).__name__}: {exc}"[:500]
        out["first_s"] = round(time.perf_counter() - t, 3)
    return out


def open_series(api: Api, ds: str, index: int, stream: dict) -> dict:
    t = time.perf_counter()
    st, doc = api.call("GET", f"/datasets/{ds}/episodes/{index}/series?" + urllib.parse.urlencode({"stream": stream["key"], "points": POINTS}))
    out = {"key": stream["key"], "s": round(time.perf_counter() - t, 3)}
    if st != 200:
        out.update(ok=False, error=f"HTTP {st} {json.dumps((doc or {}).get('error', doc), ensure_ascii=False)[:300]}")
        return out
    lines = doc.get("lines") or []
    n = len(doc.get("t") or [])
    out.update(ok=bool(lines) and n > 0, points=n, total=doc.get("total_points"), lines=len(lines))
    if not out["ok"]:
        out["error"] = "no points" if not n else "no lines"
    return out


def open_episode(api: Api, ds: str, index: int, model: dict, *, hevc_once: list) -> dict:
    by_key = {c["key"]: c for c in model["cameras"]}
    t, (st, ep) = timed(api.call, "GET", f"/datasets/{ds}/episodes/{index}/viz")
    ok(st, ep, f"episode {index}")
    out = {"index": index, "episode_s": round(t, 3), "duration_s": ep.get("duration_s"), "frames": ep.get("frames"),
           "warnings": [w.get("code") for w in ep.get("warnings") or []], "cameras": [], "series": []}
    for cam in ep["cameras"]:
        mc = by_key.get(cam["key"], {})
        codec = (mc.get("codec") or "").lower()
        if cam["access"] in ("direct", "local", "remux") and codec and codec not in BROWSER and cam.get("transcode_url"):
            res = open_camera(api, cam, mc, transcode=True)          # what a browser has to use
        else:
            res = open_camera(api, cam, mc)
        out["cameras"].append(res)
        if codec in HEVC and not hevc_once and cam.get("transcode_url"):
            hevc_once.append(True)
            fb = open_camera(api, cam, mc, transcode=True)
            fb["fallback"] = "hevc"
            out["cameras"].append(fb)
    for s in model["streams"]:
        if s.get("kind") == "series" and s.get("available"):
            out["series"].append(open_series(api, ds, index, s))
    return out


def register(api: Api, path: str, tos: dict | None = None) -> tuple[dict, float]:
    spec = ({"source": "tos", "uri": path, "region": tos["region"], "credential": tos["credential"]}
            if path.startswith("tos://") else {"source": "local", "uri": path})
    t, (st, d) = timed(api.call, "POST", "/datasets", {"input": spec}, timeout=3600)
    return ok(st, d, "register"), t


def confirm_mapping(api: Api, d: dict) -> dict:
    out: dict = {}
    t, (st, probe) = timed(api.call, "POST", "/viz/mcap-probe", {"input": {"dataset_id": d["id"]}})
    ok(st, probe, "probe")
    out["probe_s"] = round(t, 3)
    out["matched"] = (probe.get("matched") or {}).get("name") or probe["draft"].get("name")
    out["probe_warnings"] = [w.get("code") for w in probe.get("warnings") or []]
    st, doc = api.call("PUT", f"/datasets/{d['id']}/mapping", {"mapping": probe["draft"]}, timeout=3600)
    ok(st, doc, "confirm mapping")
    out["mapping_version"] = doc.get("version")
    return out


def check_subset(api: Api, path: str, cache_dir: str | None, tos: dict | None = None) -> dict:
    rec: dict = {"path": path, "name": os.path.basename(path.rstrip("/")), "ok": False}
    before = dir_bytes(cache_dir)
    try:
        d, t = register(api, path, tos)
        rec.update(id=d["id"], format=d["format"], register_s=round(t, 3), episodes_total=d.get("episode_count"))
        if d.get("viz_mapping") and d["viz_mapping"]["state"] == "none":
            rec["mapping"] = confirm_mapping(api, d)
        t, (st, model) = timed(api.call, "GET", f"/datasets/{d['id']}/viz")
        ok(st, model, "model")
        rec.update(model_s=round(t, 3), reader=model["format"]["reader"],
                   cameras=[{"key": c["key"], "kind": c["kind"], "access": c["access"], "codec": c["codec"],
                             "size": f"{c.get('width')}x{c.get('height')}", "reason": c.get("reason")} for c in model["cameras"]],
                   streams=[{"key": s["key"], "kind": s["kind"], "available": s["available"], "smart": s["smart"],
                             "reason": s.get("reason")} for s in model["streams"]],
                   annotations=[{"key": a.get("key"), "kind": a.get("kind"), "supported": a.get("supported"),
                                 "reason": a.get("reason")} for a in model.get("annotation_sources") or []],
                   model_warnings=[w.get("code") for w in model.get("warnings") or []])
        t, (st, first) = timed(api.call, "GET", f"/datasets/{d['id']}/viz/episodes?limit=1")
        ok(st, first, "episode list")
        rec["list_s"] = round(t, 3)
        st, last = api.call("GET", f"/datasets/{d['id']}/viz/episodes?limit=1&order=desc")
        ok(st, last, "episode list (last)")
        picks = [first["items"][0]["index"]] if first["items"] else []
        if last["items"] and last["items"][0]["index"] not in picks:
            picks.append(last["items"][0]["index"])
        rec["episodes_listed"] = first.get("total")
        hevc_once: list = []
        rec["episodes"] = [open_episode(api, d["id"], i, model, hevc_once=hevc_once) for i in picks]
        if picks:                                                 # the first again, warm
            warm = open_episode(api, d["id"], picks[0], model, hevc_once=[True])
            rec["warm"] = {"episode_s": warm["episode_s"],
                           "first_s": [c["first_s"] for c in warm["cameras"]],
                           "series_s": [s["s"] for s in warm["series"]]}
        cams = [c for e in rec["episodes"] for c in e["cameras"]]
        series = [s for e in rec["episodes"] for s in e["series"]]
        rec["playable"] = bool(cams) and any(c["ok"] for c in cams)
        rec["curves"] = bool(series) and all(s["ok"] for s in series)
        rec["ok"] = bool(picks) and all(c["ok"] for c in cams) and all(s["ok"] for s in series)
    except Exception as exc:  # noqa: BLE001 - one subset's failure is reported, the run goes on
        rec["error"] = f"{type(exc).__name__}: {exc}"[:800]
        rec["trace"] = traceback.format_exc()[-1500:]
    after = dir_bytes(cache_dir)
    rec["cache_bytes"] = {k: after.get(k, 0) - before.get(k, 0) for k in after if after.get(k, 0) != before.get(k, 0)}
    return rec


def pct(xs: list[float], q: float) -> float | None:
    if not xs:
        return None
    xs = sorted(xs)
    k = min(len(xs) - 1, max(0, int(round(q * (len(xs) - 1)))))
    return round(xs[k], 3)


def summary(recs: list[dict], cache_dir: str | None) -> dict:
    def stats(xs):
        return {"n": len(xs), "p50": pct(xs, 0.5), "p90": pct(xs, 0.9), "max": round(max(xs), 3) if xs else None}

    cams = [c for r in recs for e in r.get("episodes", []) for c in e["cameras"]]
    by_access: dict = {}
    for c in cams:
        if c["ok"]:
            by_access.setdefault(c["access"] + ("/hevc-fallback" if c.get("fallback") else ""), []).append(c["first_s"])
    return {
        "subsets": len(recs),
        "ok": sum(1 for r in recs if r.get("ok")),
        "playable": sum(1 for r in recs if r.get("playable")),
        "curves": sum(1 for r in recs if r.get("curves")),
        "failed": [{"name": r["name"], "error": r.get("error"),
                    "cameras": [f"{c['key']}: {c.get('error')}" for e in r.get("episodes", []) for c in e["cameras"] if not c["ok"]],
                    "series": [f"{s['key']}: {s.get('error')}" for e in r.get("episodes", []) for s in e["series"] if not s["ok"]]}
                   for r in recs if not r.get("ok")],
        "register_s": stats([r["register_s"] for r in recs if "register_s" in r]),
        "probe_s": stats([r["mapping"]["probe_s"] for r in recs if "mapping" in r]),
        "model_s": stats([r["model_s"] for r in recs if "model_s" in r]),
        "list_s": stats([r["list_s"] for r in recs if "list_s" in r]),
        "episode_s": stats([e["episode_s"] for r in recs for e in r.get("episodes", [])]),
        "first_frame_s": {k: stats(v) for k, v in sorted(by_access.items())},
        "series_s": stats([s["s"] for r in recs for e in r.get("episodes", []) for s in e["series"] if s["ok"]]),
        "warm_episode_s": stats([r["warm"]["episode_s"] for r in recs if "warm" in r]),
        "warm_first_s": stats([x for r in recs if "warm" in r for x in r["warm"]["first_s"]]),
        "warm_series_s": stats([x for r in recs if "warm" in r for x in r["warm"]["series_s"]]),
        "cache_bytes": dir_bytes(cache_dir),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--api", required=True, help="the Daemon's API base, e.g. http://127.0.0.1:18091/curation/api/v1")
    ap.add_argument("--cache-dir", default=None, help="the Daemon's CURATOR_VIZ_CACHE_DIR, to measure what it fills")
    ap.add_argument("--out", required=True, help="JSON lines, one per subset; a .summary.json next to it")
    ap.add_argument("--credential", default=None, help="the access key (as registered in the Daemon) for tos:// subsets")
    ap.add_argument("--region", default="cn-beijing", help="the region of tos:// subsets")
    ap.add_argument("paths", nargs="+", help="subset directories (non-directories are skipped) or tos:// addresses")
    args = ap.parse_args()
    api = Api(args.api)
    paths = [p for p in args.paths if p.startswith("tos://") or os.path.isdir(p)]
    if any(p.startswith("tos://") for p in paths) and not args.credential:
        ap.error("tos:// subsets need --credential")
    tos = {"credential": args.credential, "region": args.region} if args.credential else None
    recs = []
    with open(args.out, "w") as fh:
        for i, p in enumerate(paths, 1):
            rec = check_subset(api, p, args.cache_dir, tos)
            recs.append(rec)
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            fh.flush()
            cams = [c for e in rec.get("episodes", []) for c in e["cameras"]]
            print(f"[{i}/{len(paths)}] {rec['name']}: {'ok' if rec.get('ok') else 'FAIL'}"
                  f" cams={sum(c['ok'] for c in cams)}/{len(cams)}"
                  f" first={max((c['first_s'] for c in cams), default=0):.2f}s"
                  + (f" error={rec['error'][:160]}" if rec.get("error") else ""), flush=True)
    s = summary(recs, args.cache_dir)
    with open(os.path.splitext(args.out)[0] + ".summary.json", "w") as fh:
        json.dump(s, fh, ensure_ascii=False, indent=1)
    print(json.dumps(s, ensure_ascii=False, indent=1))
    sys.exit(0 if s["ok"] == s["subsets"] else 1)


if __name__ == "__main__":
    main()
