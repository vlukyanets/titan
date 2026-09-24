# Roadmap (volatile)

Everything in this folder is **planning material that is expected to change**:
milestones, implementation plans, task lists, research notes and open
questions. Permanent knowledge (what the system does and why) belongs in the
[spec](../spec/product.md), the [architecture docs](../architecture/overview.md)
and the [ADRs](../adr/).

## Contents

| File | Purpose |
|---|---|
| [milestones.md](milestones.md) | Milestones in order, with their scope and exit criteria |
| [open-questions.md](open-questions.md) | Undecided questions and where they will be answered |
| `research/` | Notes and spike code for Proposed ADRs, created when needed and deleted once the ADR is accepted |
| `plans/` | One implementation plan per milestone or feature, created when work starts |

## Workflow

1. Pick the next milestone from [milestones.md](milestones.md).
2. Write `plans/<milestone>-<topic>.md`: the goal, the spec sections it
   implements, a design sketch and a task checklist.
3. Implement task by task. Tick tasks off in the plan in the same commits that
   complete them.
4. When the milestone is done, move any lasting knowledge into the permanent
   docs, mark the milestone done, and delete the plan and research files it
   used. Git history keeps them.
