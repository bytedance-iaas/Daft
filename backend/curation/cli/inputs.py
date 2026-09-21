"""``--input`` / ``--source``: where a source-reading command gets its dataset.

``--source`` defaults from the URI: ``tos://`` means ``tos`` (read with the
input key set), anything else ``local``. ``public`` is the HuggingFace cache
bucket from the site configuration (``public_datasets``), read anonymously; a
bare name there resolves to ``<public root>/<name>`` (the REST API's
``{"source": "public", "uri": "pusht"}``).
"""
from __future__ import annotations

import argparse

from .errors import UsageError
from .framework import Context
from .storage import Storage, is_remote, open_storage

SOURCES = ("tos", "public", "local")


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--input", required=True, metavar="URI",
                        help="dataset: tos://bucket/prefix, a local directory, or a public "
                             "dataset name with --source public")
    parser.add_argument("--source", choices=SOURCES, default=None,
                        help="where the input lives (default: tos for tos:// URIs, else local)")


def open_input(ctx: Context, args: argparse.Namespace) -> Storage:
    uri = str(args.input or "").strip()
    source = args.source or ("tos" if is_remote(uri) else "local")
    if source == "public":
        from ..ingest import public_catalog

        public_catalog.apply_config(ctx.config())
        if not public_catalog.configured():
            raise UsageError("--source public: no public dataset bucket is configured "
                             "(public_datasets.bucket in the site configuration)")
        if not is_remote(uri):
            uri = public_catalog.dataset_url(uri)
        return open_storage(uri, role="input",
                            region=public_catalog.region() or ctx.input_region, anonymous=True)
    if source == "tos" and not is_remote(uri):
        raise UsageError(f"--source tos needs a tos:// URI, got {uri!r}")
    if source == "local" and is_remote(uri):
        raise UsageError(f"--source local needs a local path, got {uri!r}")
    return open_storage(uri, role="input", region=ctx.input_region)
