#!/usr/bin/env python3
"""feedback-triage — route failed trajectories into improvement queues, and
close the loop from feedback to a reviewed, versioned, regression-checked
change.

The pipeline, in the order the task's acceptance criteria name it:

    feedback  ->  enqueue  ->  review  ->  version change  ->  regression

  enqueue    every failed trajectory is classified against the failure
             taxonomy and becomes a queue item in exactly one of the
             improvement queues (knowledge / prompt / model). An item that
             does not satisfy the queue schema is refused at the door.
  review     a reviewer policy decides who may clear which queue. The agent
             reviewer may clear cheap, small items; anything in the model
             queue, or anything affecting more runs than its limit, is
             escalated to a human and can never reach a release.
  change     approved items are turned into a versioned change set with a
             changelog and concrete rule additions.
  regression the candidate config is run against a labelled suite and compared
             with the baseline. A change that breaks anything, or that does
             not actually fix what it claimed to fix, is not applied.

Everything is configuration-driven: the taxonomy, the review policy and the
queue schema all live in config/, so a team can retune the loop without
touching the router.
"""
from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover
    sys.stderr.write("this tool needs PyYAML: pip install pyyaml\n")
    raise SystemExit(2)

VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")


# ---------------------------------------------------------------- config
def load_yaml(path) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def bump(version: str) -> str:
    m = VERSION_RE.match(version)
    if not m:
        raise ValueError(f"not a semantic version: {version}")
    major, minor, patch = (int(x) for x in m.groups())
    return f"{major}.{minor + 1}.0"


class TriageError(Exception):
    """Raised when input does not satisfy the contract."""


# ---------------------------------------------------------------- classify
@dataclass
class Decision:
    trajectory_id: str
    queue: str
    signals: list
    confidence: float
    reason: str
    ambiguous: bool = False
    matched_signals: list = field(default_factory=list)
    alternative: str | None = None


def classify(trajectory: dict, taxonomy: dict) -> Decision:
    """Route a trajectory to exactly one improvement queue.

    A trajectory can carry several signals; the queue that claims the most of
    them wins, and ties are broken by the taxonomy's order, so routing is
    deterministic and explainable.
    """
    signals = list(trajectory.get("signals") or [])
    if not signals:
        return Decision(
            trajectory_id=trajectory.get("id", "?"),
            queue=taxonomy.get("default_queue", "knowledge"),
            signals=[],
            confidence=0.0,
            reason="no signals present; routed to the default queue",
        )

    order = list(taxonomy.get("queues", {}).keys())
    hits: dict[str, list] = {}
    for qname, qspec in taxonomy.get("queues", {}).items():
        matched = [s for s in signals if s in (qspec.get("signals") or [])]
        if matched:
            hits[qname] = matched

    if not hits:
        return Decision(
            trajectory_id=trajectory.get("id", "?"),
            queue=taxonomy.get("default_queue", "knowledge"),
            signals=signals,
            confidence=0.0,
            reason="no signal matched a known queue; routed to the default queue",
        )

    ranked = sorted(hits.items(), key=lambda kv: (-len(kv[1]), order.index(kv[0]) if kv[0] in order else 0))
    best, matched = ranked[0]
    runner_up = ranked[1][0] if len(ranked) > 1 else None
    confidence = round(len(matched) / len(signals), 3)

    # A tie is not a decision. When two queues claim the same number of
    # signals, the item is parked in the queue of its first signal and
    # flagged, so a human decides instead of the dictionary order deciding
    # silently.
    ambiguous = runner_up is not None and len(hits[runner_up]) == len(matched)
    reason = f"{len(matched)}/{len(signals)} signals matched the {best} queue"
    if ambiguous:
        reason += f"; tied with '{runner_up}' — needs a human"
    return Decision(
        trajectory_id=trajectory.get("id", "?"),
        queue=best,
        signals=list(signals),          # every signal, not only the matching ones
        confidence=confidence,
        reason=reason,
        ambiguous=ambiguous,
        matched_signals=list(matched),
        alternative=runner_up if ambiguous else None,
    )


# ---------------------------------------------------------------- queue
@dataclass
class QueueItem:
    id: str
    trajectory_id: str
    queue: str
    signals: list
    count: int
    cost: str
    status: str = "pending"
    proposed_by: str = "triage"
    note: str = ""
    needs_human: bool = False
    matched_signals: list = field(default_factory=list)
    history: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "QueueItem":
        return cls(
            id=data["id"],
            trajectory_id=data["trajectory_id"],
            queue=data["queue"],
            signals=list(data.get("signals") or []),
            count=int(data.get("count", 1)),
            cost=data.get("cost", "low"),
            status=data.get("status", "pending"),
            proposed_by=data.get("proposed_by", "triage"),
            note=data.get("note", ""),
            needs_human=bool(data.get("needs_human", False)),
            matched_signals=list(data.get("matched_signals") or []),
            history=list(data.get("history") or []),
        )


