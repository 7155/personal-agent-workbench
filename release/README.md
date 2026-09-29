# Build, Install, And Release

This directory separates source, preview installers, and notarized distribution:

| Scope | What it proves | Current gate |
| --- | --- | --- |
| Public source | The tracked repository is clean, licensed, and free of known private artifacts or secret-shaped content. | `python3 scripts/check_public_release.py --repository-only` |
| Local source build | The Web Control Center, native helpers, and pinned managed Pi Runtime can be built from source on the current Mac. | The non-destructive walkthrough below |
| Unsigned offline preview | A DMG installs prebuilt desktop/voice/bridge apps, Python and managed Pi through the existing stack installer. No developer tools are needed on the target Mac. | Payload hashes, native signatures, staged Pi canaries and installed component audit; no Developer ID or notarization claim |
| Public macOS distribution | A package is Developer ID signed, notarized, stapled, foreground-accepted, and hash-bound to its corresponding source. | `python3 scripts/check_public_release.py` |

The source and preview scopes can pass while notarized distribution remains
blocked. An unsigned preview must be labelled explicitly; it does not satisfy
the signed public macOS distribution gate.

## Offline Preview Installer

Download the `macOS-arm64-unsigned.dmg` asset from the GitHub Release, check its
`SHA256SUMS.txt`, open the image and run **Install Personal Agent Workbench**.
The installer targets the current user's `~/Applications` and keeps its bundled
runtime under `~/Library/Application Support/RagIme/InstallerGenerations` so the
image can be ejected after installation. Existing databases and credentials stay
in place; previous components are copied to `InstallerRecovery` before update.

The preview requires Apple Silicon and macOS 14+. It does not need Git, Xcode,
Node, pnpm or Python installed separately. It is ad-hoc signed, without Developer
ID or Apple notarization, so macOS may require an explicit Open Anyway decision
in Privacy & Security. Voice permissions and Provider credentials are configured
in Input Studio after installation. Patched Squirrel and MLX model weights are
optional and are not included in this installer.

Packagers use `scripts/build_macos_installer.py --help` with explicit prebuilt
apps, Pi payload/source, a portable Python runtime with the project's runtime
dependencies, and the pinned Electron license notices. The manifest records
product and installer revisions separately: packaging-only script updates do
not rewrite the provenance of already built apps. Corresponding source and
third-party notices are included. The graphical wrapper only prepares a durable
payload and invokes `install_product_stack.sh --binary-payload`; existing
installers remain the owners of LaunchAgents, managed Pi activation and checks.

The `RAG_IME_*` environment variables, `RagIme` Application Support directory,
and existing bundle identifiers are compatibility identifiers. The user-facing
product and Python package are named Personal Agent Workbench; changing the
stored paths or bundle identities requires a separate migration and is not part
of a source-repository rename.

## Source Build Requirements

- macOS 14 or newer on Apple Silicon;
- Python 3.12 or newer;
- Git and Xcode command-line tools;
- full Xcode only when building the patched Squirrel input method;
- Node.js 22.19 or newer;
- pnpm 11 for `control-center-web`;
- npm for the pinned Pi source checkout;
- `uv` for the locked Python environment.

Model weights, API credentials, OAuth credentials, databases, and personal
context archives are intentionally not included.

## Checkout And Dependencies

The managed runtime is built from a separate Pi checkout. Its compatibility is
owned by [`session-runtime-host-contract.json`](../integrations/pi/session-runtime-host-contract.json)
and `scripts/build_managed_pi_runtime_v2.py`. The required handler baseline is
`9c3f93c8b1c409e82e14d458510c146088c44561`; a candidate must also contain the
current contract's methods and source markers. A Pi version number alone is
not sufficient. Use an explicitly reviewed, clean Pi worktree and keep its path
caller-supplied; the installer does not guess a neighbouring checkout.

