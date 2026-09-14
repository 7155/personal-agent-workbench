# PAW Team deployment

The team entry point serves one team from a dedicated server. Each member signs
in through a browser or PAW HTTP client. A personal space contains that member's
Sessions, Memory, Knowledge and files. A project is an explicitly shared space:
its members see project Session messages/tool history, Room progress and published drafts. Ordinary
Session controls remain with the human who created the Session.
Use personal space for private discussion; per-Session private visibility inside
a project is not currently exposed by the Team deployment.

This is a separate deployment from the macOS personal Gateway. It never imports
the operator's personal database, model environment, browser profile or Skills.
Platform administrators manage accounts; project owner/maintainer membership is
still required for project management. A trusted host operator can access server
storage; application roles do not protect data from host root.

## Shared project material

Open **Knowledge** in the selected space, or **项目资料** from the project
workbench. The heading identifies the space and whether material is personal or
shared with project members. Libraries, source files, indexes and Memory stay
in that space's data root; switching spaces does not copy them.

| Role in the selected space | Read, search and download | Import documents | Create, configure, rebuild or delete libraries |
| --- | --- | --- | --- |
| Personal-space owner | Yes | Yes | Yes |
| Project owner / maintainer | Yes | Yes | Yes |
| Project contributor | Yes | Into an existing library | No |
| Project viewer | Yes | No | No |

Library managers explicitly enable a library for Agent use. Members use the
existing Knowledge search and source reader; Agents use the normal scoped
Knowledge tool. A removed member loses new access, while material already
accepted into the project remains available to its members.

Browser uploads use the original document body with the PAW login, Origin and
CSRF checks. The server accepts at most 8 MiB per file and gives the body transfer
30 seconds, then checks current write access before intake. An upload receipt
confirms intake; the document's indexing status separately reports readiness or
failure. Archive entries and expanded Office content have smaller Team limits.

Team parsing currently accepts text, Markdown, RST, CSV/TSV, JSON, HTML and
DOCX/PPTX/XLSX through the built-in parser. PDF and scanned-image OCR are not
enabled in the shared parser. Use a text or Office source for those materials.
Search uses the local keyword index; external embedding services, model graph
extraction and arbitrary regular-expression searches are unavailable. These
operations do not inherit the host's personal Provider configuration.

## Start the server

Use Python 3.12+, Git, a dedicated non-root service user and a private local
filesystem for the team data directory. Install the normal project dependencies
from [CONTRIBUTING.md](../../CONTRIBUTING.md), then build the frontend:

```sh
cd control-center-web
npm run build
cd ..
python -m rag_ime.team bootstrap-admin --data-root /srv/paw-team --username admin
python -m rag_ime.team serve --data-root /srv/paw-team --port 8770
```

`bootstrap-admin` prompts for the initial password and only works for a new team.
Passwords never belong in command-line arguments. Sign in at
`http://127.0.0.1:8770`, add members, create a project and assign its members.
The data directory must have mode 700. Without a runtime configuration, account,
project and stored-data operations are available; Agent execution reports that
the isolated worker is unconfigured.

### Import a project from a local Git repository

The trusted host operator can provision a project from an existing local
repository after the team account has been bootstrapped. Stop the Team server
while provisioning and use a reviewed source repository without active writers:

```sh
python -m rag_ime.team provision-project \
  --data-root /srv/paw-team \
  --owner admin \
  --name "Registration" \
  --source-repo /srv/repos/registration \
  --branch main
```

`--owner` is an existing active team username. `--source-repo` must be a local
trusted Git repository, and `--branch` must name an existing local branch. The
command copies the selected committed branch into a new server-owned project
repository and prints its project ID, branch, head commit and revision as JSON.
Uncommitted source files are ignored; the source repository is not modified.
The command does not fetch from a remote, run hooks or tests, install packages,
start a Host, or use Provider credentials. It is a trusted-host provisioning
operation and is not an HTTP or remote platform-admin capability. Invalid
preflight input creates no project. If the final import fails, the error names
the newly created empty project so an operator can recover it; the command
returns non-zero and never reports a successful import.

