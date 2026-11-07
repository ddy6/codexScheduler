# Host integration

The role instructions are portable Markdown. Copy the complete `poll-review`
folder into your host's skill directory, or reference its `SKILL.md` from local
project guidance. The bundled script needs Python 3.11+ on macOS or Linux and no
installed scheduler package. Other systems can use equivalent host timing while
retaining the same ownership and stopping rules.

## Codex installation

From an installed Codex Scheduler package:

```sh
codex-scheduler export-skill ~/.agents/skills/poll-review --name poll-review
```

For a project-local skill, use `.agents/skills/poll-review` as the destination.
Exports refuse existing destinations. Without installing the package, run this
from a trusted repository checkout to copy the standalone skill:

```sh
python3 -c 'from pathlib import Path; import shutil; shutil.copytree("skills/poll-review", Path.home() / ".agents/skills/poll-review")'
```

Codex's [skill documentation](https://learn.chatgpt.com/docs/build-skills) describes
local discovery. Its [subagent documentation](https://learn.chatgpt.com/docs/agent-configuration/subagents)
describes delegation and inherited permissions. No model or reasoning setting is
required by this skill. Honor the user's choice; start the helper without full
conversation history when the host supports it. If it cannot provide a bounded
context, do not represent this as the small-context workflow.

Example request after installation:

> Use poll-review for this authorized team. Keep the engineer as the source writer.
> Give the helper one frozen batch and one poll deadline at a time. Establish the
> exact identities, finite wake allowance, and available return route first. Keep
> polling manual if automatic return is unavailable.

## Capability check before automatic return

Installing a skill does not install an agent transport. Verify these capabilities
in the actual session, including loaded restrictions and caller permissions:

| Needed capability | Integration requirement |
| --- | --- |
| Clean helper context | Start with role, binding and bounded batch, without the root transcript |
| Child assignment | Direct supported native activation of the bound helper |
| Stable process ownership | Root retains the exact typed handle and can inspect it later |
| Deadline completion | Helper can sleep without inference or receive its timer's completion |
| Exact root status | One nonblocking check distinguishes idle from busy, approval and unknown |
| Exact root activation | One authorized send to that original root, preserving its settings |
| Durable continuation state | Bindings, consumed allowance and uncertain sends survive handoffs |
| Single engineer | Host/controller excludes competing engineering turns for the same work |

Where exposed, Codex collaboration tools can create clean children and assign
work using direct `followup_task`; `send_message` to a busy peer queues a note.
Do not wrap native collaboration calls in a shell or an execution tool. Child
activation and root activation are different capabilities: do not assume that a
tool for following up with children can activate its parent. Do not invent a root
UUID, change permissions, or select an alternate transport after a denied send.

If any required capability is unavailable, run focused reviews and report a
manual poll interval. The timer can still record a due deadline, but cannot itself
resume the conversation. A successful offline script test does not establish live
agent delivery. Record the first authorized real send and matching root inspection
separately from installation; do not manufacture a wake as part of installation.

Other hosts need the same capabilities, not Codex tool names. This repository does
not ship a universal adapter or assert compatibility with untested clients. Use
the durable scheduler for its supported dedicated-worker topology when you need
central enforcement. Never point that dispatcher at a native root as a workaround.

## Compact local guidance

Projects without skill discovery can reference the installed files from their
agent instructions. A suitable entry is:

> For authorized team work, follow the installed poll-review SKILL.md. The engineer
> owns code and job handles. One helper reviews explicit frozen batches and owns
> one poll deadline. Maintain a small context and one return sender. Use only the
> verified exact-target return route; otherwise yield for a manual poll. Preserve
> task bounds, consumed allowance, approval controls and uncertain effects.

Set actual paths locally. Keep real handoffs, timers and review snapshots in private
scratch excluded from version control. Do not copy session bindings into shared
guidance or replace a project's existing pause rule merely by installing this skill.
