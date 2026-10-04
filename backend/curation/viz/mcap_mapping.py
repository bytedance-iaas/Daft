"""mcap field mappings (C7 ``viz-mapping/1.0``, design doc 18 §6, D62): built-in templates, drafting
from a probe, validation, and the check reader's ``ingest.mcap_mapping`` derived from a mapping.

Drafting matches the site library (design doc 18 §6.3): the built-in UMI recognition
(``/robotN/vio/eef_pose``), the default convention of the check reader (``/action``,
``/observation.state``, ``/observation.images.*``, ``/task``) and the team's templates (those with at
least 80 % of their topics in the file) are ranked by coverage, then by how many of the file's topics
they name; on a tie the team's template wins (a UMI mapping the team adjusted and saved). When none
applies, the generic ``builtin:foxglove`` / ``builtin:ros2`` rules draft by message encoding. A
mapping drafted by the UMI or the default rule derives exactly what the check reader recognises on its
own today, so confirming such a draft without changes never changes a verdict.
"""
from __future__ import annotations

import copy
import re
from typing import Any

from .mcap_probe import FileProbe, TopicProbe

SCHEMA_VERSION = "viz-mapping/1.0"
MATCH_THRESHOLD = 0.8

BUILTINS = [
    {"id": "builtin:umi", "name": "UMI 手持夹爪（内置）",
     "description": "/robotN/sensor/cameraN/compressed 为相机；/robotN/vio/eef_pose 与 /robotN/sensor/magnetic_encoder 为动作曲线（UMI 没有指令流，末端轨迹就是它的动作）"},
    {"id": "builtin:foxglove", "name": "Foxglove 通用（内置）",
     "description": "按 schema 归类：CompressedImage / CompressedVideo 为相机，PoseInFrame、关节、编码器等数值消息为曲线，标定、坐标变换、系统信息忽略"},
    {"id": "builtin:ros2", "name": "ROS 2 通用（内置）",
     "description": "sensor_msgs Image / CompressedImage 为相机，JointState、PoseStamped、TwistStamped、WrenchStamped 为曲线，/task、/instruction 为任务描述，tf 与 CameraInfo 忽略"},
]

_UMI_POSE = re.compile(r"^/(robot\d+)/vio/eef_pose$")
_UMI_CAM = re.compile(r"^/robot\d+/sensor/camera\d+/compressed$")
#: the check reader's default convention (``ingest.mcap_reader.DEFAULT_MAPPING``)
DEFAULT_ACTION, DEFAULT_STATE, DEFAULT_TASK, DEFAULT_VIDEO = "/action", "/observation.state", "/task", "/observation.images."
_IGNORE_SCHEMAS = ("CameraCalibration", "CameraInfo", "FrameTransform", "TFMessage", "Log", "RobotInfo",
                   "SystemInfo", "SceneUpdate", "PointCloud", "Grid", "LocationFix", "LaserScan", "Diagnostic",
                   "ParameterEvent")
_HIGH_RATE_HZ = 150.0
_TASK_TOPICS = ("/task", "/instruction", "/language_instruction", "/prompt")
_TASK_KEYS = ("task", "task_name", "instruction", "language_instruction", "prompt")


def _short(schema: str | None) -> str:
    s = (schema or "").replace("/msg/", "/")
    return s.rsplit(".", 1)[-1].rsplit("/", 1)[-1]


def _empty(name: str, base: str | None) -> dict:
    return {"schema_version": SCHEMA_VERSION, "name": name, "base": base,
            "timeline": {"source": "log_time", "frame_reference": None},
            "cameras": [], "series": [], "task": None, "segments": None, "ignore": []}


def _display(topic: str) -> str:
    parts = [p for p in topic.strip("/").split("/") if p not in ("compressed", "image_raw", "sensor", "color")]
    return " ".join(parts) or topic


# ---------------------------------------------------------------- built-in drafts

