# PAW ego-browser integration

PAW vendors the open-source `ego-browser` harness and the platform-neutral
host implementation from CitroLabs' `ego-lite` repository. The browser UI and
Chromium process remain PAW-owned; this integration does not install or launch
the closed-source ego lite macOS application.

- Upstream: <https://github.com/citrolabs/ego-lite>
- Harness/Skill source revision: `27e951b1465beafd85526165f1955593b3325e64`
- Host source: upstream pull request #202 (`feat/linux-host-persistence-upstream`)
- License: MIT; see `upstream/LICENSE`

The upstream directory is kept intact so provenance and future rebases remain
auditable. PAW supplies its own lifecycle adapter through `BrowserControlService`:

1. PAW starts one isolated Chromium profile and discovers its random loopback
   DevTools port.
2. The open host attaches to that existing endpoint and therefore never owns or
   launches a second browser process.
3. The open `globalThis.ego` bridge and `ego-browser` harness execute inside a
   short-lived, PAW-audited command while the host daemon serializes Task Space
   ownership and persists tab sets.
4. PAW retains Session identity, direct capability scope, visible trace, Stop, install and rollback
   responsibility.

Do not replace this integration with the upstream app installer or its DMG. The
closed app is neither required nor shipped by PAW.

Build the vendored helpers with `python3 scripts/build_ego_browser_runtime.py`.
This calls both upstream package builds and stages a self-contained runtime at
`build/ego-browser-runtime/upstream`, with a file-hash manifest. Its validation
uses CLI help and module imports; it does not start a daemon or browser.

The Sidecar installer ships this runtime to
`app/integrations/ego-browser/upstream`, which is the default path used by
`BrowserControlService`. The binary installer carries the compiled payload and
uses its bundled Node without rebuilding or installing npm dependencies.
For a missing-files repair, the same build entry accepts `--from-built`,
`--source`, and `--output`; existing output requires explicit `--replace` and
is replaced only after the staged payload validates. Installing these helper
files does not require restarting Gateway, Pi, or Sidecar.
