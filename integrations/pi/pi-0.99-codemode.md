# Native Pi 0.99 codemode in PAW

The reviewed upstream baseline is Pi 0.99.2. PAW loads its native
`createCodemodeExtension`; it does not add a second JavaScript executor or
another Room execution owner.

`codemodeMode` has three effective Session settings: `on` exposes ordinary
tools and codemode, `only` exposes the code tool with native tool discovery,
and `off` disables codemode. The existing mode update API changes idle Sessions
through `session.codemode.set`, requires the Host response, and persists the
preference with the exact Runtime binding. Busy Sessions reject the change.
Hosts that do not declare the capability retain their older behavior.

The sandbox calls the same authorized native/product tools, including the
original Gateway admission and cancellation path. Optional direct model
helpers are disabled: Pi/Room model routing still owns model execution.
Nested calls retain their parent ID, exact arguments, and actual status in
Pi's transcript. PAW groups them under the code card and restores them on
reopen. Missing or unfinished results remain unfinished; a caught internal
error can coexist with a successfully completed outer script.

The managed payload ships the native worker and QuickJS WASM. Its source
checks cover parallel Gateway requests, invalid parameters without execution,
failed tools, exact cancellation with physical drain, mode updates and cold
history. Deterministic staged payload checks establish packaging and RPC
behavior; they do not establish real provider or multi-partner Room acceptance.
Installed version and foreground acceptance must be checked independently.

Normal execution uses the user's selected model and thinking level. Enabling
codemode does not change reasoning to max, increase Room partners, or promise
token or latency savings for a complete task.

The managed SDK loader also loads native MCP and `tool_search`. The built-in
MCP owner reads this Session's managed agent directory and trusted project
configuration. Its read-only status callback publishes connection states and
per-tool MCP exposure without command arguments, transport URLs or credentials.
`tools.list` supplies that projection to the existing Session command catalog;
the frontend's MCP tab offers inspection, refresh and idle native login/reconnect.
The MCP server exposure and Pi core tool exposure are distinct: native MCP's
`codemode` maps to deferred core discovery. The UI uses the MCP owner's per-tool
value, including overrides, instead of guessing from the core value or name.
An unavailable or replaced native owner remains unavailable in the inspector.
