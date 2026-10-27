#!/usr/bin/env python3
"""Executable tests for the feedback-triage loop.

The acceptance criteria are: enqueue, review, version change, and a regression
closed loop. These tests cover all four — and the failure paths, because a
gate that cannot fail is not a gate.

    python3 checks/test_triage.py
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

from triage.core import (  # noqa: E402
    QueueItem, TriageError, apply_change_set, apply_review, build_change_set,
    classify, compare, enqueue, load_yaml, regression, review, validate_item,
)

CONFIG = ROOT / "config"
PY = sys.executable


class T:
    def __init__(self):
        self.passed = 0
        self.failed = 0

    def check(self, name, ok, detail=""):
        if ok:
            print(f"  [PASS] {name}")
            self.passed += 1
        else:
            print(f"  [FAIL] {name}" + (f" — {detail}" if detail else ""))
            self.failed += 1
        return ok


def main() -> int:
    t = T()
    taxonomy = load_yaml(CONFIG / "taxonomy.yaml")
    policy = load_yaml(CONFIG / "policy.yaml")
    schema = load_yaml(CONFIG / "queue-schema.yaml")
    trajectories = json.loads((ROOT / "examples/trajectories.json").read_text(encoding="utf-8"))
    suite = load_yaml(ROOT / "examples/regression-suite.yaml")

    # ------------------------------------------------ enqueue
    items = enqueue(trajectories, taxonomy, schema)
    t.check("every trajectory becomes a queue item", len(items) == len(trajectories),
            f"{len(items)} of {len(trajectories)}")
    counts: dict[str, int] = {}
    for i in items:
        counts[i.queue] = counts.get(i.queue, 0) + 1
    t.check("items land in the three improvement queues",
            set(counts) == {"knowledge", "prompt", "model"}, str(counts))
    t.check("every item starts pending",
            all(i.status == "pending" for i in items))
    t.check("every item keeps the trajectory's full signal set",
            all(i.signals for i in items))

    # a trajectory whose signals tie between two queues is flagged, not
    # silently decided by the dictionary order
    tie = [tr for tr in trajectories if tr["id"] in ("t-0002", "t-0007")]
    tie_items = enqueue(tie, taxonomy, schema)
    t.check("a tied routing is flagged for a human",
            all(i.needs_human for i in tie_items),
            str([(i.id, i.queue, i.needs_human) for i in tie_items]))

    # ------------------------------------------------ the enqueue gate refuses bad items
    for name, bad in [
        ("a missing required field", {"id": "x", "trajectory_id": "t", "queue": "knowledge",
                                      "count": 1, "cost": "low",
                                      "status": "pending", "proposed_by": "triage"}),
        ("an invalid status", {"id": "x", "trajectory_id": "t", "queue": "knowledge",
                               "signals": ["a"], "count": 1, "cost": "low",
                               "status": "wat", "proposed_by": "triage"}),
        ("an invalid queue", {"id": "x", "trajectory_id": "t", "queue": "whatever",
                              "signals": ["a"], "count": 1, "cost": "low",
                              "status": "pending", "proposed_by": "triage"}),
        ("a non-positive count", {"id": "x", "trajectory_id": "t", "queue": "knowledge",
                                  "signals": ["a"], "count": 0, "cost": "low",
                                  "status": "pending", "proposed_by": "triage"}),
    ]:
        refused = False
        try:
            validate_item(bad, schema)
        except TriageError:
            refused = True
        t.check(f"the queue schema refuses {name}", refused)

    # ------------------------------------------------ review
    outcomes = review(items, policy)
    apply_review(items, outcomes)
    by_id = {o.item_id: o for o in outcomes}
    model_items = [i for i in items if i.queue == "model"]
    t.check("model-queue items are escalated to a human",
            all(by_id[i.id].decision == "escalated" for i in model_items),
            str([(i.id, by_id[i.id].decision) for i in model_items]))
    t.check("ambiguous items are escalated to a human",
            all(by_id[i.id].decision == "escalated" for i in items if i.needs_human))
    approved = [i for i in items if i.status == "approved"]
    t.check("cheap, unambiguous items are approved by the agent reviewer",
            len(approved) > 0, "nothing was approved")
    t.check("no escalated item is ever approved",
            all(i.status != "approved" for i in items if i.needs_human or i.queue == "model"))

    # the agent reviewer's own limits are enforced
    strict = json.loads(json.dumps(policy))
    strict["reviewers"]["agent"]["may_approve_queues"] = []
    strict_outcomes = review([QueueItem.from_dict(i.to_dict()) for i in items], strict)
    t.check("an item outside the reviewer's queues is escalated, not approved",
            all(o.decision != "approved" for o in strict_outcomes),
            str([(o.item_id, o.decision) for o in strict_outcomes]))

    # ------------------------------------------------ version change
    change = build_change_set(items, taxonomy, policy, taxonomy["version"])
    t.check("the change set bumps the version", change.version == "1.1.0", change.version)
    t.check("the change set only contains approved items",
            all(i in [x.id for x in items if x.status == "approved"] for i in change.items))
    t.check("the mis-mapped signal is moved, not duplicated",
            any(m["signal"] == "stale_policy" and m["from"] == "prompt"
                and m["to"] == "knowledge" for m in change.rule_moves),
            str(change.rule_moves))
    cand_tax, cand_pol = apply_change_set(change, taxonomy, policy)
    t.check("the candidate taxonomy claims stale_policy in the knowledge queue",
            "stale_policy" in cand_tax["queues"]["knowledge"]["signals"])
    t.check("the candidate taxonomy no longer claims it in the prompt queue",
            "stale_policy" not in cand_tax["queues"]["prompt"]["signals"])
    t.check("the candidate carries the new version",
            str(cand_tax["version"]) == "1.1.0", str(cand_tax.get("version")))

    # ------------------------------------------------ regression
    cases = suite["cases"]
    baseline = regression(cases, taxonomy)
    candidate = regression(cases, cand_tax)
    verdict = compare(baseline, candidate)
    t.check("the baseline fails the mis-mapped case", baseline["passed"] == len(cases) - 1,
            f"{baseline['passed']}/{baseline['total']}")
    t.check("the candidate passes every case", candidate["passed"] == candidate["total"],
            f"{candidate['passed']}/{candidate['total']}")
    t.check("the change fixes the case it was built to fix", verdict["fixed"] == ["c04"],
            str(verdict["fixed"]))
    t.check("the change regresses nothing", verdict["regressions"] == [], str(verdict["regressions"]))
    t.check("the verdict accepts the change", verdict["acceptable"] is True)

    # a candidate that breaks a previously passing case must be rejected
    broken = json.loads(json.dumps(cand_tax))
    broken["queues"]["knowledge"]["signals"] = []
    broken["queues"]["prompt"]["signals"] = ["no_faq_entry"]
    broken_result = regression(cases, broken)
    broken_verdict = compare(baseline, broken_result)
    t.check("a candidate that breaks a passing case is not acceptable",
            broken_verdict["acceptable"] is False and bool(broken_verdict["regressions"]),
            str(broken_verdict)[:200])

    # a candidate that changes nothing is not acceptable either
    noop_verdict = compare(baseline, baseline)
    t.check("a candidate that fixes nothing is not acceptable", noop_verdict["acceptable"] is False)

    # ------------------------------------------------ the committed versioned config
    applied = CONFIG / "taxonomy.v1.1.0.yaml"
    t.check("the applied versioned config is committed", applied.is_file(), str(applied))
    if applied.is_file():
        a = load_yaml(applied)
        t.check("the committed version claims stale_policy in knowledge",
                "stale_policy" in a["queues"]["knowledge"]["signals"])
        t.check("the committed version does not claim it in prompt",
                "stale_policy" not in a["queues"]["prompt"]["signals"])

    print(f"\n== {t.passed} passed, {t.failed} failed")
    return 1 if t.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
