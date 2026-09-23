"""Threshold profiles (design 12 §12). A profile is a separate, versioned file that records where its
numbers came from; the ``demo`` profile is explicitly ``calibrated: false``. Profiles are never chosen
from fault labels; without a profile the module only measures (no ``ok``, design 12 §9.1).
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import pathlib

import yaml

PROFILE_DIR = pathlib.Path(__file__).with_name("profiles")
SECTIONS = ("coverage", "position", "orientation", "temporal", "state_motion", "camera_motion", "diagnosis")


@dataclasses.dataclass(frozen=True)
class Profile:
    name: str
    version: str
    calibrated: bool
    source: str
    coverage: dict
    position: dict
    orientation: dict
    temporal: dict
    state_motion: dict
    camera_motion: dict
    diagnosis: dict
    sha256: str

    def summary(self) -> dict:
        return {"name": self.name, "version": self.version, "calibrated": self.calibrated, "sha256": self.sha256}


def load(name_or_path: str | None) -> Profile | None:
    """``demo`` -> the bundled profile; a path -> that YAML file; None / "" / "none" -> no profile."""
    if not name_or_path or name_or_path == "none":
        return None
    path = pathlib.Path(name_or_path)
    if not path.suffix:
        path = PROFILE_DIR / f"{name_or_path}.yaml"
    raw = path.read_bytes()
    doc = yaml.safe_load(raw)
    missing = [s for s in SECTIONS if s not in doc]
    if missing:
        raise ValueError(f"profile {path}: missing sections {missing}")
    return Profile(name=str(doc["name"]), version=str(doc["version"]), calibrated=bool(doc["calibrated"]),
                   source=str(doc["source"]), sha256=hashlib.sha256(raw).hexdigest(),
                   **{s: dict(doc[s]) for s in SECTIONS})


def config_hash(profile: Profile | None, extra: dict) -> str:
    payload = {"profile": profile.sha256 if profile else None, **extra}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()
