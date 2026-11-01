# The converged rewrite's open rows (`PR-CONV`)

One row per task of the converged 3GPP + O-RAN rewrite plan ([`docs/plan/CONVERGED_REWRITE_PLAN.md`](../CONVERGED_REWRITE_PLAN.md), snapshot of
31 October 2026; the live copy is the Claude Docs plan "ai-ran-smo Converged Rewrite Plan"), as its stage tables list them: 128 tasks, `S0.1` to `S8.14`.
Cite a task as `PR-CONV-S3.1`. **Needs** is as in the plan: a task ID, a milestone (`M0` to `M7`, each the gate at the end of its stage) or a decision
(`D1` to `D20`, all answered: `HISTORY.md` PR-CONV-S0.1). The pull request that finishes a task deletes its row from its stage file and adds `smo/history.d/<ID>.md` with
real detail (the content a `HISTORY.md` entry holds), and sets the task's status in the live plan to Done the same day (the plan's working rule 6). Until 1.0 the work runs in rewrite mode
(`docs/RELEASES.md`, "Until 1.0: rewrite mode"). An older item of this file that a row replaces says so with a line "Superseded by PR-CONV-Sx.y".

One file per stage, the same table as before (ID, Task, Stage, Needs, Done when):

- [`S0.md`](S0.md)
- [`S1.md`](S1.md)
- [`S2.md`](S2.md)
- [`S3.md`](S3.md)
- [`S4.md`](S4.md)
- [`S5.md`](S5.md)
- [`S6.md`](S6.md)
- [`S7.md`](S7.md)
- [`S8.md`](S8.md)

A pull request edits only its own stage's file, so pull requests of different stages never conflict here (`smo/CLAUDE.md`, "Working in parallel"). Rows closed before this split are in `HISTORY.md` §11.
