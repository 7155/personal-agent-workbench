# Frontend Agent Guide

These rules apply to UI work in `control-center-web/` and supplement
[the root guide](../AGENTS.md). Preserve its instructions and any more specific
nested guidance. This is a PAW-specific adaptation, not a replacement design
system or permission to expand the task.

## Use the existing owners

- Start with the relevant [source owners](../README.md#架构与职责) and
  [contribution checks](../CONTRIBUTING.md). For visual changes, consult the
  relevant sections of the [design system](../design-system/rag-ime-control-center/MASTER.md).
- Keep product wiring in `src/app`, windows/workspaces in `src/paw-os`, and
  shared domain state, transport and renderers with their `src/features` owners.
  Do not add a parallel router, runtime, request manager or state owner for UI polish.
- Reuse `src/components/primitives`, existing design tokens and
  `MotionProvider` / `MotionActivityBoundary`; extend the shared component
  before duplicating feedback or Agent/Room styles.

## Keyboard, focus and input

- MUST use semantic buttons, links and labelled inputs; icon-only controls need
  an accurate accessible name. Status must have text or an icon as well as color.
- MUST make actions usable by keyboard. Keep visible focus unobscured by fixed
  headers/composers; never remove an outline without a visible replacement.
  Use existing dialog/menu primitives for focus entry, Escape and focus return.
- MUST preserve PAW's existing Enter / Shift+Enter chat behavior and IME
  composition guard. Upstream textarea shortcut examples do not change this contract.
- MUST preserve draft text, selection, focus and reading position across
  updates. Do not block paste or remount inputs/messages on streaming updates.
  Put validation near its field and make the first failing field discoverable.
- MUST preserve browser zoom: never add `user-scalable=no` or `maximum-scale=1`,
  or intercept browser zoom shortcuts. Use at least 16px input text on mobile
  when needed to avoid focus zoom, rather than disabling zoom.
- MUST retain PAW target sizes: dense desktop controls at least 32px, compact
  mobile controls at least 40px, and primary touch actions at least 44px.
  Expand the hit area when the visible icon is smaller.

## Truthful, stable feedback

- MUST keep a loading button's label, accessible name, dimensions and hit target
  stable. Reuse the existing indeterminate indicator; do not invent percentages.
- MUST derive pending/disabled state from the owning request, retaining existing
  validation and busy-chat delivery behavior. Prevent duplicate submission
  without disabling unrelated actions or available Stop/Cancel controls.
- MUST distinguish delivery, acknowledgement and execution completion. A timer,
  animation, HTTP ACK or hidden window is not evidence of success or cancellation.
  Consume existing request/event projections; do not add a second lifecycle.
- MUST give errors a useful recovery action and preserve unsent work. Keep
  retry/stop/continue semantics with their existing owner; do not auto-replay an
  uncertain request. Announce concise status changes with an appropriate live
  region without reading every streaming token aloud.

## Layout, motion and review

- MUST keep the conversation readable and the main action easy to find.
  Group related secondary controls, reuse spacing tokens, and avoid competing
  primary buttons. Reserve space for fixed headers/composers and pending content.
- MUST handle empty, long, dense, loading and failed content without clipped
  actions or page-level horizontal overflow. Keep body contrast at least 4.5:1.
  Prefer responsive flex/grid and existing list virtualization.
- MUST respect reduced-motion and surface-activity settings through the existing
  motion owner. Keep feedback static when motion is reduced; task state still
  advances. Prefer transform/opacity and explicit transition properties;
  never use `transition: all` or restart entry animation on each streamed update.
- For UI behavior changes, run the affected tests and applicable checks in
  CONTRIBUTING. Verify keyboard/focus, chat shortcuts/IME, pending/error/retry,
  narrow windows, 200% browser zoom and reduced motion. Use the existing product
  entry for browser checks; report mock, browser and native evidence separately.

## Source

Adapted from [Vercel Web Interface Guidelines AGENTS.md](https://github.com/vercel-labs/web-interface-guidelines/blob/e3d624baaf29dc1fc645aff3e38f03e564d2d6b1/AGENTS.md),
commit `e3d624baaf29dc1fc645aff3e38f03e564d2d6b1` (MIT).
See [the notice](../THIRD_PARTY_NOTICES.md#design-and-interaction-references)
and [retained license](../licenses/Vercel-Web-Interface-Guidelines-LICENSE).