def draft_umi(probe: FileProbe) -> dict | None:
    topics = sorted(probe.topics)
    robots = sorted({m.group(1) for t in topics if (m := _UMI_POSE.match(t))})
    if not robots:
        return None
    m = _empty("UMI 手持夹爪", "builtin:umi")
    for t in topics:                                     # sorted, as the check reader lists them
        if _UMI_CAM.match(t):
            m["cameras"].append({"topic": t, "name": _display(t), **_schema(probe, t)})
    for r in robots:
        pose = f"/{r}/vio/eef_pose"
        m["series"].append({"topic": pose, "name": _display(pose), **_schema(probe, pose), "fields": ["pose"],
                            "labels": [f"{r}_{n}" for n in ("x", "y", "z", "qx", "qy", "qz", "qw")],
                            "unit": None, "role": "action"})
        enc = f"/{r}/sensor/magnetic_encoder"
        if enc in probe.topics:
            m["series"].append({"topic": enc, "name": _display(enc), **_schema(probe, enc), "fields": ["value"],
                                "labels": [f"{r}_gripper"], "unit": None, "role": "action"})
    used = {c["topic"] for c in m["cameras"]} | {s["topic"] for s in m["series"]}
    m["ignore"] = [t for t in topics if t not in used]
    m["task"] = _metadata_task(probe)       # the check reader reads metadata task keys on its own too
    return m


def draft_default(probe: FileProbe) -> dict | None:
    """The check reader's own convention: ``/action`` (+ ``/observation.state``)."""
    if DEFAULT_ACTION not in probe.topics:
        return None
    m = _empty("默认约定（/action、/observation.state）", None)
    for t in sorted(probe.topics):
        if t.startswith(DEFAULT_VIDEO):
            m["cameras"].append({"topic": t, "name": t[len(DEFAULT_VIDEO):] or t, **_schema(probe, t)})
    state = DEFAULT_STATE in probe.topics
    pair_name = f"{DEFAULT_STATE.strip('/')} / {DEFAULT_ACTION.strip('/')}"            # the topics' own names
    m["series"].append({"topic": DEFAULT_ACTION, "name": DEFAULT_ACTION.strip("/") if not state else pair_name,
                        **_schema(probe, DEFAULT_ACTION), "role": "action",
                        "pair_with": DEFAULT_STATE if state else None})
    if state:
        m["series"].append({"topic": DEFAULT_STATE, "name": pair_name, **_schema(probe, DEFAULT_STATE),
                            "role": "state", "pair_with": DEFAULT_ACTION})
    if DEFAULT_TASK in probe.topics:
        m["task"] = {"topic": DEFAULT_TASK}
    else:
        m["task"] = _metadata_task(probe)
    used = {c["topic"] for c in m["cameras"]} | {s["topic"] for s in m["series"]} | {DEFAULT_TASK}
    m["ignore"] = [t for t in sorted(probe.topics) if t not in used]
    return m


def _schema(probe: FileProbe, topic: str) -> dict:
    s = probe.topics[topic].schema if topic in probe.topics else None
    return {"schema": s} if s else {}


def _metadata_task(probe: FileProbe) -> dict | None:
    for rec in probe.metadata.values():
        for k in rec:
            if k.lower() in _TASK_KEYS:
                return {"metadata_key": k}
    return None


def _role_by_name(topic: str) -> str:
    t = topic.lower()
    if any(w in t for w in ("action", "command", "cmd", "target", "goal")):
        return "action"
    if any(w in t for w in ("state", "feedback", "obs", "joint", "pose", "encoder", "gripper")):
        return "state"
    return "other"


_PAIR = re.compile(r"(.*?)([-_/])(state|action)$")


def _pair_up(series: list[dict]) -> None:
    """``*-state`` / ``*-action`` (``_`` / ``/`` too) of the same stem are drawn together."""
    by = {s["topic"]: s for s in series}
    for s in series:
        m = _PAIR.match(s["topic"])
        if not m:
            continue
        other = f"{m.group(1)}{m.group(2)}{'action' if m.group(3) == 'state' else 'state'}"
        if other in by:
            s["role"] = m.group(3)
            s["pair_with"] = other


def _segment_fields(tp: TopicProbe) -> dict | None:
    """A message with a start, an end and a label is a segment (``/subtask``)."""
    nums = {f["path"] for f in tp.fields or [] if f["size"] == 1}
    texts = set(tp.strings or [])
    for a, b in (("start", "end"), ("start_s", "end_s"), ("start_time", "end_time"), ("begin", "end")):
        if a in nums and b in nums:
            label = next((x for x in ("label", "name", "text", "subtask", "task") if x in texts), None)
            if label:
                return {"start_field": a, "end_field": b, "label_field": label}
    return None


def _pick_fields(tp: TopicProbe) -> list[str] | None:
    fields = [f["path"] for f in tp.fields or []]
    for want in ("position", "q", "joint_positions", "value", "data"):
        if want in fields:
            return [want]
    return fields[:6] or None


