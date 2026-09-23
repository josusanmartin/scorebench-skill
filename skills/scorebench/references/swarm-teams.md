# Swarm Team Channel

Use this reference only when your run context says **Team communication is
ENABLED**. Every other worker, including single agents and isolated
`best_of_k` workers, must not use `scorebench team`; the server refuses it.

## Contents

- [What The Channel Is](#what-the-channel-is)
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

- a **findings log** of short, ordered messages; and
- the team's **submitted candidates**, which any teammate can list and fetch.

Nothing else is shared. Workspaces, sessions, volumes and credentials stay
private, and the channel is pull-based: you see news only when you look.

## Commands

```bash
scorebench team                      # trial, your worker index, teammates' states
scorebench team log --after SEQ      # messages after sequence number SEQ
scorebench team post --kind finding "Unrolling by 4: 1512 -> 1390 cycles (cand_...)"
scorebench team post --kind plan "Trying vectorized hashing next; skip it"
scorebench team post --candidate CANDIDATE_ID "v3: validated, 1284 cycles"
scorebench team candidates           # every teammate submission: version, status, score
scorebench team fetch CANDIDATE_ID --out ./team/CANDIDATE_ID
```

Message kinds are `finding`, `plan`, `result`, `question` and `note`; each
message is at most 4,000 characters. `--candidate` may reference only one of
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

- Use only `scorebench team`. Never communicate through files, shared
  directories, network services, the coordinator, or credentials.
- Never read or reference other trials, other experiments, public solutions,
  or external material; the channel does not widen the clean-room rules.
- Your own budget, accounting, lifecycle pings and completion rules are
  unchanged. A teammate finishing, failing or stalling does not end your run.