For team access, put this listener behind an HTTPS reverse proxy and specify
`--public-origin https://paw.example.com`. Keep the listener on loopback when the
proxy runs on the same host. Non-loopback listeners require an HTTPS public
origin. Forward SSE without buffering, keep the origin unchanged and do not
rewrite the `/team/spaces/<id>/api/...` path. The server checks the configured
origin, an HttpOnly session cookie and a CSRF token; loopback is not a login
bypass.

## Configure isolated execution

Run the Team controller on Linux with a local Docker daemon. The controller and
daemon must resolve every bind-mount source to the same local filesystem,
including the Team data directory and the controller's private broker sockets
under `/tmp` by default. Docker access alone is insufficient: a remote daemon or a macOS
controller talking to Docker Desktop cannot provide this Unix-socket topology.
For local development, a trusted controller inside the Docker Desktop Linux VM
can use explicitly shared paths. A containerized controller also needs those
paths at identical absolute locations in the daemon filesystem; merely mounting
the Docker socket does not share its private filesystem. The standalone canary
accepts a shared scratch root for that setup, but does not configure a production
controller deployment.

For a containerized controller, add `"brokerSocketRoot": "/srv/paw-brokers"`
to the existing private runtime configuration. Create that short directory in
advance, owned by the service user with mode 700, and make it available at the
same absolute path to both controller and daemon. Each controller creates its
own private random child there; shutdown removes only that child. The server
rejects invalid, symlinked or overly long configured paths before starting a
worker. This is an operator setting, not a browser-supplied filesystem path.
Omitting it preserves the normal `/tmp` placement. The data directory must still
be shared at its identical daemon path, and workers receive only their own
attempt socket rather than the shared broker parent.

The trusted controller may access Docker; the Agent container never receives
its socket. Prepare a reviewed, matching Pi v2
runtime payload with `scripts/build_managed_pi_runtime_v2.py` on the build host.
The existing builder checks source/runtime compatibility. Do not substitute an
arbitrary Pi checkout or claim Linux acceptance from a macOS smoke result.

Build the worker using the explicit payload context:

```sh
docker buildx build --build-context pi_payload=/path/to/reviewed/pi-payload \
  -f deploy/team/worker.Dockerfile -t paw-team-worker:local .
cp deploy/team/runtime.example.json /srv/paw-team-runtime.json
chmod 600 /srv/paw-team-runtime.json
```

Edit the private runtime file with the team's model endpoint/key, model ID and
validation command. Set the image to a digest in a deployed installation. The
example validation command is for a Python project; install the actual project's
toolchain in the worker image and configure an appropriate test/build command.
The worker UID/GID defaults to the service account's UID/GID so it can write its
own bind mounts. Do not run the server as root.

```sh
python -m rag_ime.team serve --data-root /srv/paw-team --port 8770 \
  --public-origin https://paw.example.com \
  --runtime-config /srv/paw-team-runtime.json
```

Each Session has an independent checkout and Git metadata. Each execution attempt
has its own Pi Host, HOME, settings and temporary files; transcript storage stays
with the Session across attempts. A short-lived attempt capability is
checked against current membership before model/tool admission. A private Unix
socket carries model/tool requests through the broker; containers have no
external network. Only the broker holds the upstream model key. The worker image
must contain required packages: arbitrary Internet package installation is not
enabled from an Agent task.

Limits currently cover per-container CPU, memory and PIDs, member/team concurrent
Hosts and tool workers, model request concurrency, daily request counts and bounded tool output.
Daily request counts are not token or monetary billing limits. Place the data
directory on a quota-managed volume for a hard disk limit. Background shell jobs,
LSP and native desktop/browser bridges require additional scoped adapters and
are not exposed as team tools. External accounts use the connection service below.

