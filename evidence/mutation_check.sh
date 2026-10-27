#!/usr/bin/env bash
# Mutation proof: the gates in this loop must be able to FAIL.
#
# A green run only means something if broken input actually goes red. Each
# check below injects a defect and asserts that the loop catches it. The inner
# script exits 0 when the gate behaved correctly (it refused, escalated or
# rejected); a non-zero exit therefore means the gate let the defect through.
#
#   bash evidence/mutation_check.sh
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(dirname "$HERE")"
PY="${PYTHON:-python3}"

cd "$ROOT"
export PYTHONPATH="$ROOT"
pass=0
fail=0

# expect_catches <name> <inline python>
# The inline script must exit 0 when the gate caught the defect.
expect_catches() {
  local name="$1"; shift
  "$@" >/tmp/mt.out 2>&1
  local rc=$?
  if [ $rc -eq 0 ]; then
    echo "  [OK]   $name"
    pass=$((pass + 1))
  else
    echo "  [BAD]  $name (the defect was not caught, exit $rc)"
    sed -n '1,8p' /tmp/mt.out | sed 's/^/         /'
    fail=$((fail + 1))
  fi
}

echo "== mutation proof: the loop's gates must reject broken input"

# M1: a queue item with an invalid status must be refused at the door
expect_catches "M1 an item with an invalid status is refused" \
  "$PY" -c "
from triage.core import validate_item, load_yaml, TriageError
schema = load_yaml('config/queue-schema.yaml')
try:
    validate_item({'id':'x','trajectory_id':'t','queue':'knowledge','signals':['a'],
                   'count':1,'cost':'low','status':'wat','proposed_by':'t'}, schema)
except TriageError:
    raise SystemExit(0)
raise SystemExit(1)
"

# M2: a trajectory whose signals tie between two queues must be flagged rather
#     than silently decided by the dictionary order
expect_catches "M2 a tied routing is flagged instead of silently decided" \
  "$PY" -c "
from triage.core import classify, load_yaml
tax = load_yaml('config/taxonomy.yaml')
d = classify({'id':'t','signals':['unknown_product','hallucinated_value']}, tax)
raise SystemExit(0 if d.ambiguous else 1)
"

# M3: an item the reviewer may not clear must be escalated, never approved
expect_catches "M3 an item outside the reviewer's permissions is escalated" \
  "$PY" -c "
from triage.core import review, load_yaml, QueueItem
pol = load_yaml('config/policy.yaml')
pol['reviewers']['agent']['may_approve_queues'] = []
item = QueueItem(id='qi-1', trajectory_id='t', queue='knowledge', signals=['a'],
                 count=1, cost='low')
out = review([item], pol)
raise SystemExit(0 if all(o.decision != 'approved' for o in out) else 1)
"

# M4: a candidate that breaks a previously passing case must be rejected
expect_catches "M4 a candidate that regresses a passing case is rejected" \
  "$PY" -c "
import json
from triage.core import regression, compare, load_yaml
suite = load_yaml('examples/regression-suite.yaml')['cases']
tax = load_yaml('config/taxonomy.yaml')
broken = json.loads(json.dumps(tax))
broken['queues']['knowledge']['signals'] = []
broken['queues']['prompt']['signals'] = ['no_faq_entry']
v = compare(regression(suite, tax), regression(suite, broken))
raise SystemExit(0 if (not v['acceptable'] and v['regressions']) else 1)
"

# M5: an escalated item must never reach a release
expect_catches "M5 an escalated item never enters the change set" \
  "$PY" -c "
from triage.core import build_change_set, load_yaml, QueueItem
tax = load_yaml('config/taxonomy.yaml'); pol = load_yaml('config/policy.yaml')
item = QueueItem(id='qi-1', trajectory_id='t', queue='model', signals=['timeout'],
                 count=3, cost='high', status='escalated')
change = build_change_set([item], tax, pol, '1.0.0')
raise SystemExit(0 if (not change.items and not change.rule_additions and not change.rule_moves) else 1)
"

# M6: a change that fixes nothing must not be accepted
expect_catches "M6 a change that fixes nothing is not accepted" \
  "$PY" -c "
from triage.core import regression, compare, load_yaml
suite = load_yaml('examples/regression-suite.yaml')['cases']
tax = load_yaml('config/taxonomy.yaml')
v = compare(regression(suite, tax), regression(suite, tax))
raise SystemExit(0 if not v['acceptable'] else 1)
"

echo "== $pass behaved as expected, $fail did not"
if [ $fail -ne 0 ]; then
  echo "MUTATION PROOF: FAIL"
  exit 1
fi
echo "MUTATION PROOF: PASS (the gates can fail, so a green run means something)"
exit 0
