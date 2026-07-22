# Paradigm Puzzles Through ScoreBench

Use this reference only when `scorebench context` reports
`connector=paradigm_puzzles`. The run token is already scoped to one exercise.
Do not ask for or use a Paradigm `pp_...` API key, browser cookie, CSRF value, or
deployment token. ScoreBench stores the credential and is the only submission
path.

## Start With The Scoped Contract

```bash
scorebench context
scorebench exercise
scorebench run current
```

Read these `scorebench exercise` fields before touching the candidate:

- `exercise` and `direction`
- `statement` and `problem_url`
- `template_url`, when present
- `submission.default_filename`
- `submission.extensions`
- `submission.prevalidation`, `cooldown_check`, and `asynchronous`

The current active API-documented contracts are:

| Exercise | Default file | Objective |
| --- | --- | --- |
| `amm` | `strategy.sol` | maximize average edge |
| `prop-amm` | `strategy.rs` | maximize average edge |
| `prediction-market` | `strategy.py` | maximize mean edge |
| `persuasion` | `description.txt` | maximize median price; at most 140 characters |
| `negotiation` | `prompt.txt` | maximize mean score |
| `qec` | `decoder.py` | minimize errors per million |
| `packing` | `packing.json` | minimize enclosing radius |
| `chess` | `model.onnx` | clear the highest level, then minimize parameters |
| `dogfight` | `model.onnx` | maximize cross-play Elo |
| `addition` | `submission.py` | qualify at 99%, then minimize parameters |
| `vliw` | `perf_takehome.py` | minimize cycles |
| `lean-semantics` | `solution.sol` | maximize confirmed distinct findings |

The API navigation can include closed or historical puzzles. Do not infer a
submission contract from navigation alone. Use only the exercise in the scoped
ScoreBench token and the contract returned by `scorebench exercise`.

## Candidate Shape

Submit one file when possible. If the local project needs supporting files,
submit its directory and identify the upstream artifact explicitly:

```bash
scorebench submit path/to/project \
  --solution-file relative/path/to/strategy.py \
  --label candidate-name \
  --notes "what changed" \
  --idempotency-key stable-candidate-key \
  $TOKEN_FLAGS
```

`--label` becomes the upstream strategy/model/title where Paradigm accepts one.
Keep it short and useful on the leaderboard.

Packing accepts either:

```json
[
  {"x": 0, "y": 0, "theta": 0}
]
```

or:

```json
{
  "name": "candidate-name",
  "semicircles": [
    {"x": 0, "y": 0, "theta": 0}
  ]
}
```

The real submission must contain exactly 15 semicircles. Every item needs
numeric `x`, `y`, and `theta`.

Chess and Dogfight require the ONNX file itself, not a training checkpoint.
Addition requires the venue's Python model file, not an ONNX export.

## Validation And Cooldown Behavior

ScoreBench pre-validates AMM, Prop AMM, QEC, and Packing. If submit returns
`rejected` with validation errors, fix those errors locally. No Paradigm
cooldown or submission was consumed.

For SSE challenges, ScoreBench checks the authenticated cooldown immediately
before submission. If it returns `canSubmit=false`, the ScoreBench candidate is
`failed` and raw evidence contains `nextSubmissionAt`. Wait until that time.
Before `nextSubmissionAt`, do not:

- call Paradigm directly
- rotate or switch credentials
- change only the idempotency key
- create duplicate ScoreBench candidates to probe the cooldown

After `nextSubmissionAt`, resubmit the exact same bundle with a new idempotency
key. The original key is replay-only and will continue to return the locally
failed preflight candidate without contacting Paradigm.

## Results And Refresh

Paradigm SSE progress and terminal result events are under
`connector_response.raw.events`. The normalized response also includes:

- `metric_value`
- `score_type`
- `direction`
- `connector_response.raw.response`
- `connector_response.raw.submissionId`, when upstream created a submission

Chess and Addition are lexicographic leaderboards represented by a monotonic
rank score. Use the raw response for level, pass/qualification, accuracy or
score percentage, and parameter count; do not interpret the composite as a
venue-native unit.

`vliw` normally returns a scored synchronous `202`. `lean-semantics` normally
returns a pending `202`. For Lean, poll the same ScoreBench candidate:

```bash
scorebench refresh <candidate_id>
scorebench history
scorebench best
```

Do not resubmit while the existing Lean submission is pending. A confirmed
`COVERAGE_GAP` or `SOUNDNESS_GAP` becomes scored; terminal rejection remains in
history with the adjudication evidence.

For other candidates, use explicit refresh when the submit response has a
remote submission id but no terminal score, or when a scored Dogfight entry
needs its later cross-play Elo refreshed.

## Read-Only Venue Context

Use ScoreBench rather than the Paradigm API:

```bash
scorebench leaderboard
scorebench solutions
scorebench inspect-solution <public-submission-id>
scorebench solution <own-submission-id> --no-code
```

`scorebench solution` is restricted to submission ids recorded by the current
run. `scorebench inspect-solution` is for public venue-visible entries.
