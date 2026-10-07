"""Where the visualizer reads a dataset from (design doc 18 §4.0): a registration, or a task's input.

The readers never care which: a :class:`VizSource` opens a ``curation.cli.storage.Storage`` over
the dataset (the registration's or the task's input key; anonymous for the public cache bucket; the
directory of a local dataset), hands out a URL a browser can GET for one object (presigned on TOS's
public endpoint, the plain public URL, or None for a local dataset - the Daemon serves those bytes
itself), and names the fingerprint the caches belong to.

* **dataset scope** (the visualize page): the registration's address and key, its last preflight,
  the listing kept at registration (``Dataset.manifest_path``), its confirmed mcap mapping, display
  configuration and external annotation file;
* **task scope** (the mini player): the task's frozen input - address and input key, the preflight
  and ``source_manifest.json`` in its run directory (D27), the mapping frozen into ``run.json``
  (D62) - so a deleted or changed registration does not change what the report shows.
"""
from __future__ import annotations

import contextlib
import json
import pathlib
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator

from curation.cli.storage import ObjectInfo

from ..errors import ApiError
from ..repo import protocol as P

#: how long a presigned camera URL lives (the episode answer says when it ends)
URL_TTL_S = 1800


@dataclass
class VizSource:
    scope: str                                   # dataset | task
    id: str                                      # dataset id, or task id
    owner: str
    name: str
    source: str                                  # tos | public | local
    uri: str
    region: str | None
    cred_id: str | None
    preflight: dict
    fingerprint: str
    dataset_id: str | None = None                # the registration (task scope: while it exists)
    mapping: dict | None = None                  # the mcap mapping (C7): confirmed / frozen
    mapping_version: int | None = None
    annotations_upload: str | None = None
    display_config: dict | None = None
    listing_path: pathlib.Path | None = None     # a kept listing (C2 source-manifest)
    task: P.Task | None = None
    run_dir: pathlib.Path | None = None
    _listing: dict[str, ObjectInfo] | None = field(default=None, repr=False)

    # -- identity --------------------------------------------------------------------
    @property
    def cache_key(self) -> tuple:
        return (self.scope, self.id, self.fingerprint, self.mapping_version or 0,
                self.annotations_upload or "")

    @property
    def is_local(self) -> bool:
        return self.source == "local"

    def format(self) -> tuple[str, str | None]:
        fmt = self.preflight.get("format") if isinstance(self.preflight, dict) else None
        fmt = fmt if isinstance(fmt, dict) else {}
        return str(fmt.get("kind") or "unknown"), fmt.get("version")

    # -- the listing ----------------------------------------------------------------
    def listing(self) -> dict[str, ObjectInfo] | None:
        """The objects of the dataset as listed at registration / task start (None: not kept)."""
        if self._listing is None and self.listing_path is not None:
            try:
                doc = json.loads(self.listing_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return None
            out = {}
            for o in doc.get("objects") or []:
                if isinstance(o, dict) and isinstance(o.get("key"), str) and isinstance(o.get("size"), int):
                    out[o["key"]] = ObjectInfo(o["key"], int(o["size"]), etag=o.get("etag"))
            self._listing = out
        return self._listing

    def size_of(self, rel: str) -> int | None:
        listing = self.listing()
        if listing is not None and rel in listing:
            return listing[rel].size
        return None


def dataset_source(rt, ds: P.Dataset, owner: str) -> VizSource:
    path = pathlib.Path(ds.manifest_path) if ds.manifest_path else None
    return VizSource(scope="dataset", id=ds.id, owner=owner, name=ds.name, source=ds.source, uri=ds.uri,
                     region=ds.region, cred_id=ds.credential_id,
                     preflight=ds.preflight if isinstance(ds.preflight, dict) else {},
                     fingerprint=ds.meta_fingerprint or "", dataset_id=ds.id,
                     mapping=ds.viz_mapping if isinstance(ds.viz_mapping, dict) else None,
                     mapping_version=ds.viz_mapping_version or None,
                     annotations_upload=ds.annotations_upload,
                     display_config=ds.display_config if isinstance(ds.display_config, dict) else None,
                     listing_path=path if path is not None and path.is_file() else None)


def task_source(rt, task: P.Task, owner: str, run_dir: pathlib.Path | None) -> VizSource:
    """A task's frozen input. Before the task ran, its registration's current state stands in."""
    preflight = task.preflight if isinstance(task.preflight, dict) else None
    listing = None
    frozen_mapping, frozen_version = None, None
    if run_dir is not None:
        pf = run_dir / "preflight.json"
        if preflight is None and pf.is_file():
            with contextlib.suppress(OSError, ValueError):
                preflight = json.loads(pf.read_text(encoding="utf-8"))
        manifest = run_dir / "source_manifest.json"
        listing = manifest if manifest.is_file() else None
        run_json = run_dir / "run.json"
        if run_json.is_file():
            with contextlib.suppress(OSError, ValueError):
                doc = json.loads(run_json.read_text(encoding="utf-8"))
                viz = doc.get("viz_mapping") if isinstance(doc, dict) else None
                if isinstance(viz, dict) and isinstance(viz.get("mapping"), dict):
                    frozen_mapping = viz["mapping"]
                    frozen_version = viz.get("version") if isinstance(viz.get("version"), int) else None
    ds = None
    if task.dataset_id:
        with contextlib.suppress(P.NotFound):
            ds = rt.repo.get_dataset(task.dataset_id, owner=owner)
    if preflight is None and ds is not None:
        preflight = ds.preflight
    if frozen_mapping is None and ds is not None and run_dir is None:
        frozen_mapping, frozen_version = ds.viz_mapping, ds.viz_mapping_version or None
    fp = ((task.source_fingerprint or {}).get("digest") if isinstance(task.source_fingerprint, dict) else None)
    fingerprint = str(fp or (preflight or {}).get("meta_fingerprint") or "")
    uri = task.input_uri
    return VizSource(scope="task", id=task.id, owner=owner,
                     name=(ds.name if ds is not None else uri.rstrip("/").rsplit("/", 1)[-1]),
                     source=task.input_source, uri=uri, region=task.input_region,
                     cred_id=task.input_cred_id, preflight=preflight or {}, fingerprint=fingerprint,
                     dataset_id=ds.id if ds is not None else None, mapping=frozen_mapping,
                     mapping_version=frozen_version,
                     annotations_upload=ds.annotations_upload if ds is not None else None,
                     display_config=ds.display_config if ds is not None else None,
                     listing_path=listing, task=task, run_dir=run_dir)


class Access:
    """Opens storage and signs URLs for one source, with the runtime's secrets service."""

    def __init__(self, rt, src: VizSource):
        self.rt, self.src = rt, src

    def _svc(self):
        from ..secrets import service_of

        return service_of(self.rt)

    def _key(self):
        from ..secrets import Unavailable

        if self.src.source != "tos":
            return None
        try:
            return self._svc().tos_key(self.src.cred_id, owner=self.src.owner, role="input")
        except Unavailable as err:
            raise ApiError("not_found", err.message_zh, details={"reason": err.code}) from None

    def local_root(self) -> pathlib.Path:
        from ..taskspec import local_path

        return pathlib.Path(local_path(self.rt.settings, self.src.uri, "uri"))

    @contextlib.contextmanager
    def storage(self) -> Iterator[Any]:
        from curation.cli.storage import LocalStorage, TosStorage

        if self.src.is_local:
            yield LocalStorage(str(self.local_root()))
            return
        key = self._key()
        svc = self._svc()
        region = self.src.region or (key.region if key is not None else None)
        with svc.tos(key, region) as (client, ends):
            yield TosStorage(self.src.uri, client, ends.region, role="input")

    def browser_url(self, rel: str, ttl_s: int = URL_TTL_S, cache_control: str | None = None) -> str | None:
        """A URL the browser GETs ``rel`` from; None for a local dataset (the Daemon serves it).
        ``cache_control`` is signed into the presigned URL so TOS echoes it on the response."""
        if self.src.is_local:
            return None
        from ..secrets import BadPath, browser_url

        key = self._key()
        region = self.src.region or (key.region if key is not None else None)
        try:
            return browser_url(self._svc(), self.src.uri, rel, ttl_s=ttl_s, key=key, region=region,
                               cache_control=cache_control)
        except BadPath:
            return None

    def lance_target(self, rel: str) -> tuple[str, dict[str, str] | None]:
        """Where Lance opens the table ``rel`` (``frames.lance``): a directory of a local dataset, or
        ``s3://`` on TOS's S3-compatible endpoint (design doc 19 §4.3) with the input key - anonymous
        for the public bucket. The key only ever goes to Lance's object store in this process."""
        from curation.viz.lance_layout import s3_endpoint_of, s3_options

        if self.src.is_local:
            root = self.local_root().resolve()
            path = (root / rel).resolve()
            try:
                path.relative_to(root)
            except ValueError:
                raise ApiError("not_found", "路径不在数据集之内") from None
            return str(path), None
        from ..secrets.tos import join_key, split_uri

        key = self._key()
        ends = self._svc().tos_endpoints(key, self.src.region or (key.region if key is not None else None))
        override = getattr(self.rt.settings, "viz_lance_s3_endpoint", None)
        endpoint = override or s3_endpoint_of(ends.server)
        host = endpoint.split("://", 1)[-1].split("/", 1)[0].split(":", 1)[0]
        bucket, prefix = split_uri(self.src.uri)
        opts = s3_options(endpoint, ends.region, bucket=bucket,
                          key_id=key.access_key_id if key is not None else None,
                          secret=key.secret_access_key if key is not None else None,
                          token=key.session_token if key is not None else None,
                          virtual_hosted=host.endswith((".volces.com", ".ivolces.com")))
        return f"s3://{bucket}/{join_key(prefix, rel)}", opts

    def local_file(self, rel: str) -> pathlib.Path:
        """The file of ``rel`` inside a local dataset; never outside it."""
        root = self.local_root().resolve()
        path = (root / rel).resolve()
        try:
            path.relative_to(root)
        except ValueError:
            raise ApiError("not_found", "路径不在数据集之内") from None
        if not path.is_file():
            raise ApiError("not_found", f"数据集里没有 {rel}")
        return path


ReadRange = Callable[[int, int], bytes]
