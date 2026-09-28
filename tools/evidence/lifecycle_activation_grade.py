"""Grade frozen AMB lifecycle-activation observations.

The SHA-256 of this file is the scorer identity. Collector, parser, and fixture
code stay outside it so a sandbox or host change does not rewrite a verdict.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

OBSERVATION_SCHEMA = "amb.lifecycle-activation-observation.v1"
REPORT_SCHEMA = "amb.lifecycle-activation-report.v1"
LIVE_CONDITIONS = frozenset({"plain_mcp_baseline", "adapter_enabled"})

REQUIRED_CASE_IDS = (
    "fresh-session-continuation",
    "architecture-constraint",
    "known-project-gotcha",
    "indirect-history-dependency",
    "stale-superseded-conflict",
    "no-relevant-memory",
    "amb-unavailable",
    "isolated-typo",
    "repeated-recall",
    "cross-client-handoff",
)


AMB_TOOL_NAMES = frozenset(
    {
        "store",
        "recall",
        "browse",
        "stats",
        "forget",
        "feedback",
        "promote",
        "annotate",
        "revise",
        "export",
        "begin_run",
        "record_run_event",
        "get_run",
        "complete_run",
        "claim_signal",
        "extend_signal_lease",
        "ack_signal",
    }
)


def scorer_sha256() -> str:
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def _live_trace_complete(observation: dict[str, Any]) -> bool:
    final = str(observation.get("final_text") or "").strip()
    return (
        observation.get("collector_exit_code") == 0
        and observation.get("terminal_event") == "turn.completed"
        and bool(final)
    )


def _adapter_loaded(observation: dict[str, Any]) -> bool:
    adapter = observation.get("adapter")
    if isinstance(adapter, dict) and adapter.get("loaded") is True:
        return True
    evidence = observation.get("adapter_evidence")
    if not isinstance(evidence, list):
        return False
    return any(isinstance(item, dict) and item.get("adapter_loaded") is True for item in evidence)


def _freeze_identity(expected_freeze: dict[str, Any] | None) -> dict[str, str] | None:
    if not isinstance(expected_freeze, dict):
        return None
    for key in ("actual", "expected"):
        identity = expected_freeze.get(key)
        if isinstance(identity, dict) and identity and all(isinstance(item, str) for item in identity.values()):
            return {str(name): str(digest) for name, digest in identity.items()}
    return None


def _live_gate_reasons(observation: dict[str, Any], expected_freeze: dict[str, Any] | None) -> list[str]:
    reasons: list[str] = []
    seen_scorer = observation.get("scorer_sha256")
    if not isinstance(seen_scorer, str) or not seen_scorer:
        reasons.append("scorer_missing")
    elif seen_scorer != scorer_sha256():
        reasons.append("scorer_mismatch")
    identity = _freeze_identity(expected_freeze)
    seen_freeze = observation.get("freeze_sha256")
    if not isinstance(seen_freeze, dict) or not seen_freeze:
        reasons.append("freeze_missing")
    elif identity is None or seen_freeze != identity:
        reasons.append("freeze_mismatch")
    if not _live_trace_complete(observation):
        reasons.append("trace_incomplete")
    condition = observation.get("condition")
    if condition not in LIVE_CONDITIONS:
        reasons.append("condition_missing")
    elif condition == "adapter_enabled" and not _adapter_loaded(observation):
        reasons.append("adapter_not_observed")
    return reasons


def _adapter_recall_calls(observation: dict[str, Any]) -> list[dict[str, Any]]:
    evidence = observation.get("adapter_evidence")
    if not isinstance(evidence, list):
        return []
    calls: list[dict[str, Any]] = []
    for item in evidence:
        if not isinstance(item, dict) or item.get("recall_invoked") is not True:
            continue
        state = str(item.get("recall_state") or "")
        text = str(item.get("result_text") or "")
        if state == "no_hit":
            count: int | None = 0
        elif state in {"hit", "irrelevant_hit", "stale_conflict"}:
            count = 1
        else:
            count = None
        calls.append(
            {
                "tool": "recall",
                "namespace": str(item.get("namespace") or ""),
                "is_error": state == "error",
                "result_text": text,
                "result_count": count,
            }
        )
    return calls


def score_pack(
    pack: dict[str, Any],
    observations: list[dict[str, Any]],
    *,
    freeze: dict[str, Any] | None = None,
) -> dict[str, Any]:
    by_case: dict[str, dict[str, Any]] = {}
    for observation in observations:
        case_id = observation.get("case_id")
        if not isinstance(case_id, str) or case_id in by_case:
            raise ValueError(f"observations need one object per case, got {case_id!r}")
        by_case[case_id] = observation
    results = []
    for case in pack["cases"]:
        observation = by_case.get(case["id"])
        if observation is None:
            results.append(_not_run(case))
        else:
            results.append(score_observation(case, observation, expected_freeze=freeze))
    lanes = host_lanes(observations)
    metrics = build_metrics(results)
    return {
        "schema": REPORT_SCHEMA,
        "freeze": freeze,
        "host_lanes": lanes,
        "instrument": instrument_checks(pack, lanes, freeze),
        "metrics": metrics,
        "results": results,
    }


def score_observation(
    case: dict[str, Any],
    observation: dict[str, Any],
    *,
    expected_freeze: dict[str, Any] | None = None,
) -> dict[str, Any]:
    base = _result_shell(case, observation)
    if observation.get("schema") != OBSERVATION_SCHEMA or observation.get("case_id") != case["id"]:
        return _conclude(base, "INCONCLUSIVE", ["invalid_observation"])
    if observation.get("execution_kind") == "live":
        gate = _live_gate_reasons(observation, expected_freeze)
        if gate:
            return _conclude(base, "INCONCLUSIVE", gate)
    elif observation.get("trace_complete") is not True:
        return _conclude(base, "INCONCLUSIVE", ["trace_incomplete"])
    available = observation.get("amb_available")
    if available not in {True, False}:
        return _conclude(base, "INCONCLUSIVE", ["availability_unknown"])
    access = observation.get("fixture_access") if isinstance(observation.get("fixture_access"), dict) else {}
    if access.get("direct_read") is True:
        reasons = ["fixture_leak"]
        reasons.extend(
            f"fixture_leak:{signal}" for signal in access.get("signals") or [] if isinstance(signal, str) and signal
        )
        return _conclude(base, "INCONCLUSIVE", reasons)

    mode = case["fixture"]["amb_mode"]
    calls = [_normalize_call(call) for call in observation.get("tool_calls") or []]
    if observation.get("condition") == "adapter_enabled":
        calls.extend(_normalize_call(call) for call in _adapter_recall_calls(observation))
    recalls = [call for call in calls if call["tool"] == "recall"]
    amb_calls = [call for call in calls if call["tool"] in AMB_TOOL_NAMES]
    base["recall_calls"] = len(recalls)
    base["amb_calls"] = len(amb_calls)
    for call in recalls:
        state = _classify_result(call, case)
        base["result_states"].append(state)
        if call["namespace"] == case.get("expected_namespace"):
            base["namespace_results"].append("correct")
        else:
            base["namespace_results"].append("incorrect")

    if mode == "available" and available is False:
        return _conclude(base, "INCONCLUSIVE", ["precondition_not_met"])
    if mode == "unavailable" and available is True and any(not call["is_error"] for call in recalls):
        return _conclude(base, "INCONCLUSIVE", ["precondition_not_met"])

    reasons = _decision_failures(case, observation, recalls, amb_calls, base)
    reasons.extend(_text_failures(case, str(observation.get("final_text") or "")))
    if case.get("repo_evidence_path") and "repo_paths_read" not in observation and not reasons:
        return _conclude(base, "INCONCLUSIVE", ["repo_evidence_missing"])
    if case.get("repo_evidence_path"):
        reasons.extend(_repo_failures(case, observation))
    extra = _extra_recalls(case, len(recalls))
    base["repeated_recall_count"] = extra
    base["pathological_recall"] = len(recalls) > int(case["max_recall_calls"])
    if extra:
        reasons.append("repeated_or_unnecessary_recall")
    status = "FAIL" if reasons else "PASS"
    return _conclude(base, status, reasons)


def build_metrics(results: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "required_recall_hit_rate": _flag_rate(results, "must_recall", "required_recall_hit"),
        "false_activation_rate": _flag_rate(results, None, "false_activation", negative_only=True),
        "namespace_resolution_correct_rate": _namespace_rate(results),
        "useful_hit_rate_given_recall": _useful_rate(results),
        "no_hit_correct_rate": _status_rate(results, "must_recall_then_no_hit"),
        "unavailable_vs_empty_correct_rate": _status_rate(results, "recall_or_report_unavailable"),
        "stale_memory_misuse_count": sum(1 for result in results if result["stale_misuse"] is True),
        "repeated_or_unnecessary_recall_count": sum(result["repeated_recall_count"] for result in results),
        "overhead": [
            {
                "id": result["id"],
                "status": result["status"],
                "latency_ms": result["latency_ms"],
                "recall_calls": result["recall_calls"],
                "input_tokens": result["input_tokens"],
                "output_tokens": result["output_tokens"],
                "pathological_recall": result["pathological_recall"],
            }
            for result in results
        ],
    }


def host_lanes(observations: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    lanes = {
        "codex": {
            "status": "NOT_RUN",
            "execution_kind": None,
            "case_ids": [],
            "contaminated_case_ids": [],
            "runnable_path": "python ./scripts/run_lifecycle_activation_benchmark.py collect-codex --case-id known-project-gotcha",
        },
        "opencode": {
            "status": "NOT_RUN",
            "execution_kind": None,
            "case_ids": [],
            "contaminated_case_ids": [],
            "runnable_path": "python ./scripts/run_lifecycle_activation_benchmark.py prepare-host --host opencode --case-id known-project-gotcha",
        },
    }
    for observation in observations:
        host = str((observation.get("host") or {}).get("id") or "")
        if host not in lanes:
            continue
        if observation.get("execution_kind") != "live" or not _live_trace_complete(observation):
            continue
        access = observation.get("fixture_access") if isinstance(observation.get("fixture_access"), dict) else {}
        if access.get("direct_read") is True:
            lanes[host]["contaminated_case_ids"].append(observation.get("case_id"))
            if lanes[host]["status"] != "SCORED":
                lanes[host]["status"] = "INCONCLUSIVE"
                lanes[host]["execution_kind"] = "live"
            continue
        lanes[host]["status"] = "SCORED"
        lanes[host]["execution_kind"] = "live"
        lanes[host]["case_ids"].append(observation.get("case_id"))
    return lanes


def instrument_checks(
    pack: dict[str, Any],
    lanes: dict[str, dict[str, Any]],
    freeze: dict[str, Any] | None,
) -> dict[str, Any]:
    ids = [case["id"] for case in pack["cases"]]
    opencode = lanes["opencode"]
    checks = {
        "freeze_verified": bool(freeze and freeze.get("ok")),
        "required_scenarios_present": tuple(ids) == REQUIRED_CASE_IDS,
        "negative_controls_present": sum(case["class"] == "negative_control" for case in pack["cases"]) >= 2,
        "metrics_not_collapsed": True,
        "codex_live_trace": lanes["codex"]["status"] == "SCORED",
        "opencode_explicit": opencode["status"] == "SCORED"
        or (opencode["status"] == "NOT_RUN" and bool(opencode["runnable_path"])),
    }
    return {"checks": checks, "pass": all(checks.values())}


def _decision_failures(
    case: dict[str, Any],
    observation: dict[str, Any],
    recalls: list[dict[str, Any]],
    amb_calls: list[dict[str, Any]],
    base: dict[str, Any],
) -> list[str]:
    decision = case["expected_decision"]
    expected_ns = case.get("expected_namespace")
    reasons: list[str] = []
    matching = [call for call in recalls if call["namespace"] == expected_ns]
    if decision == "must_recall":
        base["required_recall_hit"] = bool(recalls)
        if not matching:
            reasons.append("required_recall_miss" if not recalls else "namespace_miss")
        elif not any(_classify_result(call, case) == "useful_hit" for call in matching):
            reasons.append("useful_hit_miss")
    elif decision == "recall_and_reconcile":
        base["required_recall_hit"] = bool(recalls)
        stale = case.get("stale_marker") or ""
        current = case.get("current_marker") or ""
        final = str(observation.get("final_text") or "")
        base["stale_misuse"] = bool(stale and stale in final)
        if not matching:
            reasons.append("required_recall_miss" if not recalls else "namespace_miss")
        elif not any(_classify_result(call, case) == "useful_hit" for call in matching):
            reasons.append("useful_hit_miss")
        if current and current not in final:
            reasons.append("current_evidence_not_used")
        if base["stale_misuse"]:
            reasons.append("stale_misuse")
    elif decision == "must_recall_then_no_hit":
        base["required_recall_hit"] = bool(recalls)
        if not matching:
            reasons.append("required_recall_miss" if not recalls else "namespace_miss")
            base["no_hit_correct"] = False
        else:
            states = [_classify_result(call, case) for call in matching]
            base["no_hit_correct"] = states == ["no_hit"] or (states and all(state == "no_hit" for state in states))
            if any(call["is_error"] for call in matching):
                reasons.append("unavailable_treated_as_empty")
                base["no_hit_correct"] = False
            elif not base["no_hit_correct"]:
                reasons.append("no_hit_mismatch")
    elif decision == "recall_or_report_unavailable":
        successful = [call for call in recalls if not call["is_error"]]
        discriminated = not successful
        base["unavailable_discriminated"] = discriminated
        if successful:
            reasons.append("unavailable_treated_as_empty")
            base["unavailable_discriminated"] = False
    elif decision in {"do_not_recall", "do_not_repeat"}:
        activated = bool(amb_calls)
        base["false_activation"] = activated
        if activated:
            reasons.append("false_activation" if decision == "do_not_recall" else "repeated_or_unnecessary_recall")
    else:
        reasons.append("unknown_decision")
    return reasons


def _text_failures(case: dict[str, Any], final: str) -> list[str]:
    reasons: list[str] = []
    for marker in case.get("final_text_must_include") or []:
        if marker not in final:
            reasons.append(f"missing_required_text:{marker}")
    for marker in case.get("final_text_must_not_include") or []:
        if marker and marker in final:
            reasons.append(f"forbidden_text:{marker}")
    options = case.get("final_text_must_match_any") or []
    if options and not any(option.casefold() in final.casefold() for option in options):
        reasons.append("missing_status_phrase")
    return reasons


def _repo_failures(case: dict[str, Any], observation: dict[str, Any]) -> list[str]:
    if "repo_paths_read" not in observation:
        return ["repo_evidence_missing"]
    required = str(case["repo_evidence_path"])
    paths = observation.get("repo_paths_read") or []
    if not isinstance(paths, list) or not _saw_path([str(path) for path in paths], required):
        return ["repo_evidence_missing"]
    return []


def _extra_recalls(case: dict[str, Any], recall_count: int) -> int:
    allowed = int(case["max_recall_calls"])
    if case["expected_decision"] in {"do_not_recall", "do_not_repeat"}:
        return recall_count
    return max(0, recall_count - allowed)


def _classify_result(call: dict[str, Any], case: dict[str, Any]) -> str:
    if call["is_error"]:
        return "unavailable"
    text = call["result_text"]
    count = call.get("result_count")
    if count == 0 or (count is None and not text.strip()):
        return "no_hit"
    useful = case.get("useful_marker") or ""
    distractor = case.get("distractor_marker") or ""
    if useful and useful in text:
        return "useful_hit"
    if text.strip() or (distractor and distractor in text):
        return "irrelevant_hit"
    return "no_hit"


def _normalize_call(call: dict[str, Any]) -> dict[str, Any]:
    return {
        "tool": _normalize_tool_name(str(call.get("tool") or "")),
        "namespace": str(call.get("namespace") or ""),
        "is_error": bool(call.get("is_error")),
        "result_text": str(call.get("result_text") or ""),
        "result_count": call.get("result_count"),
        "elapsed_ms": call.get("elapsed_ms"),
    }


def _normalize_tool_name(name: str) -> str:
    leaf = name.strip().replace("/", ".").split(".")[-1]
    if "__" in leaf:
        leaf = leaf.split("__")[-1]
    return leaf


def _saw_path(paths: list[str], required: str) -> bool:
    needle = required.replace("\\", "/").lstrip("./")
    for path in paths:
        cleaned = path.replace("\\", "/").lstrip("./")
        if cleaned == needle or cleaned.endswith("/" + needle):
            return True
    return False


def _result_shell(case: dict[str, Any], observation: dict[str, Any] | None) -> dict[str, Any]:
    host = (observation or {}).get("host") or {}
    return {
        "id": case["id"],
        "scenario": case["scenario"],
        "class": case["class"],
        "expected_decision": case["expected_decision"],
        "status": "INCONCLUSIVE",
        "reasons": [],
        "recall_calls": 0,
        "amb_calls": 0,
        "namespace_results": [],
        "result_states": [],
        "required_recall_hit": None,
        "false_activation": None,
        "no_hit_correct": None,
        "unavailable_discriminated": None,
        "stale_misuse": None,
        "repeated_recall_count": 0,
        "pathological_recall": False,
        "latency_ms": None if observation is None else observation.get("latency_ms"),
        "input_tokens": None if observation is None else observation.get("input_tokens"),
        "output_tokens": None if observation is None else observation.get("output_tokens"),
        "execution_kind": None if observation is None else observation.get("execution_kind"),
        "host_id": host.get("id"),
        "condition": None if observation is None else observation.get("condition"),
    }


def _not_run(case: dict[str, Any]) -> dict[str, Any]:
    result = _result_shell(case, None)
    result["status"] = "NOT_RUN"
    result["reasons"] = ["observation_missing"]
    return result


def _conclude(result: dict[str, Any], status: str, reasons: list[str]) -> dict[str, Any]:
    result["status"] = status
    result["reasons"] = list(dict.fromkeys(reasons))
    decision = result["expected_decision"]
    if status == "INCONCLUSIVE":
        result["required_recall_hit"] = None
        result["false_activation"] = None
        result["no_hit_correct"] = None
        result["unavailable_discriminated"] = None
        result["stale_misuse"] = None
    elif decision == "must_recall_then_no_hit" and result["no_hit_correct"] is None:
        result["no_hit_correct"] = status == "PASS"
    elif decision == "recall_or_report_unavailable" and result["unavailable_discriminated"] is None:
        result["unavailable_discriminated"] = status == "PASS"
    return result


def _flag_rate(
    results: list[dict[str, Any]],
    decision: str | None,
    flag: str,
    *,
    negative_only: bool = False,
) -> dict[str, Any]:
    selected = []
    for result in results:
        if decision is not None and result["expected_decision"] != decision:
            continue
        if negative_only and result["class"] != "negative_control":
            continue
        selected.append(result)
    passed = sum(result[flag] is True for result in selected)
    failed = sum(result[flag] is False for result in selected)
    inconclusive = sum(result["status"] == "INCONCLUSIVE" for result in selected)
    not_run = sum(result["status"] == "NOT_RUN" for result in selected)
    denominator = passed + failed
    return {
        "numerator": passed,
        "complement": failed,
        "inconclusive": inconclusive,
        "not_run": not_run,
        "rate": None if denominator == 0 else passed / denominator,
    }


def _status_rate(results: list[dict[str, Any]], decision: str) -> dict[str, Any]:
    selected = [result for result in results if result["expected_decision"] == decision]
    passed = sum(result["status"] == "PASS" for result in selected)
    failed = sum(result["status"] == "FAIL" for result in selected)
    inconclusive = sum(result["status"] == "INCONCLUSIVE" for result in selected)
    not_run = sum(result["status"] == "NOT_RUN" for result in selected)
    denominator = passed + failed
    return {
        "numerator": passed,
        "complement": failed,
        "inconclusive": inconclusive,
        "not_run": not_run,
        "rate": None if denominator == 0 else passed / denominator,
    }


def _namespace_rate(results: list[dict[str, Any]]) -> dict[str, Any]:
    correct = 0
    incorrect = 0
    inconclusive = 0
    not_run = 0
    for result in results:
        if result["status"] == "INCONCLUSIVE":
            inconclusive += 1
        elif result["status"] == "NOT_RUN":
            not_run += 1
        else:
            correct += sum(item == "correct" for item in result["namespace_results"])
            incorrect += sum(item == "incorrect" for item in result["namespace_results"])
    denominator = correct + incorrect
    return {
        "numerator": correct,
        "complement": incorrect,
        "inconclusive": inconclusive,
        "not_run": not_run,
        "rate": None if denominator == 0 else correct / denominator,
    }


def _useful_rate(results: list[dict[str, Any]]) -> dict[str, Any]:
    useful = 0
    other = 0
    inconclusive = 0
    not_run = 0
    for result in results:
        if result["expected_decision"] not in {"must_recall", "recall_and_reconcile"}:
            continue
        if result["status"] == "INCONCLUSIVE":
            inconclusive += 1
        elif result["status"] == "NOT_RUN":
            not_run += 1
        else:
            useful += sum(state == "useful_hit" for state in result["result_states"])
            other += sum(state != "useful_hit" for state in result["result_states"])
    denominator = useful + other
    return {
        "numerator": useful,
        "complement": other,
        "inconclusive": inconclusive,
        "not_run": not_run,
        "rate": None if denominator == 0 else useful / denominator,
    }
