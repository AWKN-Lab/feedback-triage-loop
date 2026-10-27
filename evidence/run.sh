#!/usr/bin/env bash
# Reproducible evidence run for the feedback-triage loop.
#
#   bash evidence/run.sh
#
# Walks the whole loop, in the order the task's acceptance criteria name it:
#   1. 反馈入队   enqueue   — failed trajectories are classified and queued
#   2. 审核       review    — the policy decides who may clear what
#   3. 版本变更   change    — approved items become a versioned change set
#   4. 回归闭环   regress   — the candidate is checked against a labelled
#                              suite and applied only if it holds
#
# Everything is written to evidence/run.log.
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(dirname "$HERE")"
PY="${PYTHON:-python3}"
LOG="$HERE/run.log"
STATE="$HERE/state.json"

: > "$LOG"
run() {
  echo "\$ $*" >> "$LOG"
  "$@" >> "$LOG" 2>&1
  local rc=$?
  echo "[exit $rc]" >> "$LOG"
  echo "" >> "$LOG"
  return $rc
}
step() { echo "── $* ──" >> "$LOG"; }

cd "$ROOT"

step "environment"
echo "python: $($PY -V 2>&1)" >> "$LOG"
echo "pyyaml: $($PY -c 'import yaml; print(yaml.__version__)' 2>&1)" >> "$LOG"
echo "date:   $(date -u +%Y-%m-%dT%H:%M:%SZ)" >> "$LOG"
echo "" >> "$LOG"

rm -rf "$HERE/state.json" "$HERE/candidate"

step "1. ENQUEUE — 反馈入队"
run "$PY" triage/cli.py ingest --trajectories examples/trajectories.json --state "$STATE"

step "2. REVIEW — 审核"
run "$PY" triage/cli.py review --state "$STATE" --policy config/policy.yaml

step "3. VERSION CHANGE — 版本变更"
run "$PY" triage/cli.py change --state "$STATE" --taxonomy config/taxonomy.yaml --policy config/policy.yaml

step "4. REGRESSION — 回归闭环"
run "$PY" triage/cli.py regress --state "$STATE" --suite examples/regression-suite.yaml \
    --taxonomy config/taxonomy.yaml --policy config/policy.yaml

step "the versioned configuration that was produced"
run cat config/taxonomy.v1.1.0.yaml

step "final state"
run "$PY" triage/cli.py status --state "$STATE"

echo "log: $LOG" >> "$LOG"
tail -n 34 "$LOG"
