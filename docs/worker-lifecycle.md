# Worker lifecycle experiment

This prototype checks whether a dedicated Codex worker can yield while a job runs
and accept one completion event in the same conversation. It is an integration
experiment, not the scheduler or its recovery system.

## Verified result

The local trial passed on October 25, 2026 using macOS, Python 3.14.3, Codex CLI
0.164.0, and websockets 17.3. The worker used the configured GPT-6.1 Sol model.

| Check | Observed result |
| --- | --- |
| Job launch | One synthetic job, registered before launch |
| Yield | The first turn finished while the job was still running |
| Completion | The job exited successfully with the expected sum, 45 |
| Delivery | One CLI queue dispatch started a second turn in the exact worker |
| Context | The second turn recalled the token from the first turn |
| Stop | Two turns total; the worker returned to idle |
| Permissions | Read-only sandbox, network access disabled, on-request approvals |

Completion dispatch to final verification took about 2.6 seconds in this small
trial. That is a single observation, not a latency benchmark. The harness closed
its local server after verification; it did not install a service or send input
to the user's conversation. Approval handling passed an offline check; no live
approval request occurred.

The initial connection attempt failed before any worker or model work started.
Replacing a byte proxy with a WebSocket connection resolved the problem, and a
no-model preflight passed before the live trial. The original job identity and
unused allowance were preserved. Private runtime evidence remains local; this
summary deliberately omits prompts, machine paths, and session identifiers.

## Run it

Use Python 3.11 or newer and a signed-in Codex CLI. The initial target is local
macOS with CLI 0.164.0. Protocol support must be checked on other versions.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-prototype.txt
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python prototypes/worker_lifecycle.py --run-dir runs/queue-trial-001 --preflight-only
.venv/bin/python prototypes/worker_lifecycle.py --run-dir runs/queue-trial-001 --retry-bootstrap
```

The last command starts model work. It creates a dedicated local app-server
process and disposable workspace, uses the configured model, and never addresses
your live conversation. No service is installed. The worker uses a read-only
sandbox and on-request approvals. The controller stops on requests for approval
or other operator input; it does not supply approval answers.

The harness registers one harmless job through a narrowly defined tool. The job
waits for the worker's first turn to end before computing a small sum. Ordinary
code then queues one completion event to the exact worker. The final response must
recall a token from the first turn; the event does not repeat it. Success requires
exactly two completed turns and a worker that has returned to idle.

Each run permits one job launch and one completion dispatch. The run directory
must be new; rerunning against an existing directory is refused. A timeout or
uncertain dispatch stops the trial without retrying. Do not start another trial
to retry an event whose outcome is unknown.

The explicit `--retry-bootstrap` option reuses the same grant after a transport
check or startup failure, only while there is no worker ID and no consumed job or
continuation allowance. It preserves the job identity and startup history. Once
a worker exists, this option refuses to retry. The Unix listener requires a
WebSocket handshake; a raw byte proxy is insufficient.

Inspect `status.json`, `completion.json`, and lifecycle events in the run directory.
Logs and status are private, ignored artifacts; they may contain local paths,
thread identifiers, and prompts. Codex also retains the worker's conversation in
its normal local storage. The harness closes its own server and job on exit.

If queueing proves insufficient, a separately authorized trial can select
`--transport turn-start`. This uses the same exact worker through the app-server
API. The harness never falls back automatically after a queue attempt.

## What this does not establish

A controller-owned worker is separate from the user conversation; it is not a
native child created by Codex's agent tools. Native child addressability, idle
eviction, parent wake-ups, controller restarts, client reconnection, and durable
recovery need separate checks. This experiment does not measure token savings or
guarantee compatibility across Codex clients.

The registry and delivery protocol will be developed after this interface check.
This harness is single-use and does not claim cross-run budget enforcement or
exactly-once external effects. Run one controller at a time; concurrent controllers
and restart recovery are outside this experiment. The live job was brief, so this
does not establish behavior after extended idle periods or machine sleep.

## Interface references

- [CLI queue and app-server commands](https://learn.chatgpt.com/docs/developer-commands)
- [Thread, turn, and approval APIs](https://learn.chatgpt.com/docs/app-server)
- [Native subagent behavior](https://learn.chatgpt.com/docs/agent-configuration/subagents)

The local CLI can generate its protocol schemas with
`codex app-server generate-json-schema --experimental --out state/slice1/schemas`.
Generated schemas stay local; they are not a maintained copy of the API docs.
