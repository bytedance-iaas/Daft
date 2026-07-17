# robot-data-curation

Robot dataset curation pipeline built on Daft: quality checks (visual, motion,
kinematic limits, video-action sync, VLM task-success), exact dedup, skill
profiling, and report generation for LeRobot v2/v3 datasets.

Usage: `curation run --input <dataset_dir> --output <out_dir>` — see `curation --help`.
