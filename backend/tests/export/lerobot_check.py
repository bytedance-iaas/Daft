"""Load a delivered dataset with the official LeRobot loader and report every warning.

Needs ``lerobot`` (and torch), which the shared venv does not have on purpose.  Two
loaders cover the two formats: lerobot 0.3.x reads codebase v2.1, lerobot >= 0.4
reads v3.0 only.  Used by ``test_lerobot_loader.py`` (skipped without lerobot) and
as a script:

    <lerobot-venv>/bin/python backend/tests/export/lerobot_check.py <dataset-root> [--expect-episodes N]

prints a JSON report; exit code 0 = loaded, every checked frame decoded, no warning.

"Without warnings" is literal: Python warnings and log records of level WARNING or
above emitted while the dataset is opened and frames are read.  The video backend is
pinned to ``pyav``: left to choose, lerobot probes torchcodec, which cannot load
against Homebrew's FFmpeg 8 here and says so with a warning about the machine, not
the dataset.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import tempfile
import warnings


def lerobot_info() -> tuple[str, str] | None:
    """(lerobot version, dataset codebase version it reads), or None without lerobot."""
    try:
        import lerobot
    except ImportError:
        return None
    try:
        from lerobot.datasets.lerobot_dataset import CODEBASE_VERSION
    except ImportError:
        from lerobot.datasets.dataset_metadata import CODEBASE_VERSION
    return str(getattr(lerobot, "__version__", "?")), str(CODEBASE_VERSION)


class _Records(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.WARNING)
        self.records: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(f"{record.name}: {record.getMessage()}")


def _episode_bounds(ds, ep: int) -> tuple[int, int]:
    if hasattr(ds, "episode_data_index"):                  # lerobot 0.3.x
        return int(ds.episode_data_index["from"][ep]), int(ds.episode_data_index["to"][ep])
    row = ds.meta.episodes[ep]                              # lerobot >= 0.4
    return int(row["dataset_from_index"]), int(row["dataset_to_index"])


def load_check(root: str, *, expected_tasks: list[str] | None = None,
               reference=None, max_frame_diff: float = 0.08) -> dict:
    """Open ``root`` with ``LeRobotDataset`` and read the first, middle and last frame of
    every episode (all cameras).

    ``expected_tasks[k]``: the task string item ``task`` must have for episode k.
    ``reference(ep, frame_in_episode, camera) -> HxWx3 uint8``: the frame the video
    must show there; the mean absolute difference (0-1 scale) must stay below
    ``max_frame_diff`` (re-encoding is lossy, a wrong window is not close).
    """
    import numpy as np

    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    import datasets.config
    from lerobot.datasets.lerobot_dataset import LeRobotDataset

    version, codebase = lerobot_info()
    report: dict = {"lerobot": version, "reads": codebase, "root": root, "ok": False,
                    "warnings": [], "problems": []}
    handler = _Records()
    logging.getLogger().addHandler(handler)
    bars_were_off = datasets.is_progress_bar_enabled() is False
    datasets.disable_progress_bars()
    old_cache = datasets.config.HF_DATASETS_CACHE
    with tempfile.TemporaryDirectory(prefix="lerobot-check-") as cache:
        datasets.config.HF_DATASETS_CACHE = cache           # never reuse another run's arrow cache
        try:
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                ds = LeRobotDataset("curation/export-check", root=root, video_backend="pyav")
                report["episodes"] = int(ds.num_episodes)
                report["frames"] = int(ds.num_frames)
                cams = list(ds.meta.video_keys)
                checked = 0
                for ep in range(ds.num_episodes):
                    lo, hi = _episode_bounds(ds, ep)
                    for idx in sorted({lo, (lo + hi - 1) // 2, hi - 1}):
                        item = ds[idx]
                        checked += 1
                        if int(item["episode_index"]) != ep:
                            report["problems"].append(f"frame {idx}: episode_index "
                                                      f"{int(item['episode_index'])} != {ep}")
                        if expected_tasks is not None and item["task"] != expected_tasks[ep]:
                            report["problems"].append(f"episode {ep}: task {item['task']!r} != "
                                                      f"{expected_tasks[ep]!r}")
                        for cam in cams:
                            img = item[cam]
                            if img.ndim != 3 or img.shape[0] != 3:
                                report["problems"].append(f"frame {idx} {cam}: shape {tuple(img.shape)}")
                                continue
                            if reference is not None:
                                want = reference(ep, idx - lo, cam)
                                got = img.permute(1, 2, 0).numpy()
                                diff = float(np.abs(got - want.astype(np.float32) / 255.0).mean())
                                if diff > max_frame_diff:
                                    report["problems"].append(
                                        f"episode {ep} frame {idx - lo} {cam}: differs from the "
                                        f"source frame by {diff:.3f}")
                report["items_checked"] = checked
            report["warnings"] = [f"{w.category.__name__}: {w.message}" for w in caught]
        except Exception as e:  # noqa: BLE001 - the report says what failed
            report["problems"].append(f"{type(e).__name__}: {e}")
        finally:
            datasets.config.HF_DATASETS_CACHE = old_cache
            logging.getLogger().removeHandler(handler)
            if not bars_were_off:
                datasets.enable_progress_bars()
    report["warnings"] += handler.records
    report["ok"] = not report["problems"] and not report["warnings"]
    return report


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("root")
    p.add_argument("--expect-episodes", type=int)
    args = p.parse_args(argv)
    if lerobot_info() is None:
        print(json.dumps({"ok": False, "problems": ["lerobot is not importable"]}))
        return 2
    report = load_check(args.root)
    if args.expect_episodes is not None and report.get("episodes") != args.expect_episodes:
        report["problems"].append(f"{report.get('episodes')} episodes, expected {args.expect_episodes}")
        report["ok"] = False
    print(json.dumps(report, ensure_ascii=False, indent=1))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
