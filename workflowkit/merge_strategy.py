from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence


MERGE_STRATEGIES = ("merge", "squash", "rebase")


@dataclass(frozen=True)
class MergeStrategyResolution:
    outcome: str
    code: str
    strategy: str | None
    candidates: tuple[str, ...]
    reason: str

    def as_json(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome,
            "code": self.code,
            "strategy": self.strategy,
            "candidates": list(self.candidates),
            "reason": self.reason,
        }


def resolve_merge_strategy(
    policy: str,
    enabled_methods: Sequence[str],
    *,
    required_linear_history: bool = False,
    merge_queue_method: str | None = None,
) -> MergeStrategyResolution:
    normalized_policy = policy.strip().lower()
    if normalized_policy not in {"auto", *MERGE_STRATEGIES}:
        return blocked(
            "invalid_policy",
            f"Unsupported merge policy: {policy!r}.",
        )

    normalized_methods = {
        method.strip().lower()
        for method in enabled_methods
        if isinstance(method, str) and method.strip()
    }
    unknown_methods = normalized_methods.difference(MERGE_STRATEGIES)
    if unknown_methods:
        return blocked(
            "invalid_repository_capabilities",
            "Repository capabilities contain unsupported merge methods: "
            + ", ".join(sorted(unknown_methods)),
        )

    candidates = set(normalized_methods)
    if required_linear_history:
        candidates.discard("merge")

    normalized_queue_method = None
    if merge_queue_method is not None:
        normalized_queue_method = merge_queue_method.strip().lower()
        if normalized_queue_method not in MERGE_STRATEGIES:
            return blocked(
                "invalid_merge_queue_method",
                f"Unsupported merge-queue method: {merge_queue_method!r}.",
            )
        candidates.intersection_update({normalized_queue_method})

    ordered_candidates = ordered(candidates)
    if normalized_policy != "auto":
        if normalized_policy in candidates:
            return MergeStrategyResolution(
                outcome="resolved",
                code="resolved",
                strategy=normalized_policy,
                candidates=(normalized_policy,),
                reason=f"Explicit {normalized_policy} policy is compatible.",
            )
        return blocked(
            "explicit_strategy_incompatible",
            f"Explicit {normalized_policy} policy is incompatible with "
            "repository, branch, or merge-queue constraints.",
            ordered_candidates,
        )

    if len(ordered_candidates) == 1:
        strategy = ordered_candidates[0]
        return MergeStrategyResolution(
            outcome="resolved",
            code="resolved",
            strategy=strategy,
            candidates=ordered_candidates,
            reason=f"Auto policy resolves uniquely to {strategy}.",
        )
    if not ordered_candidates:
        queue_context = (
            f" and merge queue method {normalized_queue_method}"
            if normalized_queue_method
            else ""
        )
        return blocked(
            "no_compatible_strategy",
            "No merge strategy satisfies repository and branch constraints"
            f"{queue_context}.",
        )
    return blocked(
        "ambiguous_strategy",
        "Auto policy remains ambiguous after applying all known constraints.",
        ordered_candidates,
    )


def resolve_github_merge_strategy(
    policy: str,
    payload: Mapping[str, Any],
) -> MergeStrategyResolution:
    repository = payload.get("repository")
    if not isinstance(repository, Mapping):
        return blocked(
            "invalid_github_payload",
            "GitHub payload must contain a repository object.",
        )

    capability_fields = {
        "merge": ("allow_merge_commit", "mergeCommitAllowed"),
        "squash": ("allow_squash_merge", "squashMergeAllowed"),
        "rebase": ("allow_rebase_merge", "rebaseMergeAllowed"),
    }
    enabled_methods = []
    for method, fields in capability_fields.items():
        value = first_value(repository, fields)
        if not isinstance(value, bool):
            return blocked(
                "invalid_github_payload",
                f"GitHub repository capability for {method} is missing.",
            )
        if value:
            enabled_methods.append(method)

    required_linear_history = github_requires_linear_history(payload)
    queue_methods = github_merge_queue_methods(payload)
    if len(queue_methods) > 1:
        return blocked(
            "conflicting_merge_queue_methods",
            "Applicable GitHub merge-queue rules disagree: "
            + ", ".join(sorted(queue_methods)),
        )
    queue_method = next(iter(queue_methods), None)

    return resolve_merge_strategy(
        policy,
        enabled_methods,
        required_linear_history=required_linear_history,
        merge_queue_method=queue_method,
    )


def github_requires_linear_history(payload: Mapping[str, Any]) -> bool:
    branch_protection = payload.get("branch_protection")
    if isinstance(branch_protection, Mapping):
        graph_value = branch_protection.get("requiresLinearHistory")
        if graph_value is True:
            return True
        rest_value = branch_protection.get("required_linear_history")
        if rest_value is True:
            return True
        if isinstance(rest_value, Mapping) and rest_value.get("enabled") is True:
            return True

    for rule in active_github_rules(payload):
        rule_type = str(rule.get("type", "")).replace("-", "_").lower()
        if rule_type in {"required_linear_history", "requiredlinearhistory"}:
            return True
    return False


def github_merge_queue_methods(payload: Mapping[str, Any]) -> set[str]:
    methods: set[str] = set()
    direct = payload.get("merge_queue")
    if isinstance(direct, Mapping):
        method = first_value(direct, ("merge_method", "mergeMethod"))
        if isinstance(method, str) and method.strip():
            methods.add(method.strip().lower())

    for rule in active_github_rules(payload):
        rule_type = str(rule.get("type", "")).replace("-", "_").lower()
        if rule_type not in {"merge_queue", "mergequeue"}:
            continue
        parameters = rule.get("parameters")
        if not isinstance(parameters, Mapping):
            continue
        method = first_value(parameters, ("merge_method", "mergeMethod"))
        if isinstance(method, str) and method.strip():
            methods.add(method.strip().lower())
    return methods


def active_github_rules(
    payload: Mapping[str, Any],
) -> tuple[Mapping[str, Any], ...]:
    rulesets = payload.get("rulesets", ())
    if not isinstance(rulesets, Sequence) or isinstance(rulesets, (str, bytes)):
        return ()

    rules = []
    for ruleset in rulesets:
        if not isinstance(ruleset, Mapping):
            continue
        enforcement = str(ruleset.get("enforcement", "active")).lower()
        if enforcement != "active":
            continue
        values = ruleset.get("rules", ())
        if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
            continue
        rules.extend(value for value in values if isinstance(value, Mapping))
    return tuple(rules)


def first_value(
    mapping: Mapping[str, Any],
    fields: Sequence[str],
) -> Any:
    for field in fields:
        if field in mapping:
            return mapping[field]
    return None


def ordered(methods: set[str]) -> tuple[str, ...]:
    return tuple(method for method in MERGE_STRATEGIES if method in methods)


def blocked(
    code: str,
    reason: str,
    candidates: tuple[str, ...] = (),
) -> MergeStrategyResolution:
    return MergeStrategyResolution(
        outcome="needs_user_action",
        code=code,
        strategy=None,
        candidates=candidates,
        reason=reason,
    )
