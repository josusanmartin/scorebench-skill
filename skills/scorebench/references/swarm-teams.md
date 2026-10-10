# Swarm Team Channel

Use this reference only when your run context says **Team communication is
ENABLED**. Every other worker, including single agents and isolated
`best_of_k` workers, must not use `scorebench team`; the server refuses it.

## Runner Compute

Read the manifest's `runner` settings without changing them. Local Docker may
share host resources, divide available CPU/RAM evenly, or impose fixed per-agent
limits. Never loosen limits or move worker compute onto the coordinator to make
an overcommitted batch launch.

Some deployments offer experimental cloud runners: Cloudflare sandboxes,
AWS EC2 VMs, boat.dev VMs and, where the server lists it, OVHcloud VMs. Use the generated
launch block exactly as written. Backend, size, region or location, worker count
and lifetime are fixed by the saved experiment; never edit them, retry a
consumed launch token, replay a pairing, or re-run the launcher to recover. Each
worker gets its own sandbox or VM. Cloud compute is billed to the coordinator's
cloud account, on top of the selected model budget, and setup never purchases a
plan.

Before launching, sign in on the coordinator machine where the prompt is pasted.
A login on another device does not count:

- Cloudflare: `wrangler whoami` succeeds (use `wrangler login`, or
  `wrangler login --device --browser=false` on a headless host). The launcher
  prepares a private bridge from the verified archive before redeeming the token.
- AWS: `aws sts get-caller-identity` succeeds for the intended profile. Cloud
  credentials stay on the coordinator; never copy them into a VM, Terraform
  variables or chat.
- boat.dev: `BOAT_API_KEY` is exported in the coordinator's shell (never print
  it). Create it from the boat.dev CLI login: install the CLI
  (`curl -fsSL https://boat.dev/install | sh`, then
  `export PATH="$HOME/.ascii/bin:$PATH"` if `boat` is not found), run
  `boat login`, then
  `boat api-key create scorebench --ttl 30d --actions sandbox.create,sandbox.read,sandbox.update,sandbox.delete,exec,ssh`.
  A paid boat.dev plan is required: trial accounts allow only 2 sandboxes with
  a 2-hour lifetime and fail with `trial_auto_stop_required`. boat.dev is EU
  only, and the launcher paces creates to the plan's start limit (12 per
  minute on the $20 plan).
- All: each selected harness is signed in on the coordinator (Claude Code with
  the subscription, Codex with ChatGPT where the runner supports it), and those
  logins stay alive. Only access tokens are forwarded, never refresh tokens.

New Cloudflare experiments are automatically restricted to **EU only**. There is
no selector, and reproduced recipes keep their saved policy. The launcher checks
each sandbox's reported country and region before it sends any credentials. A
sandbox missing that report, or placed outside the policy, stops the launch
without a replacement. The restriction covers the sandbox VM only, not the bridge
or the path credentials take, so it is not a data-residency guarantee.

Cloud runners support parallel launch and Channel or Live workspace swarms.
Cloudflare runs Claude Code subscription workers only; AWS, OVH and boat.dev
VMs run Claude Code and Codex. On current servers the live
workspace is synced by the coordinator, so it works on cloud runners too (see
[Communication Levels](#communication-levels)). Older servers may keep the live
workspace on local Docker only. The server's availability for the run is always
authoritative. The hard cloud runtime and coordinator lease still apply.

Do not bypass guards, substitute API billing, or replace a lost sandbox or VM.
A failure before release stops that batch without replacement. Preserve the
downloaded evidence; checkpoints are not resumable VM snapshots. For setup or
failures, read only the relevant section on the run's ScoreBench server:
`/ui/docs/experiment-launching/#cloudflare-sandboxes`,
`/ui/docs/experiment-launching/#boatdev-workers` or
`/ui/docs/experiment-launching/#worker-compute`. Older deployments may not offer
cloud launches.

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
On deployments with the Swarms view, the owner can watch this same IRC-style
channel in the experiment monitor. Owner viewing is not evidence that a worker
read a message.

## Communication Levels

The simplified recipe picker offers Channel or Live workspace; older deployments
may also offer snapshots. The run context names the saved level; use only what
it enables. Historical snapshot-only protocols remain valid.

| Level | Adds | Run context says |
| --- | --- | --- |
| Channel | log, submitted candidates, automatic submission events, `team diff` | Team communication is ENABLED |
| Channel + snapshots (historical) | unscored work-in-progress uploads: `team share`, `team snapshots` | Work-in-progress snapshots are ENABLED |
| Live workspace | a writable team folder at `$SCOREBENCH_TEAM_DIR` and read-only views of teammates' folders under `/team` (live mounts on local Docker, synced copies on cloud runners) | A live team workspace is ENABLED |

**Snapshots** are for work that is not ready to submit but saves a teammate
time: a benchmark or test script, profiling output, a promising partial
change, measurement notes. Add a short `--note` that says what it is.

**Live workspace**: keep useful work in your team folder as you go, and read
teammates' folders (`ls /team`, then their files) before starting a new idea.
Build and test in your own `/work`; the team folder is for sharing. Teammates'
folders are read-only to you. On cloud runners the coordinator copies folders
between workers every few seconds. This is eventual, not a shared filesystem:
expect a delay, and don't rely on file locking or immediate visibility. Check
`/team/sync-status.json` (`ok` or `degraded`, plus peer observation times)
before trusting a teammate's copy. Each folder is limited to 4 MiB and 512
entries. Symlinks, special files and files that change during export block an
update, and the previous version stays readable. Your folder is uploaded as a
final snapshot when your run ends, so the owner can see what you shared.

The launcher may apply CPU and RAM limits evenly across agents or use fixed
per-agent quotas. Respect the recorded allocation; do not remove limits or
move work onto the host or another worker to evade them. Shared team folders
do not grant access to teammates' private workspaces or credentials.

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
