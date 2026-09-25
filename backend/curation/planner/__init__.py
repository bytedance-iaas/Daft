"""Planner and VLM request merge framework (work package W6, feature F2.4).

Pure computation shared by the Daemon and ``curation plan`` (design doc 02,
section 3.2); no network, no v1 orchestration. Wiring the executor and the usage
ledger into ``check`` and ``adapters/vlm_client.py`` waits until the golden
baselines are archived (see README.md in this package).

* :mod:`.limits` - effective CPU concurrency and VLM parallelism (D31)
* :mod:`.gates` - one parallelism N -> the eight VLM gates (04 §2.2)
* :mod:`.plan` - :func:`build_plan`, the execution plan (04 §3)
* :mod:`.merge` - FramePolicy, MergeUnit, MergeGroup, MergeLimits, strategies (04 §4.2)
* :mod:`.executor` - sends units through an injected ``send(request)``
* :mod:`.usage` - usage parsing and the actual / attributed ledgers (04 §5)
* :mod:`.retry` - the outer ``vlm_retry`` (04 §6)
* :mod:`.throttle` - adaptive concurrency and a resizable gate (04 §7)
* :mod:`.consistency` - merge vs single-request agreement (doc 10, section 3.4)
"""
from .consistency import (MERGE_AGREEMENT_BAR, AgreementReport, default_verdict,
                          run_merge_consistency, verdict_agreement)
from .executor import (MergeExecutor, MergeReceipts, MergeRun, UnitOutcome, VlmRequest,
                       VlmResponse, chat_payload, merge_units_for)
from .gates import GATE_NAMES, V1_CONFIG_KEYS, derive_gates, v1_set_overrides
from .limits import (Limit, PlanLimits, SiteConfig, available_cpu_workers,
                     effective_cpu_concurrency, effective_vlm_parallelism, retired_site_keys)
from .merge import (DeclaredMergeUnits, FramePolicy, MergeGroup, MergeLimits, MergeStrategy,
                    MergeUnit, NoMerge, PerEpisodeMultiModule, compose_merged_prompt,
                    merge_enabled, split_answer, strategy_for_stage, strategy_named)
from .plan import PlanError, build_plan, validate_plan
from .retry import (CallFailed, Failure, RetryPolicy, RetryStats, VlmTransportError,
                    call_with_retry, classify_failure)
from .throttle import AdaptiveThrottle, ResizableGate
from .usage import (Usage, UsageAccumulator, UsageLedger, apportion, parse_usage,
                    usage_from_response)

__all__ = [
    "AdaptiveThrottle", "AgreementReport", "CallFailed", "DeclaredMergeUnits", "Failure",
    "FramePolicy", "GATE_NAMES", "Limit", "MERGE_AGREEMENT_BAR", "MergeExecutor", "MergeGroup",
    "MergeLimits", "MergeReceipts", "MergeRun", "MergeStrategy", "MergeUnit", "NoMerge",
    "PerEpisodeMultiModule", "PlanError", "PlanLimits", "ResizableGate", "RetryPolicy",
    "RetryStats", "SiteConfig", "UnitOutcome", "Usage", "UsageAccumulator", "UsageLedger",
    "V1_CONFIG_KEYS", "VlmRequest", "VlmResponse", "VlmTransportError", "apportion",
    "available_cpu_workers",
    "build_plan", "call_with_retry", "chat_payload", "classify_failure", "compose_merged_prompt",
    "default_verdict", "derive_gates", "effective_cpu_concurrency", "effective_vlm_parallelism",
    "merge_enabled", "merge_units_for", "parse_usage", "retired_site_keys", "run_merge_consistency",
    "split_answer",
    "strategy_for_stage", "strategy_named", "usage_from_response", "v1_set_overrides",
    "validate_plan", "verdict_agreement",
]
