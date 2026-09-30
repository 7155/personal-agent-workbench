# Sign in with ChatGPT

PAW's `openai-chatgpt` provider implements the OpenAI open-source, public-client
Sign in with ChatGPT (SIWC) flow. It is distinct from `openai` API-key billing and
the older `openai-codex` connection. There is no embedded partner key or client
secret and no assertion that PAW is an approved commercial partner.

## Local sign-in and account selection

1. Update the PAW frontend, backend, and matching managed Pi payload together.
2. Open Settings → Model accounts and select **ChatGPT plan (Sign in with ChatGPT)**.
3. Choose a saved account/workspace registration or **Add another account or workspace**.
   Click **Continue with ChatGPT** and complete OpenAI's consent in the local browser.
4. Wait for PAW to report the result. New credentials become active only after
   signature, issuer, audience, expiry, nonce and returning identity validation.
   Cancelling or failing a new attempt preserves the prior active account.
5. If identity is connected but plan use is disabled, explicitly enable plan use
   in settings, or choose a separate API-key provider. No automatic billing fallback occurs.
6. Refresh the entitled model list and choose a model under `openai-chatgpt`.
   Existing Session/Room model profiles are not silently changed. Restart the
   Agent runtime after an account switch so all model pickers use the new catalog.

Registrations with the same email remain separate. Their stable labels include
a short registration identifier. Sign-out attempts to revoke the selected
renewable session, clears its local tokens, and retains its account/client
mapping. If remote revocation cannot be confirmed, PAW says so and links to
[ChatGPT settings](https://chatgpt.com/settings/usage). Other saved registrations
and API-key providers remain independent.

Eligible requests consume the user's ChatGPT plan or permitted credits balance.
The zero API-dollar estimate for this subscription provider is not a promise of
free or unlimited use. Manage plan/app limits in ChatGPT settings. Model
availability, workspace policy and serving-region restrictions are enforced by
OpenAI. Plan-limit and policy errors are not retried as transient throttling.

## Self-hosted VM

Do not paste tokens into a chat, browser local storage or a URL. A browser on a
laptop cannot finish a callback bound to a VM's `127.0.0.1`. Complete sign-in in
the same PAW version locally, then transfer one protected registration over SSH.
The VM must retain its own stable host identity.

The following are local command-line operations, deliberately unavailable through
PAW's HTTP control API. Use the verified installed payload's `runtime-host/provider-bridge.mjs`
and its `bin/node`. Substitute your absolute paths. `agentDir` is that installation's
configured Pi Agent directory, not the source repository.

On the VM, create or reuse the VM host ID first:

```sh
printf '%s\n' '{"action":"chatgpt_prepare_host","agentDir":"/path/to/vm/Agent"}' \
  | /path/to/payload/bin/node /path/to/payload/runtime-host/provider-bridge.mjs
```

On the local computer after sign-in, export only the active registration to a
new file. Export refuses to overwrite an existing file and creates it mode `0600`:

```sh
printf '%s\n' '{"action":"chatgpt_export","agentDir":"/path/to/local/Agent","path":"/path/to/private/siwc-account.json"}' \
  | /path/to/payload/bin/node /path/to/payload/runtime-host/provider-bridge.mjs
```

Transfer that file with your normal authenticated SSH tools. Stop using the
exported session on the local host while the VM owns its rotating refresh token.
On the VM, ensure the transferred file is mode `0600`, then import:

```sh
printf '%s\n' '{"action":"chatgpt_import","agentDir":"/path/to/vm/Agent","path":"/path/to/private/siwc-account.json"}' \
  | /path/to/payload/bin/node /path/to/payload/runtime-host/provider-bridge.mjs
```

Import rejects legacy Codex credentials, symlinks, oversized files and files
readable by other users. It merges the selected registration, preserves other
saved accounts and never copies a laptop's host ID. Restart the runtime and
refresh models. Remove the temporary transfer copies after verifying the import.
The returned receipt contains no tokens. Transferred sessions do not currently
have host-specific usage attribution or revocation; disconnecting the renewable
session can affect its other copies.

## Implementation and verification boundary

- Pi owns dynamic registration, JWT validation (`jose`), refresh, model discovery
  and public Responses inference. Its low-level OAuth factory requires the host
  application to supply a truthful app name, stable host ID and selected registration;
  the PAW bridge supplies these. Raw Pi CLI sign-in is not the PAW account-management UI.
- PAW owns the account picker, validated active-account changes, protected transfer,
  cancellation, and user-facing receipts. Its host ID is stored separately as
  `Agent/chatgpt-host-id`; account records are under `openai-chatgpt` in `Agent/auth.json`.
- SIWC credential mutations and rotating refresh tokens use locked, atomic mode-`0600`
  writes. Do not commit or upload those files. Keep the host-ID file when signing out.
- The model picker obtains `/v1/models` with the selected account's token. A cache
  is bound to its issued client and subject. Bundled OpenAI model data supplies
  known capability metadata only, never entitlement. Unknown slugs use conservative
  text-only/local token-budget defaults until metadata is available.
- Requests use `POST https://api.openai.com/v1/responses`, `stream:true`, `store:false`,
  developer instructions and namespaced local tools. Unsupported preview fields
  are omitted; unsupported hosted tools fail explicitly. Only `response.completed`
  counts as success. Incomplete/interrupted/failed output is not a completed turn.
- Offline fixtures cover protocol, bad tokens, cancellation, identity-only consent,
  account isolation, atomic storage, errors, routing and UI state. They do not prove
  live account eligibility, real browser login or billable inference. Those require
  an attended user-authorized acceptance run.

## Official protocol references

Reviewed 2026-09-30:
[overview](https://developers.openai.com/siwc/token-sharing-open-source),
[registration](https://developers.openai.com/siwc/token-sharing-open-source/sign-in),
[accounts and revocation](https://developers.openai.com/siwc/token-sharing-open-source/profiles-and-sessions),
[models and inference](https://developers.openai.com/siwc/token-sharing-open-source/models-and-inference),
[tokens](https://developers.openai.com/siwc/token-sharing-open-source/token-reference),
[recovery](https://developers.openai.com/siwc/token-sharing-open-source/errors-and-recovery),
[preview limits](https://developers.openai.com/siwc/token-sharing-open-source/preview-limitations),
[VMs](https://developers.openai.com/siwc/token-sharing-open-source/self-hosted-vms),
[UX](https://developers.openai.com/siwc/ui-ux-guidelines).