## Shared Apps, Pi Packages and Skills

Administrators publish exact versions in App Center. Project owners and
maintainers select published versions for the project; personal-space owners
select their own set. Contributors and viewers can inspect the project set.
Platform administration does not grant access to other users' private spaces
or to projects the administrator has not joined.

By default the server reads the existing `rag_ime/plugin_catalog.json`.
Operators can supply another catalog using the same schema and an explicit
trusted source root. For a source checkout, the example includes Session
Review and the existing 掌柜问数 App Package with its Skill:

```bash
python -m rag_ime.team serve --data-root /srv/paw-team \
  --frontend /opt/paw/control-center-web/dist \
  --package-source-root /opt/paw \
  --package-catalog /opt/paw/deploy/team/packages.catalog.example.json
```

Both options are required together. Keep these sources operator-controlled.
Publishing copies bounded, validated Package bytes to content-addressed team
storage; it does not run JavaScript, an installer, or a dependency download on
the server. Packages needing additional dependencies must use a compatible
reviewed worker image. Arbitrary remote package installation is not exposed.
The page host for a vertical App must also be part of the matching frontend
build; a manifest alone cannot supply executable frontend code.

The existing 掌柜问数 page uses the current Team space and creates a separately
owned Session for each member. Restoring a task checks its fixed Package
snapshot against that page's App version. An older or empty snapshot is not
silently treated as the current App. Host directory selection and the personal
sandbox experiment are unavailable in Team mode. Apps that require that
sandbox cannot be opened until a Team adapter implements their contract.

Every new task records the exact selected versions and selection revision.
Updating a project affects new root tasks; existing tasks and their children
retain their original resource snapshot. “Stop distribution” prevents new
selection of a version. If a space still selects that version, replace or
remove it before creating another root task. Existing tasks may continue;
this action does not claim to revoke a running task. Account, membership and
task revocation continue to use the execution controller.

At execution time only that task's fixed source directories are mounted
read-only. Its own Pi Host performs the native Package validation, preview
and installation protocol before opening the Session. The controller does
not construct another plugin runtime or reuse a member's resident Host.
Named Skills come only from the selected Package roots; personal/Codex Skill
directories and credentials are not inherited. External connections and
model service credentials continue to use their separate scoped brokers.

The published and selected states are durable configuration, not evidence
that a task has run. Real loading, execution, cancellation and container
removal require the configured OCI worker and matching Pi payload.
Retiring a task Host closes its startup admission before cleanup; a delayed
Package preflight cannot restart a discarded Host. If container startup or
removal has an uncertain outcome, execution remains pending recovery until
the controller verifies that the earlier container is absent.

## Personal and project external accounts

Install the optional server dependency with `uv sync --extra team`. Copy
`deploy/team/connections.example.json` to an owner-only configuration file outside
the repository, then pass `--connections-config /srv/paw-connections.json` to
`serve`. An empty object enables GitHub fine-grained personal access tokens.
With no configuration file, the interface reports connections as unconfigured;
the server does not create a vault or require the optional encryption dependency.

