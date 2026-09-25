"""数据完整性 - the data integrity module (design doc 14, D50-D52; F7.2-F7.4).

The first gate of the funnel (stage ``integrity``, before ``numeric``): per episode, are
its files whole and readable? Four parts, cheapest first:

* dataset level, once per call (:meth:`judge.IntegrityJudge.open`): the file plan, the
  LeRobot v3 episode table, orphan files, byte-equal files, v1's dark-camera prior and,
  for mcap, each episode against the others (a topic missing, a rate far below);
* L1 structure (:mod:`files`, :mod:`mp4`): magics, footers, box sizes, sample tables,
  frame counts - a few ranged reads per file;
* L2 whole read: mcap chunk and data-section CRCs, aligned blocks of zeros in compressed
  media, parquet pages, and v1's per-episode validation (``validate_episode_row``, D51);
* L3 (:mod:`decode`, parameter ``decode_test``, off by default): every frame decoded.

Findings are ``reject`` (the gate fails the episode), ``suspect`` (kept, and a person is
asked on the 完整性存疑 line) or ``dataset`` (the report only); see :mod:`findings`.
A storage failure is never a finding: it is an execution error (D33).
"""
from .judge import MODULE_ID, IntegrityJudge  # noqa: F401