def draft_generic(probe: FileProbe, base: str) -> dict:
    """``builtin:foxglove`` / ``builtin:ros2``: classify topics by schema and shape."""
    m = _empty("Foxglove 通用" if base == "builtin:foxglove" else "ROS 2 通用", base)
    unmapped = []
    for t in sorted(probe.topics):
        tp = probe.topics[t]
        short = _short(tp.schema)
        if tp.kind == "camera":
            m["cameras"].append({"topic": t, "name": _display(t), **_schema(probe, t)})
            continue
        if any(x in short for x in _IGNORE_SCHEMAS):
            m["ignore"].append(t)
            continue
        if tp.kind == "text" or t in _TASK_TOPICS or "Instruction" in short:
            if m["task"] is None and (t in _TASK_TOPICS or "Instruction" in short or "task" in t.lower()):
                m["task"] = {"topic": t}
            else:
                m["ignore"].append(t)
            continue
        seg = _segment_fields(tp)
        if seg is not None and m["segments"] is None:
            m["segments"] = {"topic": t, **seg}
            continue
        if tp.kind == "series":
            rate = tp.rate_hz(probe.end_ns)
            entry: dict[str, Any] = {"topic": t, "name": _display(t), **_schema(probe, t), "role": _role_by_name(t)}
            if "PoseInFrame" in short or "PoseStamped" in short or short == "Pose":
                entry["fields"] = ["pose.position", "pose.orientation"]
                entry["transforms"] = {"pose.orientation": "quat_xyzw_to_rpy"}
            elif "Twist" in short:
                entry["fields"] = ["twist.linear", "twist.angular"]
            elif "Wrench" in short:
                entry["fields"] = ["wrench.force", "wrench.torque"]
            elif "JointState" in short or tp.names:
                entry["fields"] = ["position"]
                entry["names_field"] = "name"
                entry["unit"] = "rad"
            elif "IMU" in short.upper() or "imu" in t.lower() or (
                    rate is not None and rate > _HIGH_RATE_HZ and entry["role"] == "other" and not _PAIR.match(t)):
                # a sensor sampled fast (an IMU at 200 Hz): a curve, not in the smart layout; arm
                # states and actions are often as fast (ABC-130k: 200-270 Hz) and stay in it
                entry["fields"] = [f["path"] for f in tp.fields or []][:4] or None
                entry["role"] = "other"
                entry["smart"] = False
            else:
                picked = _pick_fields(tp)
                if picked:
                    entry["fields"] = picked
            if entry.get("fields") is None:
                entry.pop("fields", None)
            m["series"].append(entry)
            continue
        unmapped.append(t)
    _pair_up(m["series"])
    if m["task"] is None:
        m["task"] = _metadata_task(probe)
    return m


# ---------------------------------------------------------------- matching

def topics_of(mapping: dict) -> set[str]:
    out = {c["topic"] for c in mapping.get("cameras") or []} | {s["topic"] for s in mapping.get("series") or []}
    task = mapping.get("task")
    if isinstance(task, dict) and task.get("topic"):
        out.add(task["topic"])
    seg = mapping.get("segments")
    if isinstance(seg, dict) and seg.get("topic"):
        out.add(seg["topic"])
    return out


def coverage(mapping: dict, topics: set[str]) -> float:
    named = topics_of(mapping)
    return len(named & topics) / len(named) if named else 0.0


def from_template(template: dict, probe: FileProbe) -> dict:
    """A site template applied to a probe: entries whose topic the file lacks are dropped."""
    m = copy.deepcopy(template)
    have = set(probe.topics)
    m["cameras"] = [c for c in m.get("cameras") or [] if c["topic"] in have]
    m["series"] = [s for s in m.get("series") or [] if s["topic"] in have]
    for s in m["series"]:
        if s.get("pair_with") and s["pair_with"] not in {x["topic"] for x in m["series"]}:
            s["pair_with"] = None
    if isinstance(m.get("task"), dict) and m["task"].get("topic") and m["task"]["topic"] not in have:
        m["task"] = None
    m["ignore"] = [t for t in m.get("ignore") or [] if t in have]
    return m