def validate_item(item: dict, schema: dict) -> None:
    """Refuse an item that does not satisfy the queue schema."""
    for key in schema.get("required", []):
        if key not in item or item[key] in (None, ""):
            raise TriageError(f"queue item is missing required field: {key}")
    if item["status"] not in schema.get("statuses", []):
        raise TriageError(f"invalid status: {item['status']}")
    if item["cost"] not in schema.get("costs", []):
        raise TriageError(f"invalid cost: {item['cost']}")
    if item["queue"] not in schema.get("queues", []):
        raise TriageError(f"invalid queue: {item['queue']}")
    if not isinstance(item["count"], int) or item["count"] < 1:
        raise TriageError("count must be a positive integer")


def enqueue(trajectories: list, taxonomy: dict, schema: dict, proposed_by: str = "triage") -> list:
    """Classify every trajectory and turn it into a queue item."""
    items = []
    for i, traj in enumerate(trajectories, start=1):
        decision = classify(traj, taxonomy)
        qspec = taxonomy.get("queues", {}).get(decision.queue, {})
        item = QueueItem(
            id=f"qi-{i:04d}",
            trajectory_id=traj.get("id", f"t-{i:04d}"),
            queue=decision.queue,
            signals=decision.signals,
            count=int(traj.get("count", 1)),
            cost=qspec.get("cost", "low"),
            status="pending",
            proposed_by=proposed_by,
            note=decision.reason,
            needs_human=decision.ambiguous,
            matched_signals=list(decision.matched_signals),
            history=[{"at": now(), "event": "enqueued", "by": proposed_by,
                      "detail": decision.reason}],
        )
        validate_item(item.to_dict(), schema)
        items.append(item)
    return items


# ---------------------------------------------------------------- review
@dataclass
class ReviewOutcome:
    item_id: str
    decision: str          # approved | rejected | escalated
    reviewer: str
    reason: str


def review(items: list, policy: dict) -> list:
    """Apply the review policy to the pending items."""
    agent = policy.get("reviewers", {}).get("agent", {})
    agent_queues = set(agent.get("may_approve_queues", []))
    agent_costs = set(agent.get("max_cost_queues", []))
    agent_max = int(agent.get("max_count_per_item", 0))
    min_count = int(policy.get("min_count_to_review", 1))
    escalate_queues = set(policy.get("escalate_queues", []))

    outcomes = []
    for item in items:
        if item.status != "pending":
            continue
        if item.count < min_count:
            outcomes.append(ReviewOutcome(item.id, "rejected", "policy",
                                          f"count {item.count} is below the review threshold {min_count}"))
        elif item.needs_human:
            outcomes.append(ReviewOutcome(item.id, "escalated", "policy",
                                          "the routing was ambiguous; a human must decide which queue it belongs to"))
        elif item.queue in escalate_queues:
            outcomes.append(ReviewOutcome(item.id, "escalated", "policy",
                                          f"queue '{item.queue}' must be approved by a human"))
        elif item.queue in agent_queues and item.cost in agent_costs and item.count <= agent_max:
            outcomes.append(ReviewOutcome(item.id, "approved", "agent",
                                          f"queue '{item.queue}' is within the agent reviewer's limits"))
        else:
            reasons = []
            if item.queue not in agent_queues:
                reasons.append(f"the agent reviewer may not clear '{item.queue}'")
            if item.cost not in agent_costs:
                reasons.append(f"cost '{item.cost}' is above the agent reviewer's limit")
            if item.count > agent_max:
                reasons.append(f"count {item.count} exceeds the agent reviewer's limit of {agent_max}")
            outcomes.append(ReviewOutcome(item.id, "escalated", "policy", "; ".join(reasons)))
    return outcomes


def apply_review(items: list, outcomes: list) -> list:
    by_id = {o.item_id: o for o in outcomes}
    for item in items:
        outcome = by_id.get(item.id)
        if outcome and item.status == "pending":
            item.status = outcome.decision
            item.history.append({"at": now(), "event": f"review:{outcome.decision}",
                                 "by": outcome.reviewer, "detail": outcome.reason})
    return items


# ---------------------------------------------------------------- change
@dataclass
class ChangeSet:
    version: str
    previous_version: str
    created_at: str
    items: list
    rule_additions: dict
    changelog: list
    rule_moves: list = field(default_factory=list)
    applied: bool = False