```bash
# Run from the PAW checkout root. Set this to the compatible Pi worktree.
PI_WORKTREE=/path/to/reviewed-pi-worktree
npm --prefix "$PI_WORKTREE" ci
uv sync --locked
pnpm --dir control-center-web install --frozen-lockfile
```

The build command below checks ancestry and the full host contract before
producing a managed generation. It fails if the supplied Pi source is too old
or incomplete. The Pi runtime is a separate dependency and is not bundled in a
PAW source archive.

## Non-Destructive Build Walkthrough

These commands build into `build/` and do not install, load LaunchAgents,
replace an input method, or restart an application:

```bash
scripts/build_control_center.sh build-release
scripts/build_voice_input.sh build
scripts/build_desktop_bridge.sh build

python3 scripts/build_managed_pi_runtime_v2.py \
  --pi-worktree "$PI_WORKTREE" \
  --output build/managed-pi-runtime/release-check \
  --force

codesign --verify --deep --strict build/RagImeControlElectron.app
codesign --verify --deep --strict build/RagImeVoice.app
codesign --verify --deep --strict build/RagImeDesktopBridge.app
```

The Control Center uses the Electron host in `control-center-web/electron/`.
The local native apps are ad-hoc signed so macOS can execute and inspect them.
That signature is suitable for development only.

Run the source and architecture gates from a clean checkout:

```bash
python3 -m compileall -q rag_ime scripts tests
python3 scripts/check_project_harness.py
python3 scripts/check_import_boundaries.py
python3 scripts/check_route_ownership.py
python3 scripts/check_product_status.py --json
uvx --from "mypy==2.3.0" mypy
uvx --from "ruff==0.14.2" ruff check rag_ime scripts tests
python3 scripts/check_public_release.py --repository-only
```

Frontend verification:

```bash
pnpm --dir control-center-web typecheck
pnpm --dir control-center-web test
pnpm --dir control-center-web build
pnpm --dir control-center-web test:e2e
```

## Local Installation

The installed desktop bundle is `~/Applications/Personal Agent Workbench.app`.
Updates retain the previous app in `~/Applications/.paw-update.*` and keep a
`RagImeControl.app` compatibility symlink for existing launch integrations.
Pinned Dock entries are refreshed in place; unrelated pins keep their order.
The bundle identifier and Application Support directory remain unchanged.


Installation changes `~/Library/Application Support/RagIme`,
`~/Library/LaunchAgents`, and `~/Applications`; `--include-squirrel` also
replaces the user-installed Squirrel bundle. Review the command before running
it.

For a first complete local installation:

```bash
scripts/prepare_squirrel_workspace.sh
scripts/build_patched_squirrel.sh build

scripts/install_product_stack.sh \
  --include-squirrel \
  --include-pi \
  --pi-worktree "$PI_WORKTREE"
```

For an existing installation that should keep its current Squirrel and skip an
optional local MLX predictor:

```bash
scripts/install_product_stack.sh \
  --include-pi \
  --pi-worktree "$PI_WORKTREE" \
  --skip-mlx
```

The installer refuses dirty tracked source, writes source-generation markers,
runs deterministic staged Session and Room lifecycle canaries before activating
the managed Pi generation, installs the visible Control Center last, and checks
that the installed components agree on the expected commit before returning
success.

For an explicitly requested local desktop development install from a feature
branch or a dirty checkout, use the existing Electron installer with this opt-in:

```bash
RAG_IME_ALLOW_DEVELOPMENT_INSTALL=1 scripts/build_paw_os_electron_host.sh install-release
```

Use `build-release` with the same flag to build without installing. The flag
accepts `0` (disabled) or `1`; enabling it is limited to these two source-build
actions. Invalid values and enabling it for preview actions are rejected.
Without it, formal desktop builds and installs require clean `main`
(the existing dirty-main install opt-in remains supported). The app marker keeps
the actual `sourceBranch`, `sourceDirty` and `developmentInstall` values. The
normal signing, checks and `.paw-update.*` backup flow still apply. This flag
does not change the full-stack installer's guards or prove backend alignment
or foreground acceptance.

