"""The data visualizer (design doc 18, D60-D64; C4 2.4.0).

Readers turn a dataset - a registration, or a task's frozen input - into one presentation model
(records, a clock, streams, annotations) that every view of the console reads; the Daemon only
serves what a browser cannot fetch itself (remuxed or transcoded cameras, JPEG frame packs, the
bytes of local datasets) and small derived data (curves, annotations, the field tree).

* :mod:`.status` - whether a registration can be visualized (``DatasetItem.viz``).
"""
