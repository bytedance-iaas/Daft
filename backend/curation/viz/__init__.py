"""Format-level logic of the data visualizer (design doc 18), shared by the CLI and the Daemon.

Preflight (``cli/preflight.py``) describes a dataset with it - features, camera codecs, the
segment annotations it recognises - and the Daemon's readers (``daemon/viz``) build the
presentation model from the same functions, so the two never disagree. Nothing here opens a
dataset itself: it reads the file-like objects it is handed and writes only into the directory it
is given (an mcap episode's products in the Daemon's cache); nothing here decides a verdict.

* :mod:`.lerobot_info` - info.json features: names, cameras and their codecs, depth and image
  columns;
* :mod:`.groups` - curve groups (design doc 18 §5.3);
* :mod:`.annotations` - segment annotations: which layouts a dataset uses (metadata only) and the
  tracks / events / labels of one episode (design doc 18 §3.4, §4.5, D64);
* :mod:`.series` - an episode's numeric columns from its parquet rows, and min / max thinning;
* :mod:`.transcode` - ``python -m curation.viz.transcode``: an H.264 copy of a camera the
  browser cannot play (D60), run by the Daemon in a subprocess;
* mcap (design doc 18 §6, D62): :mod:`.mcap_messages` (decoding, numeric leaves and field paths,
  picture codecs and sizes), :mod:`.mcap_probe` (a file's topics from its summary and first
  messages), :mod:`.mcap_mapping` (built-in templates, drafting, validation, the check reader's
  mapping and what it will not read), :mod:`.mcap_episode` (one pass over an episode: frame packs,
  curves, clock), :mod:`.remux` (Annex-B samples into fragmented mp4).
"""
