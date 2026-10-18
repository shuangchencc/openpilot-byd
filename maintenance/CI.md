# P7 CI and the shared offline entry

The new workflow is `.github/workflows/panda-compat-offline.yml`. The existing
`source-and-research.yml` is unchanged and is not evidence of P7 compatibility.
This repository contains the workflow and a locally exercised command contract;
it does **not** claim that GitHub Actions has executed successfully.

## Two distinct jobs

| Job | Trigger and inputs | What passing establishes |
| --- | --- | --- |
| `source-contract` | Pull request, branch push, or manual dispatch; ordinary Ubuntu checkout, no submodules or cached dependencies | Versioned lock/patch/tool hashes, release-gate and reproducibility fixtures, CLI contract, and CI policy match. It does not compile Panda or pandad. |
| `prepared-offline` | Manual dispatch only, trusted repository variable, fixed maintenance branch, protected environment, dedicated preprovisioned macOS ARM64 runner and its local Lima VM | Complete source/dependency verification, fresh patch replay, P5/P6 build and acceptance runs, reproducibility comparison, and the final release gate. |

Only the hosted checkout has a network preparation phase. It uses the already
recorded `actions/checkout` commit
`d23441a48e516b6c34aea4fa41551a30e30af803`, with submodules and LFS disabled and
credentials not persisted. The contract command then runs in a private network
and mount namespace with an inert `/dev`. The hosted runner's existing `sudo`,
`unshare`, Python and Git are prerequisites; failure to create the namespace
fails the job. There is no fallback to an unrestricted run or package install.

Both jobs have only `contents: read`. Neither job uses secrets, downloads build
dependencies, updates sources, publishes artifacts, uploads logs, or creates a
release. The full runner does not execute on a pull request. No step checks out,
cleans, resets, or updates the preprovisioned workspace.

## Runner preparation is a separate administrator action

Before enabling `prepared-offline`, configure all of the following in the trusted
repository. These settings cannot be enforced or created by the YAML itself:

1. Create the `panda-offline-maintainers` environment with required independent
   reviewers and deployment-branch restrictions for `songplus-offline-research`.
   Keep self-review and protection-rule bypass disabled where supported. Restrict
   the dedicated runner group to the trusted maintenance repository/workflow.
2. Register a disposable or otherwise dedicated runner with labels `self-hosted`,
   `macOS`, `ARM64`, and `songplus-panda-offline`. Do not attach C3XL, Panda, CAN,
   recovery media, signing keys, or unrelated credentials to this runner.
3. Set repository variables `PANDA_MAINTENANCE_REPOSITORY` (the exact owner/name),
   `PANDA_WORKSPACE` (absolute prepared checkout), `PANDA_LINUX_INSTANCE` (already
   running local Lima VM name), and `PANDA_LINUX_ROOT` (absolute cached Linux
   dependency root). None is a secret; do not put a device address in these values.
4. Explicitly prepare the locked source trees, Python/compiler/native dependency
   caches and the local Linux VM outside CI. P5/P6 dependency locks retain paths
   from the original prepared environment; moving to another machine requires a
   reviewed preparation/migration step. A nominal runner label does not establish
   that its paths or dependencies match the locks.
5. Prepare the exact dispatched repository commit in that workspace, with no
   tracked modifications. The shared runner verifies `HEAD == github.sha` and
   tracked cleanliness before any build. Missing sources, a dirty checkout, a
   stopped VM, or missing cached dependencies fail closed. CI never fixes these
   conditions by fetching, resetting, starting a VM, or installing packages.

The manual dispatch and protected environment together authorize use of the
dedicated runner. They do not authorize device operations or deployment. Each
run uses a new `results/panda-ci-<run_id>-<attempt>/` directory and leaves its logs
locally. Reviewers must inspect those logs on the runner; there is no automatic
artifact upload.

## The same entry can be exercised locally

The portable contract is stdlib-only and does not read the official checkout:

```sh
/usr/bin/sandbox-exec -f tools/panda_firmware_offline.sb \
  env P2_NETWORK_DEVICE_SANDBOX=macos-seatbelt \
  upstream-openpilot/.venv/bin/python -B tools/check_panda_ci.py \
  --mode contract --output results/panda-ci-contract-local
```

The complete prepared run uses the same pipeline as the protected job. Supply
the local VM and dependency directory explicitly:

```sh
upstream-openpilot/.venv/bin/python -B tools/check_panda_ci.py \
  --mode full --linux-instance PREPARED_LOCAL_LIMA_INSTANCE \
  --linux-root /ABSOLUTE/PREPARED/LINUX/DEPENDENCY/ROOT \
  --output results/panda-ci-full-local
```

Use a new output directory each time. Local work may intentionally retain other
tracked edits, so `--expected-commit` is optional for this local invocation. The
protected job always supplies it and requires cleanliness. Full orchestration
must be able to communicate with the existing local VM; its compile/test workers
use the P5/P6 no-network/no-device sandboxes. Do not wrap the entire orchestration
in a sandbox that prevents the local VM transport and then silently drop worker
isolation to compensate.

`--mode contract` verifies the canonical workflow, runs the real CLI metadata
lock command and help contracts, and executes all `maintenance/test_*.py` and
`tests/test_panda_ci.py` cases. Empty test discovery and skipped cases fail.
`--mode full` runs, in order:

1. `songplus_compat.py verify-lock`
2. `songplus_compat.py replay --output <run>/replay`
3. `songplus_compat.py validate --linux-instance ... --linux-root ... --output <run>/validation`
4. `songplus_compat.py release-check --report <run>/validation/result.json`

The final command deliberately returns **3** when complete offline evidence still
cannot authorize deployment. The CI wrapper accepts that exit only with a valid
JSON result containing `offline_compatibility_complete: true`,
`deployment_ready: false`, and nonempty `deployment_blockers`. Missing or altered
evidence, an arbitrary exit 3, an empty blocker list, or claimed deployability
fails the job. Exit 2 is an evidence failure. A green CI job therefore means
offline maintenance acceptance, never an installable or vehicle-ready release.

## Editing the workflow

`tools/check_panda_ci.py` contains the exact reviewed workflow template. Generate
the YAML with `python3 tools/check_panda_ci.py --print-workflow` and review both
changes together. Exact comparison intentionally rejects event/permission,
runner, isolation, action-pin, upload and install-step drift; this is not a
general YAML parser. Update the P7 project lock after an intentional reviewed
change, then run the contract again. This local validation does not replace a
future real GitHub syntax/runner execution check.
