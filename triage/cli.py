#!/usr/bin/env python3
"""Command line for the feedback-triage loop.

    python3 triage/cli.py ingest  --trajectories examples/trajectories.json --state work/state.json
    python3 triage/cli.py review  --state work/state.json
    python3 triage/cli.py change  --state work/state.json --taxonomy config/taxonomy.yaml --policy config/policy.yaml
    python3 triage/cli.py regress --state work/state.json --suite examples/regression-suite.yaml
    python3 triage/cli.py status  --state work/state.json

Each command advances the loop one stage and writes the state file, so the
whole loop can be run, inspected and re-run step by step.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from triage.core import (  # noqa: E402
    QueueItem,
    TriageError, apply_change_set, apply_review, build_change_set, bump,
    classify, enqueue, load_yaml, now, regression, compare, review,
)

HERE = Path(__file__).resolve().parent.parent


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # the state file is plain JSON: queue items go back to dicts on the way out
    payload = dict(state)
    payload["items"] = [i.to_dict() if hasattr(i, "to_dict") else i
                         for i in state.get("items", [])]
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def load_state(path: Path) -> dict:
    if not path.exists():
        raise SystemExit(f"no state at {path}; run `ingest` first")
    state = json.loads(path.read_text(encoding="utf-8"))
    # the state file holds plain dicts; the pipeline works on QueueItem objects
    state["items"] = [QueueItem.from_dict(i) for i in state.get("items", [])]
    return state


def cmd_ingest(args) -> int:
    trajectories = json.loads(Path(args.trajectories).read_text(encoding="utf-8"))
    taxonomy = load_yaml(args.taxonomy)
    schema = load_yaml(args.schema)
    items = enqueue(trajectories, taxonomy, schema, proposed_by=args.proposed_by)
    state = {"items": [i.to_dict() for i in items], "outcomes": [], "changeset": None,
             "created_at": now()}
    save_state(Path(args.state), state)

    counts: dict[str, int] = {}
    for item in items:
        counts[item.queue] = counts.get(item.queue, 0) + 1
    print(f"ingested {len(trajectories)} trajectories -> {len(items)} queue item(s)")
    for q, n in sorted(counts.items()):
        print(f"  {q:<12} {n}")
    print(f"  state: {args.state}")
    return 0


def cmd_review(args) -> int:
    state = load_state(Path(args.state))
    policy = load_yaml(args.policy)
    items = state["items"]
    outcomes = review(items, policy)
    apply_review(items, outcomes)
    state["outcomes"] = [o.__dict__ for o in outcomes]
    save_state(Path(args.state), state)

    for o in outcomes:
        print(f"  {o.item_id}  {o.decision:<10} by {o.reviewer:<8} {o.reason}")
    counts: dict[str, int] = {}
    for o in outcomes:
        counts[o.decision] = counts.get(o.decision, 0) + 1
    print(f"  -> {counts}")
    return 0


def cmd_change(args) -> int:
    state = load_state(Path(args.state))
    taxonomy = load_yaml(args.taxonomy)
    policy = load_yaml(args.policy)
    current_version = taxonomy.get("version", "1.0.0")
    change = build_change_set(state["items"], taxonomy, policy, current_version)
    cand_taxonomy, cand_policy = apply_change_set(change, taxonomy, policy)

    cand_dir = Path(args.state).parent / "candidate"
    cand_dir.mkdir(parents=True, exist_ok=True)
    (cand_dir / "taxonomy.yaml").write_text(
        yaml_dump(cand_taxonomy), encoding="utf-8")
    (cand_dir / "policy.yaml").write_text(yaml_dump(cand_policy), encoding="utf-8")

    state["changeset"] = {**change.__dict__, "applied": False}
    save_state(Path(args.state), state)

    print(f"change set {change.previous_version} -> {change.version}")
    for entry in change.changelog:
        print(f"  {entry['item']}  {entry['queue']:<12} {entry['change']}")
    for move in change.rule_moves:
        print(f"  moved '{move['signal']}' from {move['from']} to {move['to']}")
    print(f"  candidate config: {cand_dir}")
    return 0


def yaml_dump(data) -> str:
    import yaml
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True)


def cmd_regress(args) -> int:
    state = load_state(Path(args.state))
    suite = load_yaml(args.suite)
    taxonomy = load_yaml(args.taxonomy)
    cand_dir = Path(args.state).parent / "candidate"
    cand_taxonomy = load_yaml(cand_dir / "taxonomy.yaml")
    cand_policy = load_yaml(cand_dir / "policy.yaml")

    cases = [
        {"id": c["id"], "trajectory": c["trajectory"], "expected_queue": c["expected_queue"]}
        for c in suite["cases"]
    ]
    baseline = regression(cases, taxonomy)
    candidate = regression(cases, cand_taxonomy)
    verdict = compare(baseline, candidate)

    print(f"regression suite: {len(cases)} case(s)")
    print(f"  baseline  : {baseline['passed']}/{baseline['total']} passed")
    print(f"  candidate : {candidate['passed']}/{candidate['total']} passed")
    print(f"  fixed     : {verdict['fixed']}")
    print(f"  regressions: {verdict['regressions']}")

    state["regression"] = {"baseline": baseline, "candidate": candidate, "verdict": verdict}
    if not verdict["acceptable"]:
        state["changeset"]["applied"] = False
        save_state(Path(args.state), state)
        print("  [FAIL] the change is not applied: it regresses something or fixes nothing")
        return 1

    # the change passed: write the versioned config into config/ — this is the
    # version change the task asks to be demonstrated
    change = state["changeset"]
    out_tax = Path(args.taxonomy).parent / f"taxonomy.v{change['version']}.yaml"
    out_pol = Path(args.policy).parent / f"policy.v{change['version']}.yaml"
    out_tax.write_text(yaml_dump(cand_taxonomy), encoding="utf-8")
    out_pol.write_text(yaml_dump(cand_policy), encoding="utf-8")
    change["applied"] = True
    change["applied_files"] = [str(out_tax), str(out_pol)]
    state["changeset"] = change
    save_state(Path(args.state), state)
    print(f"  [OK] change {change['version']} applied: {out_tax.name}, {out_pol.name}")
    return 0


def cmd_status(args) -> int:
    state = load_state(Path(args.state))
    items = state.get("items", [])
    print(f"  queue items : {len(items)}")
    counts: dict[str, int] = {}
    for i in items:
        status = i.status if hasattr(i, "status") else i["status"]
        counts[status] = counts.get(status, 0) + 1
    print(f"  by status   : {counts}")
    change = state.get("changeset")
    if change:
        print(f"  change set  : {change['previous_version']} -> {change['version']} "
              f"(applied={change.get('applied')})")
    reg = state.get("regression")
    if reg:
        print(f"  regression  : baseline {reg['baseline']['passed']}/{reg['baseline']['total']}, "
              f"candidate {reg['candidate']['passed']}/{reg['candidate']['total']}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    cfg = HERE / "config"

    p = sub.add_parser("ingest", help="classify trajectories and enqueue them")
    p.add_argument("--trajectories", default="examples/trajectories.json")
    p.add_argument("--taxonomy", default=str(cfg / "taxonomy.yaml"))
    p.add_argument("--schema", default=str(cfg / "queue-schema.yaml"))
    p.add_argument("--proposed-by", default="triage")
    p.add_argument("--state", default="work/state.json")
    p.set_defaults(func=cmd_ingest)

    p = sub.add_parser("review", help="apply the review policy")
    p.add_argument("--policy", default=str(cfg / "policy.yaml"))
    p.add_argument("--state", default="work/state.json")
    p.set_defaults(func=cmd_review)

    p = sub.add_parser("change", help="build the versioned change set")
    p.add_argument("--taxonomy", default=str(cfg / "taxonomy.yaml"))
    p.add_argument("--policy", default=str(cfg / "policy.yaml"))
    p.add_argument("--state", default="work/state.json")
    p.set_defaults(func=cmd_change)

    p = sub.add_parser("regress", help="run the regression suite and apply the change if it holds")
    p.add_argument("--suite", default="examples/regression-suite.yaml")
    p.add_argument("--taxonomy", default=str(cfg / "taxonomy.yaml"))
    p.add_argument("--policy", default=str(cfg / "policy.yaml"))
    p.add_argument("--state", default="work/state.json")
    p.set_defaults(func=cmd_regress)

    p = sub.add_parser("status", help="show the state")
    p.add_argument("--state", default="work/state.json")
    p.set_defaults(func=cmd_status)

    args = ap.parse_args()
    try:
        return args.func(args)
    except TriageError as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
