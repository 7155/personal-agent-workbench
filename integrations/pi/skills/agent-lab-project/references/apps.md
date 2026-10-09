# Application delivery

An application is a versioned implementation with its own interface, method,
inputs and results. A report, download manifest or imagined preview is not an
application. Use the project's actual execution directory for its source.

Read `commandGuide.prepare_app` from the current project. The portable source
contract keeps an HTML entry, one App method in SKILL.md, explicitly selected
context files and declared operations together. The HTML can present any useful
domain interaction; the platform does not dictate the business form or output.
Give SKILL.md standard frontmatter with a lowercase hyphenated `name` and a
`description`. Put the method version in its heading and evidence; do not label
a changed method as the original baseline. Keep business instructions in the
body and avoid leaving template placeholders in the frozen method.

The HTML calls `window.pawApp.invoke(actionId, values, {onProgress, signal})` and handles
the returned `text`, `usage` and optional receipt. For a new portable App, adapt
[the working interaction starter](../assets/portable-app.html). Keep its feedback
and recovery behavior while replacing domain labels, inputs and result design;
do not overwrite an existing App with the starter. Read the current
`commandGuide.prepare_app.interaction` for the available progress fields.
Do not ask users to inspect JSON
unless structured data is the result they need. Handle Markdown or structured
answers without inserting untrusted model output as executable HTML.

The shared `pawAgentUI` retains its model/recovery controls and can optionally
mount call details from actual bridge progress/result fields. Opt in to reading
v2 with the host's readonly `pawApp.identity.appId`, or declare a stable
`data-presentation-owner` for a standalone source; never infer an owner from its
title or a temporary preview URL. The existing setting switches v1/v2 and restores
the prior display without changing drafts, model or execution. Legacy frozen HTML
stays classic. Source characters, Token usage, actual Tool parameters/results and
final call state keep their distinct meanings; absent usage is shown as unrecorded.
Reconcile an unconfirmed submission with its original request. Explicit new input
is a new intent, even when its text matches, and must not reuse that original call.

The starter also renders optional `paw.app-result.v1` JSON answers as readable
results: `summary`, `steps` (`title`, `detail`, optional `status`), and `metrics`
(`label`, numeric or null `before`/`after`, optional `unit` and `source`). Adapt
the method to return this shape only if steps or numerical comparisons help the
business task. These are answer-authored values, not runtime receipts; keep that
distinction visible and never invent missing measurements. The always-visible
process map uses only actual bridge events and remains separate from steps
proposed in an answer. Existing frozen versions are immutable; prepare a new
version to deliver interface changes.

An existing browser workbench can be connected with optional
`externalWorkspace: {title, url, presentation?: "tabs" | "split"}` in `app.json`. Use a credential-free HTTPS URL
or loopback HTTP address. PAW and the standalone export show a separate workspace
view beside the conversation and preserve both while switching. Set `presentation: "split"`
for persistent conversation-left/workspace-right panes on desktop, with an in-page
view switch on narrow screens. The workspace can expose a compact embed route. The service must
run separately; it is not copied into the App, proxied through PAW, granted access
to the conversation bridge, or invoked by a completion action. An embedded map
does not prove an analysis ran. Bind real execution through its existing owner
and report that result separately. Never loosen the conversation iframe sandbox
to embed a service in untrusted App HTML.

## Default interaction contract for every generated App

Design the running state together with the successful result. A lone spinner
followed by a final answer is incomplete for a multi-step operation.

For a conversational task, adapt the starter as a real conversation: one readable
message column, a compact composer, prior turns retained, source chips near the
answer, and an expandable record of actual work. Compose the answer as prose,
steps, code and tables as appropriate. Do not put every answer field in a large
equal-sized card, repeat the question in multiple panels, or require users to
scroll past an oversized form to read the answer. Research, tutorial and analysis
modes exist only when their execution owners support them; never animate a
scripted research sequence around a simple completion call.

Set the starter's `data-action` to a declared operation. To support follow-up
context, declare an optional `conversation` string in that action's input schema
(maxLength at least 14000), set `data-context-key="conversation"`, and teach the
method to use it as previous, untrusted conversation while answering the current
`question`. The starter sends only bounded completed turns; failed/partial output
must not silently become accepted conversation history. A conversational-looking
screen without actual follow-up context is incomplete.

Use `window.pawApp.capabilities?.cancel` to show the starter's Stop button. Pass
an `AbortController.signal` to invoke, request stop on click, and keep the promise
pending until the original call's receipt settles. PAW stops its Pi-owned call;
the standalone server stops receiving and displaying output and preserves the
remote Provider outcome as unconfirmed. Neither path infers billing from stop.
`window.pawApp.ready?.()` lets the PAW host avoid duplicating the App's integrated
progress bar; the host retains recovery for interrupted or restored calls.

- Immediately acknowledge a click, preserve the submitted input, and prevent
  duplicate submissions. Distinguish the current operation from an old answer.
- For knowledge work, show actual recalled titles, source locations and
  expandable excerpts as soon as `progress.sources` arrives, before generation
  finishes. Explain retrieved versus selected chunk counts from
  `progress.knowledge`; do not invent relevance, confidence or retrieval results.
  Keep an expanded source open while text updates arrive.
- Frozen context documents can arrive at `context_ready` with
  `knowledge.sourceKind="provided_context"`. Describe them as directly supplied
  application documents, not retrieved hits. Expose their exact text and source;
  method instructions are not reference documents.