To enable browser sign-in, register a GitHub OAuth App with callback URL
`https://paw.example.com/api/team/connections/github/callback`, set its client ID
and secret in the private configuration file, and set the matching
`--public-origin https://paw.example.com`. The flow uses a single-use state,
PKCE S256 and the original PAW login session. Pending handshakes expire after ten
minutes and are invalidated by a server restart. Callback codes and provider
errors are never sent to the frontend or the application access log; configure
the reverse proxy to omit callback query strings from its logs as well.
See [GitHub's OAuth flow documentation](https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/authorizing-oauth-apps).

Members open **连接 GitHub** from the account/workspace menu. They choose personal
or project ownership, exact repositories and allowed operations before
connecting. Project owners and maintainers manage project connections. A
platform administrator without project membership has no override. GitHub OAuth
grants `repo read:user`; a fine-grained token can impose narrower provider-side
permissions. PAW's broker always further restricts calls to the selected exact
repositories and operations. Supported operations are repository metadata, file
contents, listing/reading Issues, creating an Issue and commenting on an Issue.
This does not fetch a repository into a task or publish a Git branch.

A member grants one selected repository and a subset of operations to their own
task for a fixed duration (one minute to one day). Read-only tasks cannot write
to GitHub. Grants do not follow delegation, a new Session or a task handoff.
Personal connections stay with their human; project connections remain under
project management when their creator leaves. The underlying GitHub account
still controls its external token and can revoke it independently. In the
current project audience policy, connection results entering a project Session
are visible to project members; the grant UI discloses this before access is given.

Credentials are encrypted using the `cryptography` library's AES-GCM. The
owner-only vault key defaults to `<data-root>/secrets/connections.key`; the
operator can select another absolute path with `vaultKeyFile`. Back up the key
separately from the team database and restore both together. Application roles
do not provide confidentiality from the trusted server operator. Tokens never
enter the Agent environment, workspace, tool result or browser storage.
Revoking a PAW connection erases its stored secret and denies its grants; revoke
the token in GitHub too when external provider revocation is intended.

The server calls only fixed GitHub HTTPS endpoints, verifies TLS and pins a
resolved public address for each connection. It ignores proxy environment
variables and rejects redirects. I/O sizes, concurrent calls and timeouts are
bounded. The default 15-second wall-clock exchange deadline begins after DNS
resolution; DNS uses the operating system resolver's timeout. OAuth tokens with a refresh token rotate on demand; uncertain refresh
requires reconnecting. A stable per-task write request ID records the result in
SQLite. A repeated successful request returns its known receipt after a fresh
authorization check; an unfinished or uncertain write is not sent again.
Receipts retain only an Issue/comment identifier and derived GitHub link, not
submitted titles or bodies. A damaged successful response remains uncertain.
Canceling a task before token refresh is dispatched preserves other members'
project grants. GitHub authentication errors ask to reconnect that account and
do not sign the member out of PAW. Listings show at most 512 recent connections
and grants; the task picker shows up to 100 current tasks from 300 recent owned
bindings. A task grants listing is separately bounded by its configured grant cap.
Revocation denies subsequent calls and result delivery, but cannot undo an
operation GitHub already accepted. Inspect GitHub before intentionally submitting
a replacement for an operation with an unknown outcome. Historical operation
receipts remain on the quota-managed team volume.

## Collaborate and integrate

1. Open the project workbench and publish its objective and acceptance criteria.
   Each edit creates an immutable requirements version; concurrent edits must
   be compared before saving against the new version. The workbench shows
   members, Room assignments, fixed deliveries and the stable branch together.
   The project owner creates a Room. Each contributor adds their own Agent to it;
   different members can use the same reusable Agent persona.
2. Agents work in independent checkouts. Project members can view progress;
   another member cannot directly prompt, rewrite or edit an owned Session.
3. A new Session uses a fixed requirements version, and child Sessions inherit
   their parent's version. Publishing a new brief marks older work baselines
   and drafts as out of date. The Session owner can stop its old execution and
   explicitly adopt the latest requirements; this updates the context used by
   subsequent execution, and does not automatically start work or certify that
   the code satisfies the new requirements. An existing child's baseline stays
   unchanged when its parent adopts a newer version.
4. Publish a draft from an owned Session. Publication stops its current workers and
   exports an immutable version using safe file traversal. Agent-controlled Git
   hooks, config and credentials are not run by the trusted integration service.
   Members can inspect its diff and explicitly adopt that version into an owned
   Session. Publication skips known private configuration names and reports the
   skipped paths; this is not a general-purpose secret detector. Symlinks are
   currently rejected. A snapshot is limited to 4,096 files, 8 MiB per file and
   64 MiB total, so dependency caches and large media should stay outside source drafts.
5. An owner or maintainer integrates a draft. The service merges against the
   latest project branch, validates the combined candidate in a separate
   container, then advances the branch with a Git compare-and-swap. A conflict
   or failed validation leaves the stable branch unchanged. The draft must use
   the current requirements version, checked again in the final branch update
   transaction; changing requirements during validation cannot advance an old
   candidate. Recorded historical integration outcomes remain replayable.
6. Room work items record an objective, expected output, acceptance criteria and
   a responsible Agent. Reassigning a work item updates that responsibility;
   it does not start Pi or transfer the former owner's credentials. The target
   must have current project execution authority. Removing a project member
   revokes task capabilities and requests verified
   container termination. Re-adding the member does not revive old grants.
   Published project drafts and results remain project records. Data already
   downloaded by a former member cannot be recalled.

Only the integration service writes the trusted project repository. Parallel
Agents never share its writable `.git` directory. The first implementation uses
one metadata SQLite database plus a separate existing PAW database per space;
use a local filesystem and one team server process. This is not a distributed
control-plane deployment or a PostgreSQL migration.

## Shared project HTTP previews

Project maintainers can start or stop a shared preview from the project
workbench. Members open it in a separate tab. The preview is a project-owned
service: closing a browser or removing its original initiator does not stop an
already active preview. Access still requires current project membership and a
live PAW login. This is an internal preview, separate from external production
deployment and production databases.

Put a versioned `paw-preview.json` in the project repository, for example:

```json
{
  "schemaVersion": 1,
  "command": ["python3", "-m", "http.server", "3000"],
  "port": 3000,
  "healthPath": "/",
  "startupTimeoutSeconds": 30
}
```

The command can serve a full HTTP application with its own API. An optional
`prepareCommand` argument array runs before the app and must exit successfully.
The approved image must already contain its required toolchain and dependencies;
the preview has no external network. Project code runs only inside OCI, from a
private writable copy of a read-only fixed snapshot. Runtime data belongs to
that deployment and is disposable; this is not a shared production database.

Copy [preview.example.json](preview.example.json) to an owner-only configuration
file (`chmod 600`), set the approved image and your dedicated preview domain,
then pass `--preview-config` alongside `--runtime-config` when starting Team.
The execution configuration supplies the isolated verification command. A
successful applied integration receipt can be reused for its exact commit and
requirements version. Imported versions without a receipt are verified first.
Successful integration requests a preview refresh when previews are configured;
Git integration and preview readiness keep separate outcomes.

Route wildcard HTTPS hosts such as `*.previews.example.com` to the internal
preview listener, preserving the original `Host` header. Use a dedicated domain
and wildcard certificate; never route these applications under the PAW console
origin. The reverse proxy should not log query strings on `/__paw/enter`, because
entry URLs contain short-lived, one-use tickets. Keep both internal listeners
on loopback or a private, access-controlled network behind the trusted TLS proxy.
Local development may use `http://{deployment}.localhost:8771`; configure wildcard
loopback resolution if the browser or environment does not resolve `.localhost`.
The local HTTP fixture tests do not prove browser DNS or wildcard TLS setup.

Each deployment has its own origin. The PAW console redirects through a 30-second
one-use ticket to a 15-minute host-only HttpOnly preview cookie, then removes the
ticket from the address. Reopen the preview from the project when that lease
expires. Every request rechecks the original login and membership generation;
logout, disablement and removal invalidate access. Viewers can send read methods
only. PAW cookies, control tokens and Authorization headers are not forwarded to
project code. Project app cookies cannot claim a parent domain or reserved PAW
names. The gateway constrains redirects, CORS and browser connections to the
deployment's origin. Bundle assets locally. WebSockets, external OAuth flows,
external service calls and external production publication are not supported by
this preview path.

A candidate must pass verification, preparation and HTTP health before the
active pointer changes. Activation rechecks the stable commit, requirements
revision and initiating maintainer's membership. Failure preserves the previous
active preview. Existing tabs remain pinned to their deployment so their assets
cannot silently mix with a new version. At most one prior deployment is retained
per project for up to 15 minutes; reopen the project link after it retires.
The manager bounds concurrent starts to two and live deployments to eight for
the single server. These limits are operational defaults, not billing claims.

Stopping denies new preview requests before container cleanup. Until removal is
verified, status remains `recovery_required`. Server restart also invalidates
past live claims and cleans up owned containers; it does not automatically
replay an uncertain start. Start a fresh preview after recovery completes.
The gateway-to-container hop uses a Unix socket with network isolation, and no
PAW login cookie, model key or Docker socket is mounted into the application.

## Verify and operate

Use the focused `tests/test_team_*.py` tests and normal repository boundary
checks. HTTP fixture tests prove application authorization and database scoping;
fake launcher tests prove argument construction only. A deployed worker still
requires a real Linux canary for mount isolation, network denial, process
termination and Pi prompt/stop/recovery behavior.

Run the real canary from the source checkout with the exact reviewed worker
image. The image is mandatory and is never discovered, built or pulled by the
script; use the reviewed payload digest in a deployed environment:

```sh
python3 scripts/verify_team_oci.py --image paw-team-worker@sha256:<reviewed-digest>
```

Run it as the same dedicated non-root service account that owns the Team data
directory; the canary uses that numeric UID/GID for its bind mounts and rejects
root execution. Its temporary directories default to `/tmp`. For a trusted
containerized test controller, pass `--scratch-root /path/shared/with/daemon`
using an existing directory available at that exact path to both controller and
daemon. Keep the path short enough for Unix-domain socket limits. This option
changes only the canary's temporary location; it does not expose a TCP broker or
grant the workers Docker access.

The canary creates only temporary, uniquely named actor containers and a
localhost-only fake model/tool broker. It reports per-check `passed`,
`failed`, `unverified` and `blocked` lists as JSON, and returns non-zero for a
missing Docker daemon, a skipped Pi protocol stage, any unverified boundary, or
any failed verification. The standalone run therefore keeps Team grant
revocation unverified; use the authenticated Team coordinator checks for that
boundary.
`--skip-pi` is available when checking only kernel mounts and process limits;
that result remains unverified for Pi prompt/tool/restart behavior. No real
Provider key or endpoint is used.

Keep backups of the team metadata database, each space database and trusted
project repositories together. Quiesce tasks before filesystem backups. Treat
`execution/*/active-container.json` and stop receipts as runtime recovery records,
not instructions. The server verifies the recorded container identity before a
Session resumes. One-shot tool workers use the same recovery records, quota and
revocation boundary. Stopping a task or publishing a version also stops those
workers; a missing Docker daemon is not proof of successful termination. After a
server restart, review interrupted work and explicitly resume it; the server does
not replay uncertain actions. Never expose the data directory through a static web server.

Preview diagnostics are operator-local. Prepare and app stdout/stderr retain at
most 64 KiB each (256 KiB per deployment in total) under
`project-preview-runtime/diagnostics/<deployment>/`, separate from disposable
attempt mounts. These raw logs are not a project HTTP API. The current limit is
per deployment; operators must manage retention of historical diagnostics and
metadata alongside the team data backup policy. Very long data paths that exceed
the platform's Unix socket address limit fail with a configuration error.

On shutdown the server first denies preview access, stops candidate validation,
seals the preview runtime against late starts and waits up to 10 seconds in total
for startup jobs. A pending job is reported explicitly and remains a recovery
condition, rather than being labelled a successful shutdown. Retired preview
cleanup runs every 15 seconds, so transient retained processes may await that
cleanup or a verified removal retry.