Inspect the removal plan without changing the machine:

```bash
python3 scripts/uninstall_rag_ime.py
```

Apply that plan only when removal is intended:

```bash
python3 scripts/uninstall_rag_ime.py --apply
```

### Clean Reinstall Without Deleting User Data

Use this sequence when an older local build must be replaced but databases,
governed memory, Sessions, Provider authentication, credentials, logs, and
Rime user configuration must survive.

First verify that the user-installed Squirrel is the single canonical input
method. The second command is read-only and reports whether a duplicate
system-wide bundle would be quarantined:

```bash
python3 scripts/audit_canonical_squirrel_bundles.py
scripts/quarantine_duplicate_squirrel_app.sh --preflight
```

If the preflight reports a same-bundle-id duplicate under
`/Library/Input Methods`, run the quarantine command without `--preflight`.
It moves that duplicate aside and re-registers the canonical user bundle; it
does not delete the canonical Squirrel.

Review and then apply the same narrowly scoped uninstall options:

```bash
python3 scripts/uninstall_rag_ime.py \
  --purge-runtime-cache \
  --report /tmp/personal-agent-workbench-uninstall-plan.json

python3 scripts/uninstall_rag_ime.py \
  --apply \
  --purge-runtime-cache \
  --report /tmp/personal-agent-workbench-uninstall-result.json
```

Do not add `--purge-local-data`, `--purge-credentials`,
`--remove-rime-managed-config`, or `--remove-patched-squirrel` to a clean
reinstall. Those flags intentionally cross the preservation boundary.

Reinstall from the clean source checkout, verify that every installed
component resolves to the new source generation, and only then prune retired
managed Pi generations:

```bash
scripts/install_product_stack.sh \
  --include-pi \
  --pi-worktree "$PI_WORKTREE" \
  --skip-mlx

python3 scripts/check_installed_product_components.py --require-current

python3 scripts/prune_managed_pi_runtime.py \
  --report /tmp/personal-agent-workbench-pi-retention-plan.json

python3 scripts/prune_managed_pi_runtime.py \
  --apply \
  --plan /tmp/personal-agent-workbench-pi-retention-plan.json \
  --report /tmp/personal-agent-workbench-pi-retention-result.json
```

Runtime retention keeps the accepted active and immediate previous
generations for rollback. It removes only digest-bound generations in the
accepted retired lineage and refuses a changed dry-run plan.

## Source Publication And Binary Release

The `v0.1.0-alpha.2` GitHub prerelease is a PAW source preview. Its source
archives contain the tracked product code, documentation, tests, and public
fixtures. They do not include user data, installed business plugins, credentials,
model weights, a prebuilt Pi generation, or a signed macOS installer.
Repository access follows the repository's existing visibility.


Before changing repository visibility, commit all intended source changes and
run:

```bash
python3 scripts/check_public_release.py --repository-only
```

Do not treat `--allow-blocked` as a release pass. It exists only to print the
remaining blockers with exit code zero.

An unsigned engineering staging archive can be assembled with
`scripts/prepare_release_candidate.py` after the patched Squirrel source and
all four app bundles exist. The staging manifest deliberately contains
`releaseEligible: false`.

A distributable macOS release additionally requires:

1. one clean source commit and the exact patched Squirrel corresponding source;
2. completed foreground acceptance for input, voice, Accessibility, and Room;
3. Developer ID signing for every shipped executable and bundle;
4. Apple notarization and stapling;
5. clean-machine Gatekeeper, installation, upgrade, and rollback checks;
6. a generated `output/release/release-manifest.json` matching
   `release/release-manifest.example.json`;
7. a final successful `python3 scripts/check_public_release.py`.

The release metadata files are:

- `feature-registry.json`: feature-level source and foreground status;
- `product-status.json`: verified scope and unresolved release gates;
- `release-manifest.example.json`: the required signed-distribution evidence
  shape.

Never edit a blocker away merely to make the distribution audit green.
