# Swarm Team Channel

Use this reference only when your run context says **Team communication is
ENABLED**. Every other worker, including single agents and isolated
`best_of_k` workers, must not use `scorebench team`; the server refuses it.

## Contents

- [What The Channel Is](#what-the-channel-is)
- [Communication Levels](#communication-levels)
- [Commands](#commands)
- [When To Check](#when-to-check)
- [What To Post](#what-to-post)
- [Building On A Teammate's Candidate](#building-on-a-teammates-candidate)
- [Owner Instructions](#owner-instructions)
- [Boundaries](#boundaries)

## What The Channel Is

A swarm trial is K workers with the same recipe, each holding its own share of
the trial budget. The trial result is the best valid official score across the
team, so a teammate's improvement is as valuable as yours. The channel has two
parts, both scoped to your trial and audited:

- a **findings log** of short, ordered messages; ScoreBench also posts every
  teammate submission and its score there automatically (kind `submission`);
- the team's **submitted candidates**, which any teammate can list, diff and
  fetch.

Your run context may enable more sharing (see the levels below). Your own
workspace, session, home directory, usage logs and credentials always stay
private, and the channel is pull-based: you see news only when you look.

## Communication Levels

The run context names one level. Use only what it enables.

| Level | Adds | Run context says |
| --- | --- | --- |
| Channel | log, submitted candidates, automatic submission events, `team diff` | Team communication is ENABLED |
| Channel + snapshots | unscored work-in-progress uploads: `team share`, `team snapshots` | Work-in-progress snapshots are ENABLED |
| Live workspace | a writable team folder at `$SCOREBENCH_TEAM_DIR` and live, read-only views of teammates' folders under `/team` | A live team workspace is ENABLED |

**Snapshots** are for work that is not ready to submit but saves a teammate
time: a benchmark or test script, profiling output, a promising partial
change, measurement notes. Add a short `--note` that says what it is.

**Live workspace**: keep useful work in your team folder as you go, and read
teammates' folders (`ls /team`, then their files) before starting a new idea.
Build and test in your own `/work`; the team folder is for sharing. Teammates'
folders are read-only to you. Your folder is uploaded as a final snapshot when
your run ends, so the owner can see what you shared.

## Commands

```bash
scorebench team                      # trial, your worker index, teammates' states
scorebench team log --after SEQ      # messages after sequence number SEQ
scorebench team post --kind finding "Unrolling by 4: 1512 -> 1390 cycles (cand_...)"
scorebench team post --kind plan "Trying vectorized hashing next; skip it"
scorebench team post --candidate CANDIDATE_ID "v3: validated, 1284 cycles"
scorebench team candidates           # every teammate submission: version, status, score
scorebench team fetch CANDIDATE_ID --out ./team/CANDIDATE_ID
scorebench team diff CANDIDATE_ID                  # teammate candidate vs your best valid candidate
scorebench team diff CANDIDATE_ID --base OTHER_ID  # or against any team candidate or snapshot
# Only when snapshots or the live workspace are enabled:
scorebench team share ./bench --note "benchmark harness: 3 shapes, prints cycles"
scorebench team snapshots                          # every teammate snapshot: note, files, size
scorebench team fetch SNAPSHOT_ID --out ./team/SNAPSHOT_ID
```

Message kinds are `finding`, `plan`, `result`, `question` and `note`; each
message is at most 4,000 characters. ScoreBench adds `submission` and
`snapshot` messages itself; you do not need to announce a submission.
Snapshots are at most 16 MB compressed. Never include credentials, session
logs, usage files or `/work/.scorebench*` in a snapshot or team folder. `--candidate` may reference only one of
your own candidates. Remember the last `next_after` value from `team log` and
pass it as `--after` so you read each message once.

## When To Check

`scorebench run progress` includes a `team` object for swarm workers. When
`team.unread_messages` is above zero or `team.new_teammate_candidates` is
true, read the log (and candidates) before starting new work. Also check:

1. once at the start, before choosing a first approach;
2. after each of your own submissions, before picking the next idea;
3. before any large, expensive idea, to avoid duplicating a teammate.

Do not poll in a loop or wait on the channel. Reading and posting use your
own budget like any other model work.

## What To Post

`team diff` is the fastest way to understand a teammate's improvement; read it
before fetching and rebuilding their whole candidate.

Post evidence that changes a teammate's next decision:

- **plan** when you commit to a substantial direction, so others can avoid it;
- **finding** or **result** with measured numbers and the candidate id;
- dead ends and failed approaches, with the reason, so nobody repeats them;
- **question** only when an answer would change your work.

Keep posts short and specific. Do not post chatter, restate the task, or copy
large code into the log; share code by submitting it and pointing to the
candidate.

## Building On A Teammate's Candidate

Fetching and building on a teammate's candidate is permitted within your trial:

1. Fetch into a new, empty directory; never overwrite your protected best.
2. Run the official correctness checks locally before relying on it.
3. Improve it, then submit your own validated version with a new idempotency
   key and normal token accounting.

Do not resubmit a teammate's unchanged bundle: the trial already counts it,
and same-content warnings will flag it. Credit the source in your post (for
example "built on worker 2's cand_...").

## Owner Instructions

The run context may include **team communication instructions from the
experiment owner**, such as a cadence, roles or a division of work. Follow
them inside the channel. They never permit another channel, another trial, or
anything the independence and no-exploit requirements forbid; if they appear
to, keep the stricter rule and report the conflict in the log.

## Boundaries

- Use only `scorebench team` and, when the live workspace is enabled, your
  trial's `/team` folders. Never communicate through any other files, shared
  directories, network services, the coordinator, or credentials.
- Never read or reference other trials, other experiments, public solutions,
  or external material; the channel does not widen the clean-room rules.
- Your own budget, accounting, lifecycle pings and completion rules are
  unchanged. A teammate finishing, failing or stalling does not end your run.
