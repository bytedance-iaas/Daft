"""Format-level logic of the data visualizer (design doc 18), shared by the CLI and the Daemon.

Preflight (``cli/preflight.py``) describes a dataset with it - features, camera codecs, the
segment annotations it recognises - and the Daemon's readers (``daemon/viz``) build the
presentation model from the same functions, so the two never disagree. Nothing here does I/O
beyond the file-like objects it is handed, and nothing here decides a verdict.

* :mod:`.lerobot_info` - info.json features: names, cameras and their codecs, depth and image
  columns;
* :mod:`.groups` - curve groups (design doc 18 §5.3);
* :mod:`.annotations` - segment annotations: which layouts a dataset uses (metadata only) and the
  tracks / events / labels of one episode (design doc 18 §3.4, §4.5, D64);
* :mod:`.series` - an episode's numeric columns from its parquet rows, and min / max thinning;
* :mod:`.transcode` - ``python -m curation.viz.transcode``: an H.264 copy of a camera the
  browser cannot play (D60), run by the Daemon in a subprocess.
"""
