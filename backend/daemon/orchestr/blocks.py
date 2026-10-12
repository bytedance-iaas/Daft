"""The two blocks of a run, side by side (design doc 17 §3, D57).

The CPU block (integrity -> numeric -> frame -> duplicate) and the VLM block (vlm)
run in two threads and never filter each other. Inside a block the stages run in the plan's order,
as one chain of the episode pipeline (:func:`.pipeline.run_chain`): every episode with a record of a
stage goes on to the next one, findings and errors stop nothing (since D70 no stage needs the whole
selection at once).

Either block failing stops the other (a shared abort event, :meth:`Run.check_intent`); a pause or a
stop reaches every process of both. The CPU block books its CPU-pool slots to the run's key
(:attr:`Run.cpu_key`), the VLM block's chain to a key of its own, so that one chain leaving the pool
never gives back the other's slots.

Since registry 5.3 (design doc 23 §1, §4.3) a dispatch unit is a *chain*: stages linked by ``after``. The CPU
block's second root, ``vlm_prep`` (the model modules' CPU halves), starts a chain of its own, and the vlm stage
that comes after it continues that chain across the blocks - an episode's CPU half done, its model half is next.
A chain is named after the block of its last stage (``cpu``, ``vlm``), or after its first stage when that name is
taken (``vlm_prep`` with no model half).
"""
from __future__ import annotations

import threading

from curation.pipeline.episode_state import EpisodeState, state_path

from .runbase import Interrupt, TaskFailure


def stages_by_chain(plan: dict) -> dict[str, list[dict]]:
    """``chain -> its stages in order``: the plan's block stages linked by ``after`` (module docstring)."""
    chains: list[list[dict]] = []
    for st in plan["stages"]:
        if not st.get("block"):
            continue
        after = st.get("after")
        host = next((c for c in chains if after and c[-1]["id"] == after), None)
        if host is not None:
            host.append(st)
        else:
            chains.append([st])
    out: dict[str, list[dict]] = {}
    for chain in chains:
        key = chain[-1]["block"]
        out[chain[0]["id"] if key in out else key] = chain
    return out


def chains_of(plan: dict) -> dict[str, list[tuple[str, list[str]]]]:
    """``chain -> [(stage, modules)]``: each chain's per-episode stages in order (the episode store's layout)."""
    return {key: [(st["id"], list(st["modules"])) for st in stages if st.get("command") == "check"]
            for key, stages in stages_by_chain(plan).items()}


def stages_by_block(plan: dict) -> dict[str, list[dict]]:
    """The dispatch units of a plan: its chains (kept under the name the run's blocks had)."""
    return stages_by_chain(plan)


def prepare_store(run, plan: dict, selection: list[int]) -> None:
    """The episode store in its two-block layout, with every selected module indexed and positions recovered
    from records written outside the pipeline - before any worker opens it."""
    store = EpisodeState(state_path(run.wd.root))
    try:
        store.set_blocks(chains_of(plan))
        store.bootstrap(str(run.wd.root), [m for chain in chains_of(plan).values() for _, mods in chain for m in mods])
        store.seed_blocks(selection)
    finally:
        store.close()


#: the stage whose rejects no other chain judges (D92, design doc 28 §1)
INTEGRITY_STAGE = "integrity"


def integrity_chain(blocks: dict[str, list[dict]]) -> str | None:
    """The chain the data integrity stage is in, or None when the plan has no such stage."""
    for name, stages in blocks.items():
        if any(st["id"] == INTEGRITY_STAGE for st in stages):
            return name
    return None


def run_block(run, block: str, stages: list[dict], selection: list[int], abort: threading.Event,
              wait_for: tuple[str, str] | None = None) -> None:
    """One chain's stages in order (module docstring); ``block``: the chain's name; ``wait_for``: the integrity
    stage a chain without it waits for, episode by episode (D92)."""
    from .pipeline import run_chain

    pool_key = run.cpu_key if block == "cpu" else (*run.cpu_key, block)
    run.check_intent()
    chain = [st for st in stages if st.get("command") == "check"]
    if chain and not all(run.journal.done(s["id"]) for s in chain):
        run_chain(run, chain, selection, abort=abort, pool_key=pool_key, chain=block, wait_for=wait_for)


def _first(errors: list[BaseException]) -> BaseException:
    """The error to report: a pause or stop first, then a failure that is not just the other block giving
    up, then whatever came first."""
    for exc in errors:
        if isinstance(exc, Interrupt):
            return exc
    for exc in errors:
        if not (isinstance(exc, TaskFailure) and exc.code == "pipeline_cancelled"):
            return exc
    return errors[0]


def run_blocks(run, plan: dict, selection: list[int]) -> None:
    """Both blocks of ``plan`` over ``selection``, side by side; returns when both are done."""
    blocks = stages_by_block(plan)
    if not blocks:
        return
    prepare_store(run, plan, selection)
    abort = threading.Event()
    run._pipeline_abort = abort
    errors: list[BaseException] = []
    lock = threading.Lock()

    gate = integrity_chain(blocks)

    def job(block: str, stages: list[dict]) -> None:
        wait_for = (gate, INTEGRITY_STAGE) if gate is not None and block != gate else None
        try:
            run_block(run, block, stages, selection, abort, wait_for)
        except BaseException as exc:  # noqa: BLE001 - handed to the caller below
            with lock:
                errors.append(exc)
            abort.set()
            run.terminate_children()

    threads = [threading.Thread(target=job, args=(b, sts), name=f"block-{b}", daemon=True)
               for b, sts in blocks.items()]
    try:
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    finally:
        run._pipeline_abort = None
    if errors:
        raise _first(errors)
    run.check_intent()
