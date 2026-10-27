# feedback-triage — close the loop from failed trajectories to a reviewed,
# versioned, regression-checked change

A triage and feedback loop for an agent system: failed runs are classified
into improvement queues, reviewed under an explicit policy, turned into a
versioned change, and only released if a regression suite says the change holds.

This is the delivery for **ClawHunt task #1535** — "企业 Agent 系统 48/50 ｜
学习回流与数据飞轮" (budget USD 20, escrow funded). The task's acceptance
criteria are: *演示反馈入队、审核、版本变更及回归闭环* — demonstrate the loop of
feedback enqueue, review, version change and regression. All four stages are in
`evidence/run.log`, produced by one command.

---

## 1. The loop

```
failed trajectories
      │
      ▼
  ┌────────┐   classify against the failure taxonomy, enqueue as an item
  │ ENQUEUE│   (an item that breaks the queue schema is refused)
  └────────┘
      │
      ▼
  ┌────────┐   the review policy decides who may clear what:
  │ REVIEW │   cheap + unambiguous → the agent reviewer
  └────────┘   model queue / ambiguous / over the count limit → a human
      │
      ▼
  ┌────────┐   approved items become a versioned change set:
  │ CHANGE │   new version, changelog, concrete rule additions and moves
  └────────┘
      │
      ▼
  ┌──────────┐ the candidate is run against a labelled suite and compared
  │ REGRESSION│ with the baseline. It is applied only when it breaks nothing
  └──────────┘ and actually fixes what it was built to fix.
```

Everything is configuration-driven: `config/taxonomy.yaml` (routing),
`config/policy.yaml` (review), `config/queue-schema.yaml` (item contract).

## 2. The four stages, as demonstrated

`bash evidence/run.sh` → `evidence/run.log`:

**1. Enqueue (反馈入队)** — 8 desensitised failed trajectories are classified:

```
ingested 8 trajectories -> 8 queue item(s)
  knowledge    4
  model        2
  prompt       2
```

**2. Review (审核)** — the policy, not a human's mood, decides:

```
qi-0001  approved   by agent    queue 'knowledge' is within the agent reviewer's limits
qi-0002  escalated  by policy   the routing was ambiguous; a human must decide which queue it belongs to
qi-0005  escalated  by policy   queue 'model' must be approved by a human
  -> {'approved': 4, 'escalated': 4}
```

**3. Version change (版本变更)** — only approved items become a change set:

```
change set 1.0.0 -> 1.1.0
  qi-0001  knowledge    route 'stale_policy, missing_answer, no_faq_entry' to the knowledge queue
  moved 'stale_policy' from prompt to knowledge
```

**4. Regression (回归闭环)** — the change is applied only if it holds:

```
regression suite: 8 case(s)
  baseline  : 7/8 passed
  candidate : 8/8 passed
  fixed     : ['c04']
  regressions: []
  [OK] change 1.1.0 applied: taxonomy.v1.1.0.yaml, policy.v1.1.0.yaml
```

### The demo has a real defect in it, on purpose

The shipped baseline taxonomy files `stale_policy` under **prompt**, although a
stale policy is a **knowledge** problem. That is the kind of drift every team
has. The loop finds it: a trajectory carrying `stale_policy` alongside two
knowledge signals outvotes the prompt claim, the reviewer approves the
knowledge change, and the change set **moves** the signal rather than
duplicating it (two queues claiming one signal makes routing ambiguous).

The regression suite proves the move: case `c04` (`[stale_policy]` →
knowledge) fails on the baseline and passes on the candidate, and no other case
moves.

A second rule worth pointing at: when two queues claim the **same number** of
signals, the routing is ambiguous, and the item is flagged for a human rather
than decided by dictionary order. In the demo that is `qi-0002` and `qi-0007`.

## 3. How to use it

```bash
pip install pyyaml            # the only dependency

python3 triage/cli.py ingest  --trajectories examples/trajectories.json --state work/state.json
python3 triage/cli.py review  --state work/state.json
python3 triage/cli.py change  --state work/state.json
python3 triage/cli.py regress --state work/state.json
python3 triage/cli.py status  --state work/state.json
```

To run the loop over your own runs: export the trajectories in the shape of
`examples/trajectories.json` (id, intent, outcome, signals, count — all
desensitised), point `--taxonomy` at your taxonomy, and go.

## 4. Verification record

`python3 checks/test_triage.py` — **30/30 passed**:

| area | what is asserted |
|---|---|
| enqueue | every trajectory becomes an item; items land in the three queues; every item keeps the trajectory's **full** signal set (a signal that lost the routing vote is evidence, not noise) |
| the enqueue gate | an item with a missing field, an invalid status, an invalid queue or a non-positive count is **refused** |
| routing | a tied routing is flagged `needs_human` rather than silently decided |
| review | model-queue items are escalated; ambiguous items are escalated; an item outside the reviewer's permissions is escalated, never approved; no escalated item is ever approved |
| version change | the version is bumped; only approved items are in the set; a mis-mapped signal is **moved**, not duplicated; the candidate carries the new version |
| regression | the baseline fails the mis-mapped case and the candidate passes it; the change fixes what it targeted and regresses nothing; a candidate that breaks a passing case is **not** acceptable; a candidate that fixes nothing is **not** acceptable |
| the committed artefact | `config/taxonomy.v1.1.0.yaml` exists and carries the move |

### The gates are proven able to fail

`bash evidence/mutation_check.sh` injects six behavioural defects and requires
the loop to catch each one:

```
[OK] M1 an item with an invalid status is refused
[OK] M2 a tied routing is flagged instead of silently decided
[OK] M3 an item outside the reviewer's permissions is escalated
[OK] M4 a candidate that regresses a passing case is rejected
[OK] M5 an escalated item never enters the change set
[OK] M6 a change that fixes nothing is not accepted
MUTATION PROOF: PASS
```

Every mutation changes behaviour — a malformed item, a policy with no
permissions, a signal moved to the wrong queue, a candidate that regresses —
never a name, so a passing mutation run cannot be a rename no-op.

> One note for the record: the first version of this mutation script used
> "the script exits non-zero" as its success condition, and two checks passed
> for the wrong reason — they died on a type error rather than because the
> gate fired. The script now asserts the opposite polarity (exit 0 means the
> gate caught the defect) and the checks use real objects, so a pass means the
> gate actually fired.

## 5. Reproduce it yourself

```bash
git clone https://github.com/AWKN-Lab/feedback-triage-loop
cd feedback-triage-loop
pip install pyyaml
python3 checks/test_triage.py     # 30 checks
bash evidence/run.sh              # the four-stage demo -> evidence/run.log
bash evidence/mutation_check.sh   # proof the gates can fail
```

No credentials, no network access, no paid dependency.

## 6. Scope and honest limits

- The regression suite is a labelled set of routing cases plus the invariant
  checks, not a full model-evaluation harness: wiring it to your own evaluation
  suite means replacing the case file.
- The trajectories are **synthetic and desensitised**: no real customer, order
  or account id appears in this package. Per the task note, real data is
  exchanged through the platform after the NDA.
- The queues, the reviewer permissions and the signal taxonomy are policy, not
  science. They live in `config/` precisely so a team can argue with them
  without forking the tooling.
- Third-party model API, cloud resources and training compute are excluded
  from the budget, as the task note states.
