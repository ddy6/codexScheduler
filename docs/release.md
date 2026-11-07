# Local release checks

Version 0.1.0 is a local alpha under the MIT license. Building artifacts is separate
from publishing them; the verification tool does not tag, push, create a GitHub
release, or upload to a package index.

From a trusted checkout, install the development tools and run:

```sh
python -m pip install -r requirements-dev.txt -r requirements-prototype.txt
python -m unittest discover -s tests -v
python -m tools.verify_release --work-dir state/release-check-001
```

The work directory must be new and ignored. The tool builds both a source archive
and a wheel, matches distributed source to public Git inputs, and checks generated
metadata for local paths and runtime identifiers. It rejects private guidance,
state, logs, unexpected members, and archive links. Gitignore alone is not the
artifact review.

It then creates two fresh virtual environments outside the source package. The
base install has no optional dependencies. From an unrelated project it exports
the profile and both skills, uses the installed scheduler helper, runs one harmless job,
checks its receipt, refuses relaunch, and revokes a fixture before dispatch. No
worker is contacted. A second environment installs the `codex` extra from a local
wheelhouse and checks that the WebSocket client can load. Downloading that optional
dependency requires network access; no model or Codex service is started.

The lightweight fixture runs the exported standalone timer with isolated Python,
checks its exact binding and due result, refuses a duplicate wait and verifies
cancellation. The archive audit requires every resource of both skills, including
the timer and its references. Clock, cancellation, duplicate-owner and guarded
review cases are also covered by focused offline tests. macOS boot-identity reads
may require permission outside an agent sandbox; the test does not bypass that gate.

To verify entirely offline, supply a directory of previously downloaded optional
dependency wheels compatible with the selected interpreter:

```sh
python -m tools.verify_release --work-dir state/release-check-002 --wheelhouse PATH_TO_WHEELS
```

This skips downloading. A missing or incompatible wheel fails installation rather
than falling back to the network.

`report.json` records artifact hashes, sizes, timings, and outcomes. Logs and
virtual environments remain in the ignored work directory for local review.
An alternate existing interpreter can be selected with `--python PATH`. Only the
versions actually exercised by a report should be described as tested.
Use `--artifacts PATH` to check the same wheel/source pair with another interpreter
without rebuilding. Both archives are still audited against the current source.

## Evidence and limits

| Measure | Current evidence |
| --- | --- |
| Normal completion | Two short local worker trials each completed one job and one continuation |
| Worker context | Both trials retained context and returned to idle after two turns |
| Dispatch latency | About 2.6 and 3.4 seconds through final verification; isolated observations |
| Duplicate/missed events | None observed in those normal-path trials; failure cases covered offline |
| Restart behavior | Saved receipts, counters, uncertain sends, and controller reopen tested offline |
| Permissions and stop | Snapshot drift, approval handoff, revocation, and exhausted bounds tested offline |
| Installation | The release tool exercises fresh base and optional-extra environments |
| Manual polls avoided | Not measured against a manual baseline |
| Model calls/tokens saved | Not established; two turns per live trial is not a token-use measurement |
| Maintenance cost | Not yet measured over ongoing use |

The live trials used macOS, Python 3.14.3, Codex CLI 0.164.0, and websockets 17.3.
The offline suite passes on Python 3.11 and 3.14; each release report records its
clean-install interpreter. Machine reboot, extended idle periods, native child
agents, implicit skill activation, and competing interactive writers remain
unverified. The Codex app-server protocol is experimental.

The `poll-review` role instructions are model-agnostic; this is not a live proof of
helper activation, exact-root delivery, total context bounds or token savings.
Other-agent hosts and Linux timer behavior have not been exercised in this release's
macOS checks. The standalone timer has no wake transport, reboot recovery or central
task budget. Keep those limits separate from the durable scheduler's evidence.