def draft(probe: FileProbe, *, template: str | None = None,
          site_templates: list[dict] | None = None) -> tuple[dict, dict | None]:
    """(draft mapping, matched ``{template_id, name, coverage}`` or None)."""
    site = {t["id"]: t for t in site_templates or []}
    topics = set(probe.topics)
    if template == "builtin:umi":
        m = draft_umi(probe)
        if m is not None:
            return m, {"template_id": "builtin:umi", "name": BUILTINS[0]["name"], "coverage": round(coverage(m, topics), 3)}
    if template is None:
        # (coverage, topics named, a team template first on a tie, the draft, what matched)
        ranked: list[tuple[float, int, int, dict, dict | None]] = []
        umi = draft_umi(probe)
        if umi is not None:
            ranked.append((coverage(umi, topics), len(topics_of(umi) & topics), 0, umi,
                           {"template_id": "builtin:umi", "name": BUILTINS[0]["name"], "coverage": None}))
        d = draft_default(probe)
        if d is not None:
            ranked.append((coverage(d, topics), len(topics_of(d) & topics), 0, d, None))
        for t in site.values():
            if not isinstance(t.get("mapping"), dict):
                continue
            c = coverage(t["mapping"], topics)
            if c >= MATCH_THRESHOLD:
                ranked.append((c, len(topics_of(t["mapping"]) & topics), 1, t["mapping"],
                               {"template_id": t["id"], "name": t["name"], "coverage": None}))
        if ranked:
            c, _, team, m, matched = max(ranked, key=lambda r: r[:3])
            if team:
                m = from_template(m, probe)
            if matched is not None:
                matched = {**matched, "coverage": round(c, 3)}
            return m, matched
    elif template in site and isinstance(site[template].get("mapping"), dict):
        t = site[template]
        return from_template(t["mapping"], probe), {"template_id": t["id"], "name": t["name"],
                                                    "coverage": round(coverage(t["mapping"], topics), 3)}
    base = template if template in ("builtin:foxglove", "builtin:ros2") else (
        "builtin:ros2" if any((tp.message_encoding or "") == "cdr" for tp in probe.topics.values()) else "builtin:foxglove")
    m = draft_generic(probe, base)
    matched = {"template_id": base, "name": next(b["name"] for b in BUILTINS if b["id"] == base),
               "coverage": round(coverage(m, topics), 3)} if template else None
    return m, matched


def topic_uses(mapping: dict, probe: FileProbe) -> dict[str, tuple[str, str | None, str]]:
    """``{topic: (use, role, name)}`` for the probe table (camera / series / task / segments /
    ignore / unmapped)."""
    out: dict[str, tuple[str, str | None, str]] = {}
    for c in mapping.get("cameras") or []:
        out[c["topic"]] = ("camera", None, c.get("name") or "")
    for s in mapping.get("series") or []:
        out[s["topic"]] = ("series", s.get("role"), s.get("name") or "")
    task = mapping.get("task")
    if isinstance(task, dict) and task.get("topic"):
        out[task["topic"]] = ("task", None, "任务描述")
    seg = mapping.get("segments")
    if isinstance(seg, dict) and seg.get("topic"):
        out[seg["topic"]] = ("segments", None, "分段标注")
    for t in mapping.get("ignore") or []:
        out.setdefault(t, ("ignore", None, ""))
    for t in probe.topics:
        out.setdefault(t, ("unmapped", None, ""))
    return out


# ---------------------------------------------------------------- validation

def validate(mapping: Any, topics: set[str] | None = None) -> list[dict]:
    """Located problems of a mapping (empty = fine): the C7 Schema, then the rules it cannot say -
    a topic used twice, pairs of the same role or missing, a frame reference that is not mapped,
    topics the dataset does not have (when ``topics`` is given)."""
    from ..contracts import schemas

    problems = [{"field": e.split(":", 1)[0] or "<root>", "problem": e.split(": ", 1)[-1]}
                for e in schemas.errors("viz-mapping.schema.json", mapping)]
    if problems or not isinstance(mapping, dict):
        return problems
    uses: dict[str, str] = {}

    def use(topic: str, where: str) -> None:
        if topic in uses:
            problems.append({"field": where, "problem": f"topic {topic} 同时出现在 {uses[topic]} 与 {where}"})
        else:
            uses[topic] = where

    for i, c in enumerate(mapping["cameras"]):
        use(c["topic"], f"cameras.{i}")
    series = {s["topic"]: s for s in mapping["series"]}
    for i, s in enumerate(mapping["series"]):
        use(s["topic"], f"series.{i}")
        other = s.get("pair_with")
        if other:
            partner = series.get(other)
            if partner is None:
                problems.append({"field": f"series.{i}.pair_with", "problem": f"{other} 不是映射里的曲线"})
            elif partner.get("role") == s.get("role"):
                problems.append({"field": f"series.{i}.pair_with", "problem": "成对的两组要一组状态、一组动作"})
            elif partner.get("pair_with") not in (None, s["topic"]):
                problems.append({"field": f"series.{i}.pair_with", "problem": f"{other} 已经和别的曲线成对"})
    for i, t in enumerate(mapping.get("ignore") or []):
        if t in uses:
            problems.append({"field": f"ignore.{i}", "problem": f"topic {t} 已经映射，不能同时忽略"})
    ref = (mapping.get("timeline") or {}).get("frame_reference")
    if ref and ref not in uses:
        problems.append({"field": "timeline.frame_reference", "problem": f"帧号基准 {ref} 不是映射里的相机或曲线"})
    if topics is not None:
        for topic in sorted(topics_of(mapping) | set(mapping.get("ignore") or [])):
            if topic not in topics:
                where = uses.get(topic, "ignore")
                problems.append({"field": where, "problem": f"数据集里没有 topic {topic}"})
    return problems