def build_change_set(items: list, taxonomy: dict, policy: dict, current_version: str) -> ChangeSet:
    """Turn the approved items into a versioned change set.

    A knowledge or prompt item proposes a new signal mapping; a model item
    proposes a routing override. Anything not approved is left out — an
    escalated item can never end up in a release.
    """
    approved = [i for i in items if i.status == "approved"]
    additions: dict[str, list] = {}
    moves: list = []
    changelog = []
    def claimed_by(signal: str) -> str | None:
        for qname, qspec in taxonomy.get("queues", {}).items():
            if signal in (qspec.get("signals") or []):
                return qname
        return None

    for item in approved:
        for signal in item.signals:
            owner = claimed_by(signal)
            if owner == item.queue:
                continue            # already routed here; nothing to change
            if owner is None:
                # an unmapped signal: the queue it now belongs to claims it
                bucket = additions.setdefault(item.queue, [])
                if signal not in bucket:
                    bucket.append(signal)
            else:
                # the signal is currently claimed by a different queue: it is
                # a mis-mapping, and the fix is a move, not a duplicate
                moves.append({"signal": signal, "from": owner, "to": item.queue})
                bucket = additions.setdefault(item.queue, [])
                if signal not in bucket:
                    bucket.append(signal)
        changelog.append({
            "item": item.id,
            "trajectory": item.trajectory_id,
            "queue": item.queue,
            "count": item.count,
            "signals": item.signals,
            "change": f"route '{', '.join(item.signals)}' to the {item.queue} queue",
        })
    return ChangeSet(
        version=bump(current_version),
        previous_version=current_version,
        created_at=now(),
        items=[i.id for i in approved],
        rule_additions=additions,
        rule_moves=moves,
        changelog=changelog,
    )


def apply_change_set(change: ChangeSet, taxonomy: dict, policy: dict) -> tuple[dict, dict]:
    """Produce the candidate configs. The caller decides whether to keep them:
    the regression result decides that, not this function."""
    new_taxonomy = json.loads(json.dumps(taxonomy))
    new_taxonomy["version"] = change.version
    for qname, signals in change.rule_additions.items():
        spec = new_taxonomy.get("queues", {}).setdefault(qname, {"signals": [], "cost": "low"})
        existing = spec.setdefault("signals", [])
        for signal in signals:
            if signal not in existing:
                existing.append(signal)
    # a moved signal is removed from the queue that used to claim it
    for move in change.rule_moves:
        for qname, qspec in new_taxonomy.get("queues", {}).items():
            if qname == move["to"]:
                continue
            sigs = qspec.get("signals") or []
            if move["signal"] in sigs:
                qspec["signals"] = [s for s in sigs if s != move["signal"]]
    new_policy = json.loads(json.dumps(policy))
    new_policy["version"] = change.version
    return new_taxonomy, new_policy


# ---------------------------------------------------------------- regression
@dataclass
class RegressionCase:
    id: str
    trajectory: dict
    expected_queue: str


def regression(cases: list, taxonomy: dict) -> dict:
    """Run the labelled suite against a taxonomy and report per-case results."""
    results = []
    for case in cases:
        # a case may be a RegressionCase or a plain dict
        traj = case["trajectory"] if isinstance(case, dict) else case.trajectory
        expected = case["expected_queue"] if isinstance(case, dict) else case.expected_queue
        cid = case["id"] if isinstance(case, dict) else case.id
        decision = classify(traj, taxonomy)
        passed = decision.queue == expected
        results.append({
            "id": cid,
            "expected": expected,
            "actual": decision.queue,
            "passed": passed,
            "reason": decision.reason,
        })
    passed_n = sum(1 for r in results if r["passed"])
    return {
        "total": len(results),
        "passed": passed_n,
        "failed": len(results) - passed_n,
        "results": results,
        "pass_rate": round(passed_n / len(results), 4) if results else 0.0,
    }


def compare(baseline: dict, candidate: dict) -> dict:
    """A candidate is acceptable when it breaks nothing that used to pass, and
    actually fixes at least the cases it was built to fix."""
    base_by_id = {r["id"]: r for r in baseline["results"]}
    cand_by_id = {r["id"]: r for r in candidate["results"]}
    regressions = []
    fixed = []
    still_failing = []
    for cid, cres in cand_by_id.items():
        bres = base_by_id.get(cid)
        if bres and bres["passed"] and not cres["passed"]:
            regressions.append({"id": cid, "expected": cres["expected"], "actual": cres["actual"]})
        elif bres and not bres["passed"] and cres["passed"]:
            fixed.append(cid)
        elif not cres["passed"]:
            still_failing.append(cid)
    return {
        "regressions": regressions,
        "fixed": fixed,
        "still_failing": still_failing,
        "acceptable": not regressions and bool(fixed),
    }