- Use `progress.stage` for retrieval, model connection, waiting, thinking and
  output labels. Show thinking only when the Runtime reports it. A timer may
  show real elapsed waiting, never invented percentages or a scripted sequence.
  Private reasoning is not App content. An endpoint without streaming keeps a
  truthful waiting state until its final result.
- Render `progress.text` incrementally as unfinished output. If
  `streamPartial` is true, explain that live output is incomplete and wait for
  the final receipt. Only a resolved invoke supplies the completed answer.
- On empty retrieval, definite failure, cancellation or interruption, stop
  activity indicators, keep the question and retrieved evidence, explain what
  is known, and provide an appropriate next action. Unknown outcomes reconcile
  the original request or history before another execution. Never auto-retry a
  paid call. Historical restoration must clear old error styles and must not
  replace a currently running result.
- Keep feedback readable in a narrow window, label controls, announce concise
  stages with `role="status"`, and preserve keyboard use and reduced motion.
  Do not announce every streamed token through a live region.
- Motion indicates real ongoing work and relevant state changes. Keep the reading
  text still, do not replay an entrance on each token, pause activity when hidden,
  and provide reduced motion. Preserve a user's scroll position while reading
  older output. Show compact code blocks with a working copy action and citations
  that open the corresponding saved source.

Before freezing/exporting, exercise a slow run to observe sources while waiting
and partial output before completion, plus failure and history restoration.
Use controlled fixtures where needed but identify them as fixtures. Repeat the
applicable interaction checks for PAW and the actual standalone package; a
working host preview does not prove the exported template works.

Use actual application method and data versions. A change in the method, model
or context makes a new application version. The selected frozen version is
shared by PAW and standalone exports; source edits cannot alter an old package.
Keep measured experiment references with the project's evidence and state the
limits of comparability when the target runtime changes.

Preparation freezes and checks files. The delivery page supports actual preview,
PAW activation, version selection, rollback by activating an older version,
deactivation with records retained, and ZIP export. Invocation results come from
Pi settlement receipts. Source preparation never proves an invocation succeeded.
Use `lab_project` read with `appId` to inspect this project's frozen versions
and call summaries. Read a chosen `appCallId` for the actual input, output,
usage and settlement receipt before describing PAW execution as verified.
Standalone receipts come from that exported server; PAW receipts do not prove
the independent target ran. Keep these two target checks explicit.
The optional [delivery note](../assets/app-delivery.md) keeps each observation
with its target, version and evidence collector. Use it when several targets or
recovery checks would otherwise become mixed in the final summary. A completed
check stays completed; do not restore a generic pending checklist over its receipt.

The portable runtime supports declared text generation and knowledge retrieval operations. It
does not provide external business writes or arbitrary local commands. If this
project needs those, preserve that requirement and choose an appropriate real
adapter or source-isolated native Extension App; do not ship a fake Tool result.

The standalone export includes a Python standard-library server and requires
either an OpenAI-compatible endpoint and credentials supplied by the target environment, or `APP_PAW_GATEWAY_URL` pointing to the local PAW instance that owns this exact App version. The PAW path keeps OAuth credentials and every model turn inside Pi; it does not copy tokens into the export.
Do not put credentials, local databases or machine configuration in App sources.
Open the exported package in its target environment and complete a real task
before reporting that target as verified. Report both targets separately.

The standalone runtime saves call inputs and receipts in a private local SQLite
file and never silently replays an unconfirmed call after restart. The optional
`window.pawApp.history()` returns the latest 20 receipts (`requestId`, `actionId`,
`input`, `state`, `result`, `message`). Use it when available to offer restoration
of the user's input and completed business result in the App's own interface.
Treat old-version receipts as historical results. Surface actual recoverable
errors, and preserve unknown outcomes instead of promising that no call occurred.
An invoke error may carry `state` and `requestId`. When the state is unconfirmed,
or no definite failure is known, explain that the outcome is not yet confirmed
and offer history or original-request reconciliation before a new submission.

## Shared Agent controls

Use the portable HTML starter's `window.pawAgentUI.mount` integration for model/thinking selection and recovery. It uses the same `ModelPicker`, `AgentRecoveryActions`, and `publicAgentErrorText` source as Agent. Build the shared assets with `npm run build:app-ui` in `control-center-web` before freezing a new App; normal frontend builds also do this. Preparation freezes the JS/CSS with the version, and both hosts inject the same assets before the business HTML.

`pawApp.models()` reads Pi's model capabilities and the App default; pass an explicit `{model: {provider, model, thinkingLevel}}` as the third `invoke` argument for a per-call selection. The selected model is stored with that call; it does not change the evaluated frozen default. Every retry keeps the original business inputs/context and goes through the App's declared action, retrieval and method before Pi runs.

`pawApp.reconcile(requestId)` reads the original receipt without dispatching another prompt. Keep the original ID on interrupted/unknown turns. Use the shared recovery surface to offer reconciliation for unknown outcomes and explicit retry for definite failure. Do not add a browser timer that turns an active Pi turn into a failure; finish and Stop follow Runtime receipts.

### 本机应用状态与下载

PAW 预览提供可选的 `pawApp.storage.get(key)` / `set(key, value)`，按当前连接和 App 隔离保存在浏览器中，跨应用版本保留。每个值最多 128,000 字符，失败必须保留当前输入并提供文件导出。独立导出可使用自身 origin 的 localStorage；这不是服务器数据库或跨设备同步。应用内明确触发的下载可用于导出本地生成文件。