# ---------------------------------------------------------------- the check reader's mapping

def check_mapping(mapping: dict) -> dict:
    """``ingest.mcap_mapping`` derived from a C7 mapping (design doc 18 §6.2): ``role=action``
    series are the action (each field path a source; no fields = the whole message), state series
    the state - or the action's stand-in when there is no action series, the time anchor the check
    reader needs; cameras are the video topics; ``builtin:umi`` adds its embodiment profile."""

    def sources(entries: list[dict]) -> tuple[Any, list[str]]:
        out, names = [], []
        for s in entries:
            fields = s.get("fields") or []
            if not fields:
                out.append({"topic": s["topic"], "fields": None})
            else:
                out += [{"topic": s["topic"], "fields": f} for f in fields]
            names += list(s.get("labels") or [])
        if len(out) == 1 and out[0]["fields"] is None:
            return out[0]["topic"], names            # the convention's spelling: a bare topic
        return out, names

    series = mapping.get("series") or []
    actions = [s for s in series if s.get("role") == "action"]
    states = [s for s in series if s.get("role") == "state"]
    if actions:
        action, names = sources(actions)
        state = sources(states)[0] if states else None
    elif states:
        action, names = sources(states)
        state = None
    else:
        action, names, state = None, [], None
    task = mapping.get("task")
    out: dict[str, Any] = {"action": action, "state": state,
                           "task": task["topic"] if isinstance(task, dict) and task.get("topic") else DEFAULT_TASK,
                           "video_prefix": DEFAULT_VIDEO,
                           "video_topics": [c["topic"] for c in mapping.get("cameras") or []]}
    named = actions or states
    if names and isinstance(action, list) and all(s.get("labels") for s in named):
        out["action_names"] = names
    if mapping.get("base") == "builtin:umi":
        out["profile"] = "umi_das"
    return out


def check_gaps(mapping: dict, probe) -> list[dict]:
    """What the check reader will not read in ``mapping`` (design doc 18 §6.2, the reader is an A-class
    file and stays as it is in this phase): numbers it cannot take from a source (protobuf repeated
    fields picked by a path, ROS 2 nested messages), H.265 cameras. The visualizer reads both; the
    warnings say the checks cannot use this mapping."""
    gaps: list[dict] = []
    chk = check_mapping(mapping)
    unread = []
    for role in ("action", "state"):
        spec = chk.get(role)
        sources = [{"topic": spec, "fields": None}] if isinstance(spec, str) else list(spec or [])
        for src in sources:
            tp = probe.topics.get(src["topic"])
            if tp is None or tp.kind != "series":
                continue
            ok = (tp.check_fields or {}).get(src["fields"]) if src.get("fields") else tp.check_whole
            if ok is False:
                unread.append(src["topic"] + (f" 的 {src['fields']}" if src.get("fields") else ""))
    if unread:
        gaps.append({"code": "checks_gap",
                     "message": f"质检读取器读不出 {'、'.join(unread[:6])}{' 等' if len(unread) > 6 else ''}的数值"
                                "（protobuf 的 repeated 字段、ROS 2 的嵌套消息）：质检用不了这份映射，可视化不受影响"})
    h265 = [t for t in chk.get("video_topics") or [] if t in probe.topics and probe.topics[t].codec == "h265"]
    if h265:
        gaps.append({"code": "checks_gap",
                     "message": f"质检读取器只读 JPEG 与 H.264 相机，读不了 H.265（{'、'.join(h265[:4])}）："
                                "质检用不了这份映射，可视化不受影响"})
    return gaps
