# Pi 0.87 compatibility in PAW

Baseline: published Pi **0.87.1**, protocol 2. Updating the executable alone is not a compatibility acceptance. Unreleased upstream changes are not part of this baseline.

## Ownership and support

| Pi capability | PAW integration / acceptance boundary |
| --- | --- |
| Updated model catalog, provider fixes, adaptive thinking and context limits | Pi owns discovery and inference. PAW uses the runtime model catalog and its supported thinking levels rather than a static model-name allowlist. Availability still depends on provider credentials and endpoint support. GPT-6 Sol/Luna routing has actual request and settled-model evidence in the Jev canary; other providers are not thereby live-tested. |
| Image-only prompts and model-specific image encoding | PAW passes attachments through the image-attachment runtime contract. Imported OpenAI-compatible gateway models now preserve `modalities.input`, `inputLimits.images.resize`, image-count/request-byte metadata, and `promptCache` lifetimes through a scalar allowlist. Native catalog models retain Pi metadata. Unit tests prove import behavior, not a live image inference call. |
| Canonical context, persisted context edits and system-inclusive extension transforms | Pi SDK remains the context/history owner; PAW does not rebuild Pi conversation history or add another compactor. Session control, debug context and settlement remain runtime projections. A PAW context-edit GUI is **not** implemented by this integration. Extension transform semantics need their own runtime tests, not just a PAW frontend fixture. |
| Prompt, steer, abort and canonical idle snapshot | The staged Session smoke checks these methods with deterministic inference. This does not count as a real provider test. |
| Fork/rewrite, multi-session, managed plugins, dynamic tools and settled events | Exposed only when the runtime capability contract declares them. Existing session/Room/package smoke checks remain separate from model inference tests. |
| New CLI/TUI behavior | Terminal-only flags, key bindings and terminal rendering are not PAW desktop UI features. PAW uses its own accessible conversation renderer and the runtime protocol. |

## Configuration import

The existing user-owned OpenCode-style provider file accepts these allowlisted additions under an imported OpenAI-compatible model:

```json
{
  "modalities": { "input": ["text", "image"] },
  "inputLimits": {
    "maxRequestBytes": 8000000,
    "images": {
      "maxPerMessage": 5,
      "maxPerRequest": 20,
      "resize": { "maxWidth": 1568, "maxHeight": 1568, "maxBytes": 524288, "jpegQuality": 75 }
    }
  },
  "promptCache": { "short": 300, "long": 3600 }
}
```

Only positive safe integers are forwarded; JPEG quality must not exceed 100. Unknown fields, arbitrary commands, headers and invalid values are not forwarded. Credentials continue to use child-process environment references. `maxBytes` measures encoded image payload. Pi does not rewrite historical images on a model change, and its documented count/request-byte metadata is **not** an enforcement guarantee. Cache lifetime metadata does not itself enable PAW background cache warming.

## Explicit limits

The installed runtime reports `sessionExactTurnCancel`, `sessionRetiredTurnRecovery`, and `sessionInterruptedTurnRecovery` as false. PAW must not advertise these as supported, infer them from ordinary abort support, or synthesize successful receipts.

Relevant owners: [runtime contract](session-runtime-host-contract.json), [provider translation](../../rag_ime/pi/provider_config.py), [provider tests](../../tests/test_pi_provider_config.py). Local smoke/canary reports are private acceptance artifacts, not a distribution-release declaration.
