"""Write the platform's side of the sample set's taxonomy from the module registry (design doc 17 §6.2).

``platform_status``, ``platform_codes`` and ``platform_conditions`` in ``taxonomy.json`` are notes about the
platform, not part of the taxonomy (C6 has the items without them). They are generated from the registry's
export, ``docs/contracts/modules.json``, so they follow the platform and nobody edits them by hand:

* 能判 - a code of the item rejects or asks a person under the default policy (P18);
* 有读数 - the item's codes are only reported, or a module covers it with a reading and no code;
* 部分 - one of the two above, but every module that covers it needs something not every dataset has
  (``platform_conditions`` says what);
* 没有 - no module covers it: a gap (design doc 16 §3.10);
* 能处理 - a control item, which no code may name.

The preflight is not a module but judges one item itself: SET-4, the input format (design doc 17 §6.1); its
code is ``preflight``. ``platform_codes`` lists ``module.code`` of every code mapped to the item. The free-text ``platform`` column
(what the samples showed) is left alone.

    PYTHONPATH=tools python -m regression_samples.coverage_from_registry          # rewrite taxonomy.json
    PYTHONPATH=tools python -m regression_samples.coverage_from_registry --check  # exit 1 when out of date
"""
from __future__ import annotations

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
TAXONOMY = os.path.join(HERE, "taxonomy.json")
MODULES_JSON = os.path.join(REPO, "docs", "contracts", "modules.json")

#: a module's needs that some datasets cannot meet, and what each asks for
CONDITIONS = {"embodiment_profile": "机器人型号要在规格库里",
              "state": "数据集要有状态量",
              "eef_input": "要上传 trajectory.json"}
JUDGED_LEVELS = ("blocking", "review")
#: items the preflight judges, outside the registry
PREFLIGHT_ITEMS = ("SET-4",)
NOTE = ("平台侧的注记，不属于分类表：由模块注册表生成（coverage_from_registry.py，设计 17 §6.2）。"
        "能判 = 默认策略下判废或转人工；有读数 = 只报告或只有读数；部分 = 覆盖它的模块都有前提；没有 = 缺口；能处理 = 对照项")


def _conditions(module: dict) -> list[str]:
    return [CONDITIONS[n] for n in sorted(module.get("needs") or []) if n in CONDITIONS]


def platform_side(item: dict, modules: list[dict]) -> dict:
    """The generated notes of one taxonomy item."""
    if item.get("kind") == "control":
        return {"platform_status": "能处理", "platform_codes": []}
    if item["id"] in PREFLIGHT_ITEMS:
        return {"platform_status": "能判", "platform_codes": ["preflight"]}
    covering = [m for m in modules if item["id"] in m.get("covers", ())]
    codes = [(m, c) for m in modules for c in m.get("codes", ()) if c.get("item") == item["id"]]
    named = [f"{m['id']}.{c['code']}" for m, c in codes]
    if not covering:
        return {"platform_status": "没有", "platform_codes": named}
    judged = any(c.get("level") in JUDGED_LEVELS for _, c in codes)
    if any(not _conditions(m) for m in covering):
        return {"platform_status": "能判" if judged else "有读数", "platform_codes": named}
    return {"platform_status": "部分", "platform_codes": named,
            "platform_conditions": sorted({x for m in covering for x in _conditions(m)})}


def generate(taxonomy: dict, registry: dict) -> dict:
    """``taxonomy`` with its platform notes regenerated from ``registry`` (the C1 export)."""
    if registry.get("taxonomy_version") != taxonomy.get("taxonomy_version"):
        raise SystemExit(f"the registry binds taxonomy {registry.get('taxonomy_version')}, "
                         f"this file is {taxonomy.get('taxonomy_version')}")
    modules = registry["modules"]
    out = dict(taxonomy)
    items = []
    for item in taxonomy["items"]:
        fresh = {k: v for k, v in item.items() if k not in ("platform_status", "platform_codes", "platform_conditions")}
        side = platform_side(item, modules)
        # keep the platform notes where they were: right after the free-text platform column
        merged = {}
        for k, v in fresh.items():
            merged[k] = v
            if k == "platform":
                merged.update(side)
        if "platform_status" not in merged:
            merged.update(side)
        items.append(merged)
    out["items"] = items
    legend = dict(out.get("legend") or {})
    legend["platform_status"] = NOTE
    out["legend"] = legend
    return out


def dump(doc: dict) -> str:
    return json.dumps(doc, ensure_ascii=False, indent=1) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--check", action="store_true", help="exit 1 if taxonomy.json is not what the registry says")
    ap.add_argument("--taxonomy", default=TAXONOMY)
    ap.add_argument("--modules", default=MODULES_JSON, help="the registry export (docs/contracts/modules.json)")
    a = ap.parse_args(argv)
    with open(a.taxonomy, encoding="utf-8") as f:
        current = f.read()
    with open(a.modules, encoding="utf-8") as f:
        registry = json.load(f)
    text = dump(generate(json.loads(current), registry))
    if a.check:
        if text != current:
            print(f"{a.taxonomy} is out of date: run python -m regression_samples.coverage_from_registry",
                  file=sys.stderr)
            return 1
        return 0
    if text != current:
        with open(a.taxonomy, "w", encoding="utf-8") as f:
            f.write(text)
        print(f"wrote {a.taxonomy}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
