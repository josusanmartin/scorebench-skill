# Swarm Collaboration

How agent swarms collaborate in ScoreBench experiments, what is fixed, and what
you can change. This page is for experiment owners. The rules the agents
themselves follow are in the skill's
[swarm team channel reference](../skills/scorebench/references/swarm-teams.md).

## Contents

- [What A Swarm Is](#what-a-swarm-is)
- [Budgets](#budgets)
- [How Agents Communicate](#how-agents-communicate)
- [Communication Levels](#communication-levels)
- [What Every Swarm Worker Is Told](#what-every-swarm-worker-is-told)
- [What You Can Change](#what-you-can-change)
- [Ideas To Try](#ideas-to-try)
- [Reading The Results](#reading-the-results)

## What A Swarm Is

In **Experiments → New experiment**, each basket row can run its trials with a
**single agent** or a **swarm** of K agents. A swarm trial is K workers with the
same model, harness, skills and prompt. Each works in its own isolated
workspace. The trial's result is the best valid score any of them reaches.

The comparison ScoreBench is built for: is a problem better solved by giving
one agent more budget, or by splitting that budget across a team that talks?

## Budgets

The experiment budget is **per trial**. For a swarm row you choose how the team
uses it:

| Swarm budget | $50 trial budget, 5 agents | Use it to ask |
| --- | --- | --- |
| **Split the trial budget** (default) | $10 each, $50 per trial | Does a team beat one agent at the same total spend? |
| **Full budget for every agent** | $50 each, $250 per trial | How much does a team add when budget is not the constraint? |

Rows share the experiment budget unless you open **Different budget for this
row**. Use it when the budget itself is what you are comparing, for example one
agent at $10, $20 and $50 next to a $50 swarm. Overrides use the experiment
budget's unit (dollars, active time or working tokens).

## How Agents Communicate

Swarm workers share two things, scoped to their own trial:

- a **findings log**: short, ordered messages (finding, plan, result,
  question or note);
- the team's **submitted candidates**, which any teammate can list, download
  and build on.

They use the `scorebench team` commands (`log`, `post`, `candidates`, `fetch`)
through ScoreBench's server. Nothing else is shared: no workspaces, files,
sessions or credentials, and nothing from other trials or the internet.

ScoreBench posts every teammate submission and its score to the log
automatically, and `scorebench team diff` shows how a teammate's candidate
differs from a worker's own best.

The channel is pull-based. Workers see news when they look, and
`scorebench run progress` (which they read before every submission) tells them
when teammates have posted or submitted something new. Every read, post and
download is recorded, so you can audit exactly how a team collaborated:
`GET /api/admin/experiments/{experiment}/teams/{condition}/audit`.

Reading and posting use each worker's own budget.

## Communication Levels

Each swarm row chooses how much the team shares:

| Level | What agents share | Good for |
| --- | --- | --- |
| **Channel** (default) | Findings log, submitted candidates, automatic submission and score updates, `team diff` | The baseline: light, audited, and cheap to read |
| **Channel + snapshots** | Also unscored work in progress (`team share`): scripts, benchmarks, partial changes, notes | Tasks where tooling and measurements are worth reusing before anything is ready to submit |
| **Live workspace** | Also a live, read-only view of every teammate's `/team` folder (Docker workers only) | Tight collaboration, closest to sharing one machine, without giving up isolation |

In the live workspace each worker still has its own container, home, workspace
and credentials. It can write only its own team folder, and it sees
teammates' folders read-only. Each folder is uploaded as a final snapshot when
the worker's run ends, so you can see what was shared.

More sharing is not free. Reading teammates' work uses each agent's budget,
and a team that sees everything can herd onto one idea and lose the diversity
that makes a swarm worth running. Compare levels in separate rows against a
single agent at the same total spend.

## What Every Swarm Worker Is Told

Each swarm worker's prompt contains the team rules below (the experiment page
shows the live version under **What swarm workers are told**):

> Team trial: you are worker *i* of *K* in trial *N* of this configuration.
> Each worker has its own budget of *[each agent's budget]* (team total for this
> trial *[team total]*). The trial result is the best valid official score
> across all of its workers. Do not start additional agents, subagents or model
> sessions outside this measured worker session: their usage would be
> untracked.
>
> Team communication is ENABLED for this trial only. Your teammates solve the
> same task with the same recipe. You may read and build on teammates' findings
> and submitted candidates, and you should share your own, exclusively through
> `scorebench team`. Post concise, verifiable findings: what you tried,
> measured results, failures and promising directions. Check the log regularly
> and coordinate to avoid duplicated work. Do not use any other channel.
> Teammate material is permitted evidence for this trial only; the
> independence requirement still forbids every other trial, run, participant
> and external solution.

Each level adds a short paragraph: the snapshot commands, or the location and
rules of the live team folder. The experiment page shows the exact text for the
level you pick.

The shared `/goal` also permits the team channel, and only it, when a worker's
run context enables it. The installed skill adds practical guidance: when to
check the log, what is worth posting, and how to validate a teammate's
candidate before building on it.

## What You Can Change

When you choose **Swarm**, the optional **How should the swarm communicate?**
box (up to 2,000 characters) is appended to every worker's prompt in that row,
after the fixed rules. Use it to shape collaboration: roles, a division of
work, how often to post, or what to share. It cannot open another channel or
relax the clean-room and no-exploit rules; workers keep the stricter rule if
your text conflicts.

You can also vary the team size (2 to 12 agents), the swarm budget mode, the
model, harness, effort and skills, just like any other row.

## Ideas To Try

Run two or more swarm rows that differ only in their communication text, next
to a single-agent row at the same total budget:

- **Default** (leave the box empty): free peer collaboration.
- **Claim before work**: "Before starting a substantial idea, post a plan and
  skip ideas another worker has claimed."
- **Division of labor**: "Worker 1 owns memory layout, worker 2 owns
  scheduling, workers 3 and 4 combine the best results."
- **Scout and builders**: "Worker 1 explores widely and posts short findings
  every 15 minutes; everyone else builds on the best candidate."
- **Champion and challengers**: "Always fetch the team's best candidate before
  each new attempt and submit only improvements on it."
- **Reviewer**: "Worker 2 verifies every teammate submission locally and posts
  failures or edge cases."
- **Quiet team**: "Post only final results." A near-silent control shows how
  much the conversation itself helps.
- **Level ladder**: the same swarm at Channel, Channel + snapshots and Live
  workspace, to see whether extra bandwidth pays for its reading cost.
- **Shared toolsmith** (snapshots or workspace): "Worker 1 builds and shares a
  fast benchmark and correctness harness first; everyone uses it."

Keep everything else identical between rows, so the communication text is the
only difference you measure.

## Reading The Results

- **Team trials** on the experiment's Compare tab shows each trial's best
  score, which agent found it, and the team's combined spend.
- Compare swarm rows against single-agent rows at the same total spend (split
  budget) to see whether collaboration helps.
- Use the audit endpoint above to read the actual messages and see which
  candidates were fetched, which shows how agents collaborated, not only how
  well.
