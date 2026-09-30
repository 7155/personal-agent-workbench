#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path, PurePosixPath

import yaml


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from rag_ime.agent_tool_ids import CONTROL_TOOL_IDS
from rag_ime.agent_extensions import (
    extension_app_binding_capability,
    extension_app_binding_capability_from_capabilities,
    extension_app_binding_sha256,
)
from rag_ime.managed_pi_runtime import (
    MANIFEST_NAME,
    ManagedPiRuntimeError,
    build_managed_pi_runtime_manifest,
    discover_managed_pi_runtime,
    write_managed_pi_runtime_manifest,
)


SESSION_RUNTIME_CONTRACT = (
    ROOT / "integrations" / "pi" / "session-runtime-host-contract.json"
)
SKILL_ROUTING_CARDS = ROOT / "integrations" / "pi" / "skill-routing-cards.json"
EXTENSION_APPS_ROOT = ROOT / "control-center-web" / "extension-apps"
_EXTENSION_APP_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_PI_PACKAGE_NAME = re.compile(
    r"^(?:@[a-z0-9][a-z0-9._~-]{0,63}/)?[a-z0-9][a-z0-9._~-]{0,63}$"
)
_PI_PACKAGE_VERSION = re.compile(
    r"^(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)
_PI_PACKAGE_RESOURCE_KEYS = ("extensions", "skills", "prompts", "themes")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_EXTENSION_PACKAGE_SEGMENT = re.compile(
    r"^(?:[a-z0-9][a-z0-9.-]*|SKILL\.md)$"
)
_MAX_NATIVE_PACKAGE_FILES = 1024
_MAX_NATIVE_PACKAGE_BYTES = 20 * 1024 * 1024
_EXTENSION_APP_PRESENTATIONS = frozenset(
    {"workspace", "conversation", "library", "studio", "utility"}
)
_EXTENSION_APP_ACCENTS = frozenset(
    {"cyan", "blue", "violet", "amber", "green", "rose", "slate"}
)
_EXTENSION_APP_ICON_SYMBOLS = frozenset(
    {"analytics", "assistant", "document", "commerce"}
)
_EXTENSION_APP_SANDBOX_DEFAULTS = frozenset({"required", "optional", "disabled"})
_EXTENSION_APP_SANDBOX_FIELDS = frozenset({"default", "connectorPackageId", "policyId"})
BUNDLED_SKILL_SUPPORT_DIRS: frozenset[str] = frozenset()
# The legacy @paw/pi-subagent package runs child Sessions inline and blocks the
# parent turn. Product Sessions use the native agents gateway instead, so this
# package must not be present in a newly built managed Runtime catalog.
DISABLED_BUNDLED_PI_PACKAGE_DIRS: frozenset[str] = frozenset({"subagent"})
PROJECT_ROUTING_SKILLS = frozenset(
    {
        "bootstrap-project-context",
        "memory-curation",
        "pawos-app-builder",
        "pawos-system",
        "plugin-creator",
        "project-maintainer",
    }
)
ROUTING_CARD_FIELDS = ("name", "when", "notFor", "does", "input", "output")
MAX_ROUTING_CARD_CHARS = 200
SKILL_SOURCE_KINDS = ("bundled", "configured", "pi-installed")
REQUIRED_PI_RUNTIME_BASE_COMMIT = "9c3f93c8b1c409e82e14d458510c146088c44561"
REQUIRED_RUNTIME_METHODS = (
    "hello",
    "health",
    "models.list",
    "completion.once",
    "completion.cancel",
    "tools.list",
    "tools.sync",
    "session.open",
    "session.control_state",
    "session.settlement.get",
    "session.await_settled",
    "session.snapshot",
    "session.debug.context",
    "session.commands",
    "session.command.invoke",
    "session.fork.candidates",
    "session.fork",
    "session.rewind",
    "session.prompt",
    "session.steer",
    "session.follow_up",
    "session.abort",
    "session.compact",
    "session.model.set",
    "session.thinking.set",
    "session.close",
    "room.dispatch",
    "room.cancel",
    "approval.resolve",
    "review.resolve",
    "ui.resolve",
    "plugins.catalog",
    "plugins.list",
    "plugins.create",
    "plugins.package.create",
    "plugins.package.prepare",
    "plugins.validate",
    "plugins.install.preview",
    "plugins.install",
    "plugins.enable",
    "plugins.disable",
    "plugins.uninstall",
    "plugins.rollback",
)
_SESSION_RUNTIME_SOURCE_KEYS = (
    "protocol",
    "runtimeHost",
    "contextInspection",
    "toolBridge",
    "toolResults",
    "session",
    "pluginManager",
    "packageCatalog",
    "packageManager",
)
# Product builds must keep the reviewed Pi worktree clean, while Skill routing
# is a PAW policy that must execute inside the Host loader. Apply this
# fail-closed overlay only to the copied build input. This script is included
# in the content-addressed Runtime version digest.
_CANDIDATE_SKILL_PATHS_SOURCE = r'''async function optionalCandidateSkillPaths(params: Record<string, unknown>, cwd: string): Promise<string[] | undefined> {
    const value = params.candidateSkillPaths;
    if (value === undefined) return undefined;
    if (!Array.isArray(value) || value.length < 1 || value.length > 8
        || params.piSkillsEnabled !== true || params.noContextFiles !== true
        || !Array.isArray(params.skillAllowlist) || params.skillAllowlist.length < 1) {
        throw new RuntimeProtocolError("INVALID_PARAMS", "Candidate Skills require an isolated explicit Session resource policy");
    }
    const root = await realpath(cwd);
    const paths: string[] = [];
    for (const item of value) {
        if (typeof item !== "string" || item.length > 4096 || !pathIsAbsolute(item)) {
            throw new RuntimeProtocolError("INVALID_PARAMS", "Candidate Skill path must be absolute");
        }
        const canonical = await realpath(item);
        const entry = await realpath(join(canonical, "SKILL.md"));
        if (canonical === root || !isInside(root, canonical) || !isInside(canonical, entry)
            || !(await stat(canonical)).isDirectory() || !(await stat(entry)).isFile()
            || paths.includes(canonical)) {
            throw new RuntimeProtocolError("INVALID_PARAMS", "Candidate Skill must stay inside its isolated Session workspace");
        }
        paths.push(canonical);
    }
    return paths;
}

'''


_RUNTIME_HOST_SOURCE_OVERLAYS: dict[
    str,
    tuple[tuple[str, str], ...],
] = {
    "src/pi-session.ts": (
        (
            'import { createHash, randomUUID } from "node:crypto";',
            'import { createHash, randomUUID } from "node:crypto";\n'
            'import { sessionResourceSettings, sessionLegacyExtensionPaths, type SessionResourceDisclosurePolicy } from "./session-resource-policy.ts";',
        ),
        (
            '\t\tconst legacyExtensionPaths = await managedLegacyExtensionPaths(options.activePluginDir);',
            '\t\tconst legacyExtensionPaths = sessionLegacyExtensionPaths(await managedLegacyExtensionPaths(options.activePluginDir), options.resourceDisclosurePolicy?.disabledPluginIds ?? []);',
        ),
        (
            '\t\t\tagentDir: options.agentDir,\n\t\t\tsettingsManager,',
            '\t\t\tagentDir: options.agentDir,\n\t\t\tsettingsManager: sessionResourceSettings(settingsManager, options.disabledPluginSources ?? []),',
        ),
        (
            "\tactivePluginDir: string;\n\tskillPaths: string[];",
            "\tactivePluginDir: string;\n"
            "\tskillPaths: string[];\n"
            "\tskillAllowlist?: string[];\n"
            "\tresourceDisclosurePolicy?: SessionResourceDisclosurePolicy;\n"
            "\tdisabledPluginSources?: string[];\n"
            "\tcandidateSkillPaths?: string[];\n"
            "\tcompactionInstructions?: string;",
        ),
        (
            "\t\tconst selectedSkillPaths = [",
            "\t\t// Only authenticated Session resource policy can replace these roots.\n"
            "\t\tconst selectedSkillPaths = options.candidateSkillPaths ?? [",
        ),
        (
            "\t\tconst settingsManager = SettingsManager.create(options.cwd, options.agentDir, { projectTrusted: true });",
            "\t\tconst settingsManager = SettingsManager.create(options.cwd, options.agentDir, { projectTrusted: true });\n"
            "\t\tif (options.compactionInstructions !== undefined) {\n"
            "\t\t\tconst compaction = { ...settingsManager.getCompactionSettings(), instructions: options.compactionInstructions };\n"
            "\t\t\tsettingsManager.applyOverrides({ compaction });\n"
            "\t\t}",
        ),
        (
            "\t\tconst skillPromptFocus = "
            "roomSkillPromptFocus(options.roomSkillPolicy) ?? [];\n"
            "\t\tconst toolPromptFocus = "
            "roomToolPromptFocus(options.roomSkillPolicy) ?? [];",
            "\t\tconst skillPromptFocus = "
            "roomSkillPromptFocus(options.roomSkillPolicy) ?? [];\n"
            "\t\tconst allowedSkillNames = options.skillAllowlist === undefined\n"
            "\t\t\t? undefined\n"
            "\t\t\t: new Set(options.skillAllowlist);\n"
            "\t\tconst toolPromptFocus = "
            "roomToolPromptFocus(options.roomSkillPolicy) ?? [];",
        ),
        (
            "\t\t\t\t\t\tskills: base.skills.filter(\n"
            "\t\t\t\t\t\t\t(skill) => skill.sourceInfo.scope === "
            '"temporary" || skill.sourceInfo.origin === "package",\n'
            "\t\t\t\t\t\t),",
            "\t\t\t\t\t\tskills: base.skills\n"
            "\t\t\t\t\t\t\t.filter(\n"
            "\t\t\t\t\t\t\t\t(skill) => skill.sourceInfo.scope === "
            '"temporary" || skill.sourceInfo.origin === "package",\n'
            "\t\t\t\t\t\t\t)\n"
            "\t\t\t\t\t\t\t.filter(\n"
            "\t\t\t\t\t\t\t\t(skill) => !options.resourceDisclosurePolicy?.disabledSkillNames.includes(skill.name)\n"
            "\t\t\t\t\t\t\t\t\t&& (allowedSkillNames === undefined\n"
            "\t\t\t\t\t\t\t\t\t|| allowedSkillNames.has(skill.name)),\n"
            "\t\t\t\t\t\t\t),",
        ),
    ),
    "src/runtime-host.ts": (
        (
            'case "session.open": {\n\t\t\t\tconst sessionId = requiredSessionId(params);',
            'case "session.open": {\n\t\t\t\tconst resourceDisclosurePolicy = sessionResourceDisclosurePolicy(params.resourceDisclosurePolicy);\n'
            '\t\t\t\tconst disabledPlugins = new Set(resourceDisclosurePolicy.disabledPluginIds);\n'
            '\t\t\t\tconst sessionPackages = disabledPlugins.size ? await this.nativePackages.list() : [];\n'
            '\t\t\t\tconst disabledPluginSources = sessionPackages.filter((item) => disabledPlugins.has(item.id)).map((item) => item.source);\n'
            '\t\t\t\tconst sessionId = requiredSessionId(params);',
        ),
        (
            'import { existsSync, readdirSync } from "node:fs";',
            'import { sessionResourceDisclosurePolicy } from "./session-resource-policy.ts";\nimport { existsSync, readdirSync } from "node:fs";',
        ),
        (
            '\t\t\t\t\t\tisPackageCapabilityEnabled: (capability) => this.nativePackages.hasEnabledCapability(capability),\n'
            '\t\t\t\t\t\tnoContextFiles: optionalBoolean(params, "noContextFiles"),',
            '\t\t\t\t\t\tisPackageCapabilityEnabled: (capability) => disabledPlugins.size\n'
            '\t\t\t\t\t\t\t? sessionPackages.some((item) => item.enabled && !disabledPlugins.has(item.id) && item.capabilities.includes(capability))\n'
            '\t\t\t\t\t\t\t: this.nativePackages.hasEnabledCapability(capability),\n'
            '\t\t\t\t\t\tnoContextFiles: optionalBoolean(params, "noContextFiles"),',
        ),
        (
            "\tif (typeof value !== \"boolean\") {\n"
            "\t\tthrow new RuntimeProtocolError("
            '"INVALID_PARAMS", `${key} must be a boolean`);\n'
            "\t}\n"
            "\treturn value;\n"
            "}\n\n"
            "function requiredBoolean",
            "\tif (typeof value !== \"boolean\") {\n"
            "\t\tthrow new RuntimeProtocolError("
            '"INVALID_PARAMS", `${key} must be a boolean`);\n'
            "\t}\n"
            "\treturn value;\n"
            "}\n\n"
            "function optionalCompactionInstructions(params: Record<string, unknown>): string | undefined {\n"
            "\tconst value = params.compactionInstructions;\n"
            "\tif (value === undefined) return undefined;\n"
            "\tif (typeof value !== \"string\" || [...value].length > 8000 || /[\\x00-\\x08\\x0b\\x0c\\x0e-\\x1f]/u.test(value)) {\n"
            "\t\tthrow new RuntimeProtocolError(\"INVALID_PARAMS\", \"compactionInstructions must be bounded text\");\n"
            "\t}\n\treturn value;\n}\n\n"
            "function optionalSkillAllowlist("
            "params: Record<string, unknown>): string[] | undefined {\n"
            "\tconst value = params.skillAllowlist;\n"
            "\tif (value === undefined || value === null) return undefined;\n"
            "\tif (!Array.isArray(value) || value.length > 128) {\n"
            "\t\tthrow new RuntimeProtocolError("
            '"INVALID_PARAMS", "skillAllowlist must be an array of at most 128 Skill names");\n'
            "\t}\n"
            "\tconst names: string[] = [];\n"
            "\tconst seen = new Set<string>();\n"
            "\tfor (const item of value) {\n"
            "\t\tif (typeof item !== \"string\") {\n"
            "\t\t\tthrow new RuntimeProtocolError("
            '"INVALID_PARAMS", "skillAllowlist must contain only Skill names");\n'
            "\t\t}\n"
            "\t\tconst name = item.trim();\n"
            "\t\tif (!/^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/.test(name) "
            "|| seen.has(name)) {\n"
            "\t\t\tthrow new RuntimeProtocolError("
            '"INVALID_PARAMS", "skillAllowlist contains an invalid or duplicate Skill name");\n'
            "\t\t}\n"
            "\t\tseen.add(name);\n"
            "\t\tnames.push(name);\n"
            "\t}\n"
            "\treturn names;\n"
            "}\n\n"
            + _CANDIDATE_SKILL_PATHS_SOURCE
            + "function requiredBoolean",
        ),
        (
            "\t\t\t\t\t\tsessionControlState: true,\n"
            "\t\t\t\t\t\tsessionSnapshot: true,",
            "\t\t\t\t\t\tsessionControlState: true,\n"
            "\t\t\t\t\t\tsessionSkillAllowlist: true,\n"
            "\t\t\t\t\t\tsessionResourceDisclosure: true,\n"
            "\t\t\t\t\t\tsessionCandidateSkillPaths: true,\n"
            "\t\t\t\t\t\tsessionPromptSettings: true,\n"
            "\t\t\t\t\t\tsessionSnapshot: true,",
        ),
        (
            "\t\t\t\t\t\tcodexSkillsEnabled: optionalBoolean("
            'params, "codexSkillsEnabled"),\n'
            "\t\t\t\t\t\tmodelRuntime: this.modelRuntime,",
            "\t\t\t\t\t\tcodexSkillsEnabled: optionalBoolean("
            'params, "codexSkillsEnabled"),\n'
            "\t\t\t\t\t\tskillAllowlist: optionalSkillAllowlist(params),\n"
            "\t\t\t\t\t\tresourceDisclosurePolicy,\n"
            "\t\t\t\t\t\tdisabledPluginSources,\n"
            "\t\t\t\t\t\tcandidateSkillPaths: await optionalCandidateSkillPaths(params, cwd),\n"
            "\t\t\t\t\t\tcompactionInstructions: optionalCompactionInstructions(params),\n"
            "\t\t\t\t\t\tmodelRuntime: this.modelRuntime,",
        ),
    ),
    "src/tool-bridge.ts": (
        (
            "const ROOM_DELEGATION_GATEWAY_REQUEST_TIMEOUT_MS = 300_000;",
            "const DELEGATION_GATEWAY_REQUEST_TIMEOUT_MS = 300_000;",
        ),
        (
            'if (options.gatewayTimeoutMs !== undefined || toolName !== "room_partner") return options;',
            "if (\n"
            "\t\toptions.gatewayTimeoutMs !== undefined\n"
            "\t\t|| (toolName !== \"room_partner\" && toolName !== \"agents\")\n"
            ") return options;",
        ),
        (
            "gatewayTimeoutMs: ROOM_DELEGATION_GATEWAY_REQUEST_TIMEOUT_MS,",
            "gatewayTimeoutMs: DELEGATION_GATEWAY_REQUEST_TIMEOUT_MS,",
        ),
    ),
}
_OAUTH_RUNTIME_MODULES = {
    "openai-chatgpt.ts": (
        "packages/ai/src/auth/oauth/openai-chatgpt.ts",
        "openaiChatGPTOAuth",
    ),
    "anthropic.ts": (
        "packages/ai/src/auth/oauth/anthropic.ts",
        "anthropicOAuth",
    ),
    "github-copilot.ts": (
        "packages/ai/src/auth/oauth/github-copilot.ts",
        "githubCopilotOAuth",
    ),
    "openai-codex.ts": (
        "packages/ai/src/auth/oauth/openai-codex.ts",
        "openaiCodexOAuth",
    ),
    "openrouter.ts": ("packages/ai/src/auth/oauth/openrouter.ts", "openRouterOAuth"),
    "kimi-coding.ts": ("packages/ai/src/auth/oauth/kimi-coding.ts", "kimiCodingOAuth"),
    "xai.ts": ("packages/ai/src/auth/oauth/xai.ts", "xaiOAuth"),
    "radius.ts": (
        "packages/ai/src/auth/oauth/radius.ts",
        "createRadiusOAuth",
    ),
}


# Patch the shared native summary entry, not its trigger, checkpoint, retry or
# cancellation loop. The exact pinned SDK text must match before we build.
_SDK_PROMPT_OVERLAYS: dict[str, tuple[tuple[str, str], ...]] = {
    "dist/core/settings-manager.js": ((
        "    getCompactionSettings() {\n        return {\n            enabled: this.getCompactionEnabled(),\n            reserveTokens: this.getCompactionReserveTokens(),\n            keepRecentTokens: this.getCompactionKeepRecentTokens(),\n        };\n    }",
        "    getCompactionSettings() {\n        return {\n            enabled: this.getCompactionEnabled(),\n            reserveTokens: this.getCompactionReserveTokens(),\n            keepRecentTokens: this.getCompactionKeepRecentTokens(),\n            instructions: this.settings.compaction?.instructions,\n        };\n    }",
    ),),
    "dist/core/agent-session.js": ((
        "    async _runDefaultCompaction(preparation, requestModel, apiKey, headers, customInstructions, signal, env, reason) {\n        return compact(preparation, requestModel, apiKey, headers, customInstructions, signal, this.thinkingLevel, this.agent.streamFunction, env, this.settingsManager.getRetrySettings(), this._summarizationRetryCallbacks({ source: \"compaction\", reason }), undefined);\n    }",
        "    async _runDefaultCompaction(preparation, requestModel, apiKey, headers, customInstructions, signal, env, reason) {\n        const instructions = [this.settingsManager.getCompactionSettings().instructions, customInstructions].filter(value => typeof value === \"string\" && value.trim()).join(\"\\n\\n\") || undefined;\n        return compact(preparation, requestModel, apiKey, headers, instructions, signal, this.thinkingLevel, this.agent.streamFunction, env, this.settingsManager.getRetrySettings(), this._summarizationRetryCallbacks({ source: \"compaction\", reason }), undefined);\n    }",
    ),),
    "dist/core/compaction/compaction.js": (
        (
            "            sourceContext: cacheFriendly?.turnPrefixSourceContext,\n            requestOptions: cacheFriendly?.requestOptions,\n        });",
            "            sourceContext: cacheFriendly?.turnPrefixSourceContext,\n            requestOptions: cacheFriendly?.requestOptions,\n        }, customInstructions);",
        ),
        (
            "async function generateTurnPrefixSummary(messages, model, reserveTokens, apiKey, headers, env, signal, thinkingLevel, streamFn, retry, callbacks, cacheFriendly) {",
            "async function generateTurnPrefixSummary(messages, model, reserveTokens, apiKey, headers, env, signal, thinkingLevel, streamFn, retry, callbacks, cacheFriendly, customInstructions) {\n    const focus = customInstructions ? `\\n\\nAdditional focus: ${customInstructions}` : \"\";",
        ),
        (
            "buildSummarizationContext(SOURCE_CONTEXT_TURN_PREFIX_SUMMARIZATION_PROMPT, sourceContext)",
            "buildSummarizationContext(SOURCE_CONTEXT_TURN_PREFIX_SUMMARIZATION_PROMPT + focus, sourceContext)",
        ),
        (
            "promptText = SOURCE_CONTEXT_TURN_PREFIX_SUMMARIZATION_PROMPT;",
            "promptText = SOURCE_CONTEXT_TURN_PREFIX_SUMMARIZATION_PROMPT + focus;",
        ),
        (
            "${TURN_PREFIX_SUMMARIZATION_PROMPT}`;",
            "${TURN_PREFIX_SUMMARIZATION_PROMPT}${focus}`;",
        ),
    ),
}


_SDK_PROMPT_OVERLAYS_V087 = {
    **_SDK_PROMPT_OVERLAYS,
    "dist/core/settings-manager.js": tuple(
        tuple(text.replace("getCompactionSettings()", "getCompactionSettings(model)")
              .replace("getCompactionReserveTokens()", "getCompactionReserveTokens(model)")
              .replace("getCompactionKeepRecentTokens()", "getCompactionKeepRecentTokens(model)")
              for text in replacement)
        for replacement in _SDK_PROMPT_OVERLAYS["dist/core/settings-manager.js"]
    ),
    "dist/core/compaction/compaction.js": (
        (
            "await generateTurnPrefixSummary(turnPrefixMessages, model, settings.reserveTokens, apiKey, headers, env, signal, thinkingLevel, streamFn, retry, callbacks, sessionId)",
            "await generateTurnPrefixSummary(turnPrefixMessages, model, settings.reserveTokens, apiKey, headers, env, signal, thinkingLevel, streamFn, retry, callbacks, sessionId, customInstructions)",
        ),
        (
            "async function generateTurnPrefixSummary(messages, model, reserveTokens, apiKey, headers, env, signal, thinkingLevel, streamFn, retry, callbacks, sessionId) {",
            "async function generateTurnPrefixSummary(messages, model, reserveTokens, apiKey, headers, env, signal, thinkingLevel, streamFn, retry, callbacks, sessionId, customInstructions) {\n    const focus = customInstructions ? `\\n\\nAdditional focus: ${customInstructions}` : \"\";",
        ),
        ("${TURN_PREFIX_SUMMARIZATION_PROMPT}`;", "${TURN_PREFIX_SUMMARIZATION_PROMPT}${focus}`;"),
    ),
}


def _prepare_sdk_prompt_overlay(pi_root: Path, destination: Path) -> Path:
    """Copy built SDK resources and keep the pinned Pi checkout untouched."""
    source = pi_root / "packages" / "coding-agent"
    destination.mkdir(parents=True)
    shutil.copy2(source / "package.json", destination / "package.json")
    shutil.copytree(source / "dist", destination / "dist", symlinks=True)
    (destination / "node_modules").symlink_to(pi_root / "node_modules", target_is_directory=True)
    settings_source = (source / "dist/core/settings-manager.js").read_text(encoding="utf-8")
    overlays = _SDK_PROMPT_OVERLAYS_V087 if "getCompactionSettings(model)" in settings_source else _SDK_PROMPT_OVERLAYS
    for relative_path, replacements in overlays.items():
        path = destination / relative_path
        content = path.read_text(encoding="utf-8")
        for before, after in replacements:
            if content.count(before) != 1:
                raise ManagedPiRuntimeError(f"Pi SDK prompt overlay does not match pinned source: {relative_path}")
            content = content.replace(before, after, 1)
        path.write_text(content, encoding="utf-8")
    return destination


def _run(command: list[str], *, cwd: Path) -> str:
    completed = subprocess.run(
        command,
        cwd=cwd,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return completed.stdout.strip()


def _default_node() -> str:
    configured = os.environ.get("RAG_IME_MANAGED_NODE", "").strip()
    if configured:
        return configured
    try:
        installed = discover_managed_pi_runtime(
            Path.home() / "Library" / "Application Support" / "RagIme"
        )
        installed_node = Path(installed.node_executable)
        if installed_node.is_file():
            return str(installed_node)
    except (OSError, ManagedPiRuntimeError):
        pass
    codex_runtime = (
        Path.home()
        / ".cache"
        / "codex-runtimes"
        / "codex-primary-runtime"
        / "dependencies"
        / "node"
        / "bin"
        / "node"
    )
    if codex_runtime.is_file():
        return str(codex_runtime)
    # Do not bypass discover_managed_pi_runtime with a raw pointer read: a
    # digest-invalid generation must never supply the Node used to build its
    # replacement, and every authoritative pointer read shares the lifecycle
    # lock.
    return shutil.which("node") or ""


def _runtime_host_root(pi_root: Path) -> Path:
    # The Runtime Host was moved out of the old Pi package tree. Keep one
    # source of truth so a stale checkout can never be selected implicitly.
    return pi_root / "integrations" / "rag-ime-runtime-host"


def _prepare_runtime_host_overlay(
    package_root: Path,
    destination: Path,
    *,
    pi_root: Path,
) -> Path:
    """Copy the pinned Runtime Host and apply product-owned guarded patches."""

    if destination.exists() or destination.is_symlink():
        raise ManagedPiRuntimeError(
            f"Runtime Host overlay destination already exists: {destination}"
        )
    shutil.copytree(package_root, destination, symlinks=True)
    node_modules = destination / "node_modules"
    if node_modules.exists() or node_modules.is_symlink():
        raise ManagedPiRuntimeError(
            "Runtime Host source unexpectedly contains node_modules"
        )
    source_node_modules = pi_root / "node_modules"
    if not source_node_modules.is_dir():
        raise ManagedPiRuntimeError(
            f"Pi worktree dependencies are unavailable: {source_node_modules}"
        )
    node_modules.symlink_to(source_node_modules, target_is_directory=True)

    shutil.copy2(
        ROOT / "scripts" / "runtime-host-session-resource-policy.ts",
        destination / "src" / "session-resource-policy.ts",
    )

    for relative_path, replacements in _RUNTIME_HOST_SOURCE_OVERLAYS.items():
        source_path = destination / relative_path
        if not source_path.is_file():
            raise ManagedPiRuntimeError(
                f"Runtime Host overlay source is missing: {relative_path}"
            )
        source = source_path.read_text(encoding="utf-8")
        for before, after in replacements:
            matches = source.count(before)
            if matches != 1:
                raise ManagedPiRuntimeError(
                    "Runtime Host overlay anchor mismatch for "
                    f"{relative_path}: expected 1 match, found {matches}"
                )
            source = source.replace(before, after, 1)
        source_path.write_text(source, encoding="utf-8")
    return destination


def _pi_worktree_error(pi_root: Path) -> str:
    canonical = _runtime_host_root(pi_root)
    if canonical.is_symlink():
        return (
            "canonical Pi Runtime Host source must be a real directory: "
            f"{canonical}"
        )
    legacy = pi_root / "packages" / "rag-ime-runtime-host"
    if legacy.is_dir() or legacy.is_symlink():
        return (
            "unsupported legacy Pi Runtime Host source at "
            f"{legacy}; use a canonical Pi worktree containing "
            "integrations/rag-ime-runtime-host"
        )
    if canonical.is_dir():
        return ""
    return (
        "Pi worktree is incomplete; expected canonical source at "
        f"{canonical}"
    )


def _node_relocation_error(node: Path) -> str:
    if sys.platform != "darwin":
        return ""
    try:
        linked = subprocess.run(
            ["otool", "-L", str(node)],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return "could not inspect Node dynamic-library dependencies"
    if "@rpath/libnode." in linked:
        return "Node depends on an external libnode dylib and is not relocatable"
    return ""


def _dirty_pi_worktree_sha256(pi_root: Path) -> str:
    try:
        tracked = subprocess.run(
            ["git", "diff", "--binary", "--no-ext-diff", "HEAD", "--"],
            cwd=pi_root,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        ).stdout
        untracked = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard", "-z"],
            cwd=pi_root,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        ).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        raise ManagedPiRuntimeError(
            f"Pi dirty source identity is unavailable: {exc}"
        ) from exc

    digest = hashlib.sha256()
    digest.update(b"tracked\0")
    digest.update(tracked)
    for encoded_path in sorted(item for item in untracked.split(b"\0") if item):
        path = pi_root / os.fsdecode(encoded_path)
        digest.update(b"\0untracked\0")
        digest.update(encoded_path)
        digest.update(b"\0")
        try:
            if path.is_symlink():
                digest.update(b"symlink\0")
                digest.update(os.fsencode(os.readlink(path)))
            elif path.is_file():
                digest.update(b"file\0")
                with path.open("rb") as handle:
                    while chunk := handle.read(1024 * 1024):
                        digest.update(chunk)
            else:
                raise ManagedPiRuntimeError(
                    f"Pi dirty source contains an unsupported untracked path: {path}"
                )
        except OSError as exc:
            raise ManagedPiRuntimeError(
                f"Pi dirty source identity is unavailable for {path}: {exc}"
            ) from exc
    return digest.hexdigest()


def _verify_pi_worktree(pi_root: Path) -> str:
    """Verify the explicit Pi source checkout before any build work begins.

    Formal artifacts require a clean detached worktree descended from the
    reviewed Runtime Host baseline. Explicit development installs may build a
    dirty detached worktree, but the returned revision includes a content
    digest so it cannot masquerade as the clean commit.
    """

    pi_root = pi_root.expanduser().resolve()
    worktree_error = _pi_worktree_error(pi_root)
    if worktree_error:
        raise ManagedPiRuntimeError(worktree_error)
    try:
        repository_root = Path(
            _run(["git", "rev-parse", "--show-toplevel"], cwd=pi_root)
        ).resolve(strict=True)
        commit = _run(["git", "rev-parse", "HEAD"], cwd=pi_root)
        status = _run(
            ["git", "status", "--porcelain=v1", "--untracked-files=all"],
            cwd=pi_root,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ManagedPiRuntimeError(
            f"Pi worktree Git provenance is unavailable: {exc}"
        ) from exc
    if repository_root != pi_root:
        raise ManagedPiRuntimeError(
            "Pi worktree path is not the Git worktree root: "
            f"{pi_root} != {repository_root}"
        )
    try:
        branch = subprocess.run(
            ["git", "symbolic-ref", "--quiet", "--short", "HEAD"],
            cwd=pi_root,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except OSError as exc:
        raise ManagedPiRuntimeError(
            f"Pi worktree Git provenance is unavailable: {exc}"
        ) from exc
    if branch.returncode == 0:
        raise ManagedPiRuntimeError(
            "Pi worktree must use a detached HEAD; found branch "
            f"{branch.stdout.strip() or '<unknown>'}"
        )
    if branch.returncode != 1:
        detail = branch.stderr.strip() or f"git symbolic-ref exited {branch.returncode}"
        raise ManagedPiRuntimeError(
            f"Pi worktree detached-HEAD verification failed: {detail}"
        )
    dirty = bool(status.strip())
    if dirty and os.environ.get("RAG_IME_ALLOW_DIRTY_INSTALL") != "1":
        raise ManagedPiRuntimeError(
            "Pi worktree must be clean; uncommitted source changes were found"
        )
    try:
        subprocess.run(
            [
                "git",
                "merge-base",
                "--is-ancestor",
                REQUIRED_PI_RUNTIME_BASE_COMMIT,
                "HEAD",
            ],
            cwd=pi_root,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ManagedPiRuntimeError(
            "Pi worktree does not contain the required reviewed Runtime Host "
            f"ancestry {REQUIRED_PI_RUNTIME_BASE_COMMIT}: {exc}"
        ) from exc
    if dirty:
        return f"{commit}+dirty.{_dirty_pi_worktree_sha256(pi_root)}"
    return commit


def _source_revision(pi_root: Path) -> str:
    """Return an ancestry-verified clean or explicit development revision."""

    return _verify_pi_worktree(pi_root)


def _verified_session_runtime_contract(pi_root: Path) -> tuple[dict[str, object], str]:
    try:
        contract_bytes = SESSION_RUNTIME_CONTRACT.read_bytes()
        contract = json.loads(contract_bytes)
    except (OSError, json.JSONDecodeError) as exc:
        raise ManagedPiRuntimeError(
            f"Session runtime source contract is unreadable: {exc}"
        ) from exc
    if not isinstance(contract, dict) or contract.get("schemaVersion") != (
        "rag-ime.pi-session-runtime-host-contract.v1"
    ):
        raise ManagedPiRuntimeError(
            "Session runtime source contract schema is unsupported"
        )
    if (
        contract.get("sourceRepository") != "https://github.com/7155/pi.git"
        or contract.get("sourcePackage") != "@earendil-works/pi-rag-ime-runtime-host"
        or contract.get("protocolVersion") != "2"
    ):
        raise ManagedPiRuntimeError(
            "Session runtime source provenance is unsupported"
        )
    methods = contract.get("requiredMethods")
    if methods != list(REQUIRED_RUNTIME_METHODS):
        raise ManagedPiRuntimeError(
            "Session runtime source contract methods are incomplete"
        )
    minimum_commit = str(contract.get("minimumHandlersCommit") or "").strip()
    if len(minimum_commit) != 40 or any(
        character not in "0123456789abcdef" for character in minimum_commit
    ):
        raise ManagedPiRuntimeError(
            "Session runtime minimum handlers commit is invalid"
        )
    if minimum_commit != REQUIRED_PI_RUNTIME_BASE_COMMIT:
        raise ManagedPiRuntimeError(
            "Session runtime minimum handlers commit is not the reviewed baseline"
        )
    # The product Pi fork can be rebased onto a newer upstream history. The
    # baseline commit identifies the reviewed capability contract; current
    # source is proven below by the complete handler map and exact markers,
    # while the payload records its real HEAD plus dirty-tree digest.
    sources = contract.get("handlerSources")
    if not isinstance(sources, dict) or set(sources) != set(
        _SESSION_RUNTIME_SOURCE_KEYS
    ):
        raise ManagedPiRuntimeError(
            "Session runtime handler source map is missing"
        )
    source_texts: dict[str, str] = {}
    for key in _SESSION_RUNTIME_SOURCE_KEYS:
        relative = Path(str(sources.get(key) or ""))
        if not relative.parts or relative.is_absolute() or ".." in relative.parts:
            raise ManagedPiRuntimeError(
                "Session runtime handler source path is unsafe"
            )
        if len(relative.parts) < 3 or relative.parts[:2] != (
            "integrations",
            "rag-ime-runtime-host",
        ):
            raise ManagedPiRuntimeError(
                "Session runtime handler source path must be under "
                "integrations/rag-ime-runtime-host: "
                f"{relative.as_posix()}"
            )
        resolved = (pi_root / relative).resolve()
        if not resolved.is_relative_to(pi_root.resolve()):
            raise ManagedPiRuntimeError(
                "Session runtime handler source escaped the Pi worktree"
            )
        try:
            source_texts[key] = resolved.read_text(encoding="utf-8")
        except OSError as exc:
            raise ManagedPiRuntimeError(
                f"Session runtime handler source is missing: {exc}"
            ) from exc
    required_markers = contract.get("requiredSourceMarkers")
    if not isinstance(required_markers, dict) or set(required_markers) != set(
        _SESSION_RUNTIME_SOURCE_KEYS
    ):
        raise ManagedPiRuntimeError(
            "Session runtime source marker map is incomplete"
        )
    for key in _SESSION_RUNTIME_SOURCE_KEYS:
        markers = required_markers.get(key)
        if not isinstance(markers, list) or not markers:
            raise ManagedPiRuntimeError(
                f"Session runtime source markers are missing: {key}"
            )
        for marker in markers:
            if not isinstance(marker, str) or not marker or marker not in source_texts[key]:
                raise ManagedPiRuntimeError(
                    f"Pi Runtime Host source marker is missing: {key}:{marker}"
                )
    protocol_source = source_texts["protocol"]
    runtime_host_source = source_texts["runtimeHost"]
    runtime_method_declaration = re.search(
        r"export\s+type\s+RuntimeMethod\s*=\s*(.*?);",
        protocol_source,
        flags=re.DOTALL,
    )
    if runtime_method_declaration is None:
        raise ManagedPiRuntimeError(
            "Pi Runtime Host protocol method declaration is missing"
        )
    declared_methods = tuple(
        re.findall(
            r'\|\s*"([a-z][a-z0-9_.]{0,63})"',
            runtime_method_declaration.group(1),
        )
    )
    if declared_methods != tuple(methods):
        raise ManagedPiRuntimeError(
            "Session runtime source contract methods do not match the Pi protocol"
        )
    for method in methods:
        if f'| "{method}"' not in protocol_source or f'case "{method}"' not in runtime_host_source:
            raise ManagedPiRuntimeError(f"Pi Runtime Host does not implement {method}")
    return contract, hashlib.sha256(contract_bytes).hexdigest()


def _hash_tree(path: Path) -> bytes:
    digest = hashlib.sha256()
    if not path.is_dir():
        return digest.digest()
    for item in sorted(candidate for candidate in path.rglob("*") if candidate.is_file()):
        digest.update(item.relative_to(path).as_posix().encode("utf-8"))
        digest.update(item.read_bytes())
    return digest.digest()


_BUNDLED_OVERLAY_SOURCE_ROOT = b"/rag-ime-managed/runtime-host"


def _normalize_bundled_overlay_paths(
    bundle: Path,
    overlay_package_root: Path,
) -> int:
    """Remove ephemeral overlay paths that make identical Host builds differ."""

    payload = bundle.read_bytes()
    replacements = 0
    source_roots = {
        str(overlay_package_root).encode("utf-8"),
        str(overlay_package_root.resolve()).encode("utf-8"),
    }
    for source_root in source_roots:
        occurrences = payload.count(source_root)
        if not occurrences:
            continue
        payload = payload.replace(
            source_root,
            _BUNDLED_OVERLAY_SOURCE_ROOT,
        )
        replacements += occurrences
    bundle.write_bytes(payload)
    return replacements


def _product_skill_dirs(
    skills_root: Path,
    *,
    allowed_support_dirs: frozenset[str] = BUNDLED_SKILL_SUPPORT_DIRS,
) -> tuple[Path, ...]:
    if not skills_root.is_dir():
        return ()
    skills: list[Path] = []
    for item in sorted(skills_root.iterdir(), key=lambda candidate: candidate.name):
        if not item.is_dir():
            continue
        if (item / "SKILL.md").is_file():
            skills.append(item)
            continue
        if item.name in allowed_support_dirs:
            continue
        raise ManagedPiRuntimeError(
            "bundled Skill discovery failed: "
            f"direct child directory {item} is missing SKILL.md"
        )
    return tuple(skills)


def _skill_frontmatter(skill_file: Path, *, source: str) -> dict[str, object]:
    try:
        content = skill_file.read_text(encoding="utf-8")
        parts = content.split("---", 2)
        if len(parts) != 3 or parts[0].strip():
            raise ValueError("missing leading YAML frontmatter")
        value = yaml.safe_load(parts[1])
    except (OSError, UnicodeError, yaml.YAMLError, ValueError) as error:
        raise ManagedPiRuntimeError(
            f"{source} Skill frontmatter is invalid at {skill_file}: {error}"
        ) from error
    if not isinstance(value, dict):
        raise ManagedPiRuntimeError(
            f"{source} Skill frontmatter must be an object at {skill_file}"
        )
    name = value.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ManagedPiRuntimeError(
            f"{source} Skill frontmatter has no non-empty name at {skill_file}"
        )
    if name.strip() != skill_file.parent.name:
        raise ManagedPiRuntimeError(
            f"{source} Skill name {name!r} does not match directory "
            f"{skill_file.parent.name!r} at {skill_file}"
        )
    return value


def _skill_routing_projection(
    frontmatter: dict[str, object],
    *,
    skill_name: str,
) -> dict[str, object]:
    metadata = frontmatter.get("metadata")
    nested = metadata.get("routing") if isinstance(metadata, dict) else None
    routing = nested if isinstance(nested, dict) else frontmatter
    projection: dict[str, object] = {"name": frontmatter.get("name")}
    for field in ROUTING_CARD_FIELDS[1:]:
        if field not in routing:
            raise ManagedPiRuntimeError(
                f"bundled Skill {skill_name!r} is missing routing field {field!r}"
            )
        projection[field] = routing[field]
    return projection


def _discover_skill_files(paths: tuple[Path, ...], *, source: str) -> tuple[Path, ...]:
    discovered: dict[Path, Path] = {}
    for configured_path in paths:
        path = configured_path.expanduser()
        if not path.exists():
            continue
        candidates: tuple[Path, ...]
        if path.is_file():
            candidates = (path,) if path.name == "SKILL.md" else ()
        elif (path / "SKILL.md").is_file():
            candidates = (path / "SKILL.md",)
        else:
            candidates = tuple(sorted(path.rglob("SKILL.md")))
        for candidate in candidates:
            try:
                canonical = candidate.resolve(strict=True)
            except OSError as error:
                raise ManagedPiRuntimeError(
                    f"{source} Skill path cannot be resolved: {candidate}: {error}"
                ) from error
            discovered.setdefault(canonical, candidate)
    return tuple(discovered[key] for key in sorted(discovered, key=lambda item: item.as_posix()))


def _validated_collision_policy(value: object) -> dict[str, frozenset[str]]:
    if not isinstance(value, dict) or value.get("default") != "reject":
        raise ManagedPiRuntimeError(
            "Skill collisionPolicy.default must be the fail-closed value 'reject'"
        )
    bundled_wins = value.get("bundledWins")
    if not isinstance(bundled_wins, dict):
        raise ManagedPiRuntimeError("Skill collisionPolicy.bundledWins must be an object")
    unknown_sources = set(bundled_wins) - {"configured", "pi-installed"}
    if unknown_sources:
        raise ManagedPiRuntimeError(
            "Skill collisionPolicy.bundledWins has unknown sources: "
            + ", ".join(sorted(unknown_sources))
        )
    result: dict[str, frozenset[str]] = {}
    for source in ("configured", "pi-installed"):
        names = bundled_wins.get(source, [])
        if (
            not isinstance(names, list)
            or any(not isinstance(name, str) or not name.strip() for name in names)
            or len(names) != len(set(names))
        ):
            raise ManagedPiRuntimeError(
                f"Skill collisionPolicy.bundledWins.{source} must be a unique string array"
            )
        result[source] = frozenset(names)
    return result


def _resolve_skill_source_collisions(
    *,
    bundled: tuple[Path, ...],
    configured: tuple[Path, ...],
    pi_installed: tuple[Path, ...],
    collision_policy: object,
) -> dict[str, dict[str, str]]:
    bundled_wins = _validated_collision_policy(collision_policy)
    candidates: dict[str, list[dict[str, str]]] = {}
    source_paths = {
        "bundled": bundled,
        "configured": configured,
        "pi-installed": pi_installed,
    }
    for source in SKILL_SOURCE_KINDS:
        files = _discover_skill_files(source_paths[source], source=source)
        for skill_file in files:
            frontmatter = _skill_frontmatter(skill_file, source=source)
            name = str(frontmatter["name"]).strip()
            entry = {
                "name": name,
                "source": source,
                "path": str(skill_file.resolve()),
            }
            candidates.setdefault(name, []).append(entry)

    resolved: dict[str, dict[str, str]] = {}
    for name in sorted(candidates):
        entries = candidates[name]
        if len(entries) == 1:
            resolved[name] = entries[0]
            continue
        bundled_entries = [entry for entry in entries if entry["source"] == "bundled"]
        non_bundled_sources = {entry["source"] for entry in entries if entry["source"] != "bundled"}
        bundled_is_explicit_winner = (
            len(bundled_entries) == 1
            and all(name in bundled_wins[source] for source in non_bundled_sources)
        )
        if bundled_is_explicit_winner:
            resolved[name] = bundled_entries[0]
            continue
        diagnostics = "; ".join(
            f"{entry['source']}={entry['path']}" for entry in entries
        )
        raise ManagedPiRuntimeError(
            f"unresolved Skill name collision for {name!r}: {diagnostics}; "
            "collisionPolicy.default=reject"
        )
    return resolved


def _compact_card_length(card: dict[str, object]) -> int:
    runtime_card = {
        field: card[field]
        for field in ROUTING_CARD_FIELDS
        if field in card
    }
    return len(json.dumps(runtime_card, ensure_ascii=False, separators=(",", ":")))


def _validated_skill_routing_catalog(
    cards_path: Path,
    skills_root: Path,
) -> dict[str, object]:
    try:
        catalog = json.loads(cards_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ManagedPiRuntimeError(
            f"Skill routing card catalog is invalid at {cards_path}: {error}"
        ) from error
    if not isinstance(catalog, dict) or catalog.get("schemaVersion") != (
        "rag-ime.skill-routing-card-catalog.v1"
    ):
        raise ManagedPiRuntimeError("Skill routing card catalog has an invalid schemaVersion")
    cards = catalog.get("cards")
    if not isinstance(cards, list):
        raise ManagedPiRuntimeError("Skill routing card catalog must contain cards[]")

    cards_by_name: dict[str, dict[str, object]] = {}
    for index, value in enumerate(cards):
        if not isinstance(value, dict):
            raise ManagedPiRuntimeError(f"Skill routing card cards[{index}] must be an object")
        name = value.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ManagedPiRuntimeError(
                f"Skill routing card cards[{index}].name must be non-empty"
            )
        if name in cards_by_name:
            raise ManagedPiRuntimeError(f"duplicate Skill routing card: {name}")
        unexpected = set(value) - set(ROUTING_CARD_FIELDS)
        if unexpected:
            raise ManagedPiRuntimeError(
                f"Skill routing card {name!r} has unsupported fields: "
                + ", ".join(sorted(unexpected))
            )
        when = value.get("when")
        does = value.get("does")
        not_for = value.get("notFor")
        if (
            not isinstance(when, list)
            or not when
            or any(not isinstance(item, str) or not item.strip() for item in when)
            or not isinstance(does, str)
            or not does.strip()
            or (
                not_for is not None
                and (
                    not isinstance(not_for, list)
                    or not not_for
                    or any(
                        not isinstance(item, str) or not item.strip()
                        for item in not_for
                    )
                )
            )
            or any(
                field in value
                and (
                    not isinstance(value[field], str)
                    or not str(value[field]).strip()
                )
                for field in ("input", "output")
            )
        ):
            raise ManagedPiRuntimeError(
                f"Skill routing card {name!r} has invalid routing fields"
            )
        if _compact_card_length(value) > MAX_ROUTING_CARD_CHARS:
            raise ManagedPiRuntimeError(
                f"Skill routing card exceeds {MAX_ROUTING_CARD_CHARS} characters: {name}"
            )
        serialized = json.dumps(value, ensure_ascii=False)
        if any(
            marker in serialized
            for marker in (
                "file://",
                str(ROOT),
                str(skills_root.resolve()),
                "/Users/",
                "/Volumes/",
                "/home/",
                "\\Users\\",
            )
        ):
            raise ManagedPiRuntimeError(
                f"Skill routing card leaks a managed filesystem path: {name}"
            )
        cards_by_name[name] = value

    product_skills = _product_skill_dirs(skills_root)
    product_names = {skill.name for skill in product_skills}
    for name in sorted(PROJECT_ROUTING_SKILLS):
        if name not in product_names:
            raise ManagedPiRuntimeError(
                f"project routing card source Skill is missing: bundled={name}"
            )
        source = _skill_frontmatter(skills_root / name / "SKILL.md", source="bundled")
        projection = _skill_routing_projection(source, skill_name=name)
        card = cards_by_name.get(name)
        if card != projection:
            raise ManagedPiRuntimeError(
                f"project routing card drift for {name!r}: "
                "card must exactly project bundled SKILL.md frontmatter"
            )

    collision_policy = catalog.get("collisionPolicy")
    bundled_wins = _validated_collision_policy(collision_policy)
    for source, names in bundled_wins.items():
        unknown_names = names - product_names
        if unknown_names:
            raise ManagedPiRuntimeError(
                f"Skill collisionPolicy.bundledWins.{source} references "
                "unknown bundled Skills: "
                + ", ".join(sorted(unknown_names))
            )
    coverage = catalog.get("coverage")
    if not isinstance(coverage, dict):
        raise ManagedPiRuntimeError("Skill routing card coverage must be an object")
    multiplicity = coverage.get("duplicateSourceMultiplicity")
    if not isinstance(multiplicity, dict):
        raise ManagedPiRuntimeError(
            "Skill routing card coverage.duplicateSourceMultiplicity must be an object"
        )
    for name, count in multiplicity.items():
        if (
            not isinstance(name, str)
            or not name.strip()
            or not isinstance(count, int)
            or count < 2
        ):
            raise ManagedPiRuntimeError(
                f"invalid Skill routing source multiplicity for {name!r}: {count!r}"
            )
    represented_entries = len(cards) + sum(
        count - 1 for count in multiplicity.values()
    )
    derived_scope = {
        "bundledSkillEntries": len(product_skills),
        "projectedBundledCards": len(PROJECT_ROUTING_SKILLS),
        "canonicalCards": len(cards),
    }
    if catalog.get("scope") != derived_scope:
        raise ManagedPiRuntimeError(
            f"Skill routing card scope drift: expected {derived_scope!r}"
        )
    if coverage.get("representedSourceEntries") != represented_entries:
        raise ManagedPiRuntimeError(
            "Skill routing card coverage drift: "
            f"expected representedSourceEntries={represented_entries}"
        )
    return catalog


def _copy_product_skills(source_root: Path, runtime_root: Path) -> tuple[str, ...]:
    runtime_root.mkdir(parents=True, exist_ok=True)
    copied: list[str] = []
    for skill in _product_skill_dirs(source_root):
        shutil.copytree(skill, runtime_root / skill.name, dirs_exist_ok=True)
        copied.append(skill.name)
    return tuple(copied)


def _json_object_without_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate object key {key!r}")
        value[key] = item
    return value


def _extension_pi_resource_prefix(raw_path: str) -> PurePosixPath:
    """Return the non-glob prefix of a Pi package resource path."""

    if "\\" in raw_path:
        raise ValueError("resource paths must use POSIX separators")
    path = raw_path[1:] if raw_path.startswith("!") else raw_path
    if not path or path.startswith("/"):
        raise ValueError("resource paths must be relative")
    relative = PurePosixPath(path)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("resource paths must stay inside the package")
    prefix: list[str] = []
    for part in relative.parts:
        if part in ("", "."):
            continue
        if any(character in part for character in "*?[{"):
            break
        prefix.append(part)
    return PurePosixPath(*prefix)


def _extension_pi_package_manifest(
    package_root: Path,
    *,
    source: str,
) -> dict[str, object]:
    if package_root.is_symlink() or not package_root.is_dir():
        raise ManagedPiRuntimeError(
            f"Extension App Pi Package directory is invalid or symlinked: {source}"
        )
    try:
        package_items = tuple(package_root.rglob("*"))
    except OSError as error:
        raise ManagedPiRuntimeError(
            f"Extension App Pi Package cannot be inspected at {source}: {error}"
        ) from error
    for item in package_items:
        if item.is_symlink():
            raise ManagedPiRuntimeError(
                f"Extension App Pi Package contains a symlink: {item}"
            )
    files = [item for item in package_items if item.is_file()]
    if (
        len(files) > _MAX_NATIVE_PACKAGE_FILES
        or sum(item.stat().st_size for item in files) > _MAX_NATIVE_PACKAGE_BYTES
    ):
        raise ManagedPiRuntimeError(
            "Extension App Pi Package exceeds Native Runtime file or size limits"
        )
    for directory, directories, file_names in os.walk(package_root, followlinks=False):
        names = [*directories, *file_names]
        if len({name.casefold() for name in names}) != len(names):
            raise ManagedPiRuntimeError(
                f"Extension App Pi Package has a case-colliding path in {directory}"
            )
        for name in names:
            if not _EXTENSION_PACKAGE_SEGMENT.fullmatch(name):
                raise ManagedPiRuntimeError(
                    "Extension App Pi Package paths must use lowercase ASCII, "
                    f"digits, dots, or hyphens (except SKILL.md): {name!r}"
                )

    manifest_path = package_root / "package.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise ManagedPiRuntimeError(
            f"Extension App Pi Package manifest is missing or symlinked: {manifest_path}"
        )
    try:
        manifest = json.loads(
            manifest_path.read_text(encoding="utf-8"),
            object_pairs_hook=_json_object_without_duplicate_keys,
        )
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
        raise ManagedPiRuntimeError(
            f"Extension App Pi Package manifest is invalid at {manifest_path}: {error}"
        ) from error
    if not isinstance(manifest, dict):
        raise ManagedPiRuntimeError(
            f"Extension App Pi Package manifest must be an object: {manifest_path}"
        )

    package_name = manifest.get("name")
    if not isinstance(package_name, str) or not _PI_PACKAGE_NAME.fullmatch(
        package_name
    ):
        raise ManagedPiRuntimeError(
            f"Extension App Pi Package manifest has an invalid name at {manifest_path}"
        )
    package_version = manifest.get("version")
    if not isinstance(package_version, str) or not _PI_PACKAGE_VERSION.fullmatch(
        package_version
    ):
        raise ManagedPiRuntimeError(
            f"Extension App Pi Package manifest has an invalid version at {manifest_path}"
        )

    pi_manifest = manifest.get("pi")
    if not isinstance(pi_manifest, dict):
        raise ManagedPiRuntimeError(
            f"Extension App Pi Package manifest requires a pi object at {manifest_path}"
        )
    resource_count = 0
    for key in _PI_PACKAGE_RESOURCE_KEYS:
        if key not in pi_manifest:
            continue
        paths = pi_manifest[key]
        if not isinstance(paths, list) or not paths:
            raise ManagedPiRuntimeError(
                f"Extension App Pi Package pi.{key} must be a non-empty string array "
                f"at {manifest_path}"
            )
        for raw_path in paths:
            if not isinstance(raw_path, str) or not raw_path.strip():
                raise ManagedPiRuntimeError(
                    f"Extension App Pi Package pi.{key} contains an invalid resource "
                    f"path at {manifest_path}"
                )
            try:
                prefix = _extension_pi_resource_prefix(raw_path.strip())
            except ValueError as error:
                raise ManagedPiRuntimeError(
                    f"Extension App Pi Package pi.{key} contains an unsafe resource "
                    f"path {raw_path!r} at {manifest_path}: {error}"
                ) from error
            if raw_path.strip().startswith("!"):
                continue
            prefix_path = package_root.joinpath(*prefix.parts)
            if not prefix_path.exists():
                raise ManagedPiRuntimeError(
                    f"Extension App Pi Package pi.{key} references a missing path "
                    f"{raw_path!r} at {manifest_path}"
                )
            resource_count += 1
    if resource_count == 0:
        raise ManagedPiRuntimeError(
            f"Extension App Pi Package manifest declares no Pi resources at {manifest_path}"
        )
    display_name = manifest.get("displayName")
    if display_name is not None and (
        not isinstance(display_name, str) or not display_name.strip()
    ):
        raise ManagedPiRuntimeError(
            f"Extension App Pi Package manifest has an invalid displayName at {manifest_path}"
        )
    return manifest


def _extension_app_manifest(
    app_directory: Path,
    *,
    slug: str,
    package_root: Path,
    package_manifest: dict[str, object],
) -> dict[str, object] | None:
    """Validate the optional App contract next to a Pi Package.

    Package-only entries remain compatible with the generic bundled Package
    importer.  A directory that provides ``pawos-app.json`` is an Extension
    App and must carry the complete co-version binding before it can enter a
    managed Runtime payload.
    """

    manifest_path = app_directory / "pawos-app.json"
    if not manifest_path.exists() and not manifest_path.is_symlink():
        return None
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise ManagedPiRuntimeError(
            f"Extension App manifest is missing or symlinked: {manifest_path}"
        )
    try:
        app_manifest = json.loads(
            manifest_path.read_text(encoding="utf-8"),
            object_pairs_hook=_json_object_without_duplicate_keys,
        )
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
        raise ManagedPiRuntimeError(
            f"Extension App manifest is invalid at {manifest_path}: {error}"
        ) from error
    if not isinstance(app_manifest, dict):
        raise ManagedPiRuntimeError(
            f"Extension App manifest must be an object: {manifest_path}"
        )

    expected_id = f"extension:{slug}"
    if app_manifest.get("schemaVersion") != "pawos.extension-app.v1":
        raise ManagedPiRuntimeError(
            f"Extension App manifest schemaVersion is unsupported: {manifest_path}"
        )
    if app_manifest.get("id") != expected_id:
        raise ManagedPiRuntimeError(
            f"Extension App manifest id must match its source directory: {slug}"
        )
    if app_manifest.get("route") != f"/extensions/{slug}":
        raise ManagedPiRuntimeError(
            f"Extension App manifest route must match its source directory: {slug}"
        )
    required_text = (
        "packageId",
        "version",
        "label",
        "shortLabel",
        "tagline",
        "skillRef",
        "verticalSuiteId",
        "verticalSuiteRevision",
    )
    for field in required_text:
        value = app_manifest.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ManagedPiRuntimeError(
                f"Extension App manifest requires non-empty {field}: {manifest_path}"
            )
    if app_manifest.get("presentation") not in _EXTENSION_APP_PRESENTATIONS:
        raise ManagedPiRuntimeError("Extension App manifest presentation is invalid")
    if app_manifest.get("accent") not in _EXTENSION_APP_ACCENTS:
        raise ManagedPiRuntimeError("Extension App manifest accent is invalid")
    sandbox_contract = app_manifest.get("sandbox")
    if sandbox_contract is not None and (
        not isinstance(sandbox_contract, dict)
        or set(sandbox_contract) != _EXTENSION_APP_SANDBOX_FIELDS
        or sandbox_contract.get("default") not in _EXTENSION_APP_SANDBOX_DEFAULTS
        or sandbox_contract.get("connectorPackageId") != "vertical-agent-sandbox"
        or sandbox_contract.get("policyId") != "vertical-readonly-v1"
    ):
        raise ManagedPiRuntimeError("Extension App manifest sandbox contract is invalid")
    icon = app_manifest.get("icon")
    if (
        not isinstance(icon, dict)
        or icon.get("symbol") not in _EXTENSION_APP_ICON_SYMBOLS
        or not isinstance(icon.get("background"), str)
        or re.fullmatch(r"#[0-9A-Fa-f]{6}", str(icon.get("background") or "")) is None
    ):
        raise ManagedPiRuntimeError("Extension App manifest icon is invalid")
    package_name = package_manifest.get("name")
    package_version = package_manifest.get("version")
    if app_manifest["packageId"] != package_name:
        raise ManagedPiRuntimeError(
            "Extension App packageId does not match Pi Package name: "
            f"{app_manifest['packageId']!r} != {package_name!r}"
        )
    if app_manifest["version"] != package_version:
        raise ManagedPiRuntimeError(
            "Extension App version does not match Pi Package version: "
            f"{app_manifest['version']!r} != {package_version!r}"
        )
    skill_ref = str(app_manifest["skillRef"])
    if not _EXTENSION_APP_SLUG.fullmatch(skill_ref):
        raise ManagedPiRuntimeError(
            f"Extension App skillRef is invalid: {skill_ref!r}"
        )
    skill_file = package_root / "skills" / skill_ref / "SKILL.md"
    if skill_file.is_symlink() or not skill_file.is_file():
        raise ManagedPiRuntimeError(
            "Extension App skillRef does not resolve to a regular package Skill: "
            f"{skill_ref}"
        )
    try:
        skill_sha256 = hashlib.sha256(skill_file.read_bytes()).hexdigest()
    except OSError as error:
        raise ManagedPiRuntimeError(
            f"Extension App Skill cannot be hashed: {skill_file}: {error}"
        ) from error
    if app_manifest.get("skillSha256") != skill_sha256:
        raise ManagedPiRuntimeError(
            "Extension App manifest skillSha256 does not match its Package Skill"
        )

    try:
        from rag_ime.vertical_agent_harness import resolve_builtin_vertical_suite

        suite = resolve_builtin_vertical_suite(
            app_manifest["verticalSuiteId"],
            app_manifest["verticalSuiteRevision"],
        )
    except Exception as error:
        raise ManagedPiRuntimeError(
            "Extension App vertical suite binding is not registered: "
            f"{app_manifest['verticalSuiteId']!r}/"
            f"{app_manifest['verticalSuiteRevision']!r}: {error}"
        ) from error
    sandbox = suite.get("sandbox")
    if (
        not isinstance(sandbox, dict)
        or sandbox.get("network") != "blocked"
        or sandbox.get("productionWriteBlocked") is not True
    ):
        raise ManagedPiRuntimeError(
            "Extension App vertical suite must block network and production writes"
        )

    binding_sha256 = app_manifest.get("bindingSha256")
    if not isinstance(binding_sha256, str) or not _SHA256.fullmatch(binding_sha256):
        raise ManagedPiRuntimeError(
            "Extension App manifest bindingSha256 must be a lowercase SHA-256"
        )
    expected_binding = extension_app_binding_sha256(
        app_manifest,
        skill_sha256=skill_sha256,
        package_version=str(package_version),
    )
    if binding_sha256 != expected_binding:
        raise ManagedPiRuntimeError(
            "Extension App bindingSha256 does not match canonical manifest, "
            f"Skill, and package version: expected {expected_binding}"
        )
    binding_capability = extension_app_binding_capability(binding_sha256)

    paw = package_manifest.get("paw")
    if not isinstance(paw, dict):
        raise ManagedPiRuntimeError(
            "Extension App Pi Package manifest requires a paw object"
        )
    capabilities = paw.get("capabilities")
    if (
        not isinstance(capabilities, list)
        or any(not isinstance(value, str) or not value.strip() for value in capabilities)
        or len(capabilities) != len(set(capabilities))
    ):
        raise ManagedPiRuntimeError(
            "Extension App Pi Package paw.capabilities must be a unique string array"
        )
    existing_binding = extension_app_binding_capability_from_capabilities(capabilities)
    raw_binding_tokens = [
        value
        for value in capabilities
        if isinstance(value, str) and value.startswith("pawos.extension.binding.")
    ]
    if raw_binding_tokens != [binding_capability] or existing_binding != binding_capability:
        raise ManagedPiRuntimeError(
            "Extension App Pi Package binding capability does not match bindingSha256"
        )
    package_extension = paw.get("extensionApp")
    if not isinstance(package_extension, dict):
        raise ManagedPiRuntimeError(
            "Extension App Pi Package manifest requires paw.extensionApp"
        )
    expected_extension = {
        "id": expected_id,
        "packageId": str(package_name),
        "version": str(package_version),
        "bindingSha256": binding_sha256,
        "skillRef": skill_ref,
        "skillSha256": skill_sha256,
        "verticalSuiteId": str(app_manifest["verticalSuiteId"]),
        "verticalSuiteRevision": str(app_manifest["verticalSuiteRevision"]),
        "sandbox": sandbox_contract,
        "manifest": app_manifest,
    }
    for field, expected in expected_extension.items():
        if package_extension.get(field) != expected:
            raise ManagedPiRuntimeError(
                "Extension App Pi Package paw.extensionApp does not match "
                f"the App manifest for {field}: expected {expected!r}"
            )
    return {
        "id": expected_id,
        "packageId": str(package_name),
        "version": str(package_version),
        "bindingSha256": binding_sha256,
        "bindingCapability": binding_capability,
        "skillRef": skill_ref,
        "skillSha256": skill_sha256,
        "verticalSuiteId": str(app_manifest["verticalSuiteId"]),
        "verticalSuiteRevision": str(app_manifest["verticalSuiteRevision"]),
        "sandbox": sandbox_contract,
        "manifest": app_manifest,
    }


def _discover_extension_app_pi_packages(
    source_root: Path | None = None,
) -> tuple[tuple[str, Path, dict[str, object], dict[str, object] | None], ...]:
    root = EXTENSION_APPS_ROOT if source_root is None else source_root
    if root.is_symlink():
        raise ManagedPiRuntimeError(
            f"Extension App source root must be a real directory: {root}"
        )
    if not root.exists():
        return ()
    if not root.is_dir():
        raise ManagedPiRuntimeError(
            f"Extension App source root is not a directory: {root}"
        )

    discovered: list[
        tuple[str, Path, dict[str, object], dict[str, object] | None]
    ] = []
    for app_directory in sorted(root.iterdir(), key=lambda item: item.name):
        if app_directory.is_symlink():
            raise ManagedPiRuntimeError(
                f"Extension App source directory contains a symlink: {app_directory}"
            )
        if not app_directory.is_dir():
            continue
        package_root = app_directory / "pi-package"
        if not package_root.exists() and not package_root.is_symlink():
            continue
        if not _EXTENSION_APP_SLUG.fullmatch(app_directory.name):
            raise ManagedPiRuntimeError(
                f"Extension App source directory has an invalid slug: {app_directory.name}"
            )
        manifest = _extension_pi_package_manifest(
            package_root,
            source=f"{app_directory.name}/pi-package",
        )
        app_manifest = _extension_app_manifest(
            app_directory,
            slug=app_directory.name,
            package_root=package_root,
            package_manifest=manifest,
        )
        discovered.append((app_directory.name, package_root, manifest, app_manifest))
    return tuple(discovered)


def _bundled_pi_package_names(source_root: Path) -> set[str]:
    names: set[str] = set()
    for item in source_root.iterdir():
        if (
            item.name in DISABLED_BUNDLED_PI_PACKAGE_DIRS
            or item.is_symlink()
            or not item.is_dir()
        ):
            continue
        package_json = item / "package.json"
        if package_json.is_symlink() or not package_json.is_file():
            continue
        try:
            manifest = json.loads(package_json.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if isinstance(manifest, dict) and isinstance(manifest.get("name"), str):
            names.add(manifest["name"])
    return names


def _hash_extension_app_pi_packages(source_root: Path | None = None) -> bytes:
    digest = hashlib.sha256()
    for slug, package_root, _manifest, app_manifest in _discover_extension_app_pi_packages(source_root):
        digest.update(slug.encode("utf-8"))
        digest.update(b"\0")
        digest.update(_hash_tree(package_root))
        if app_manifest is not None:
            digest.update(
                json.dumps(
                    app_manifest,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            )
            digest.update(b"\0")
    return digest.digest()


def _copy_bundled_pi_packages(
    source_root: Path,
    destination: Path,
    *,
    extension_apps_root: Path | None = None,
) -> None:
    """Keep Pi's built-in package catalog adjacent to the bundled Host.

    ``bundled-package-catalog.ts`` resolves ``../pi-packages`` relative to the
    Runtime Host entrypoint.  A staged or installed payload therefore needs
    the tracked catalog tree at the payload root, not in App Support from an
    older installation.
    """

    catalog = source_root / "catalog.json"
    if source_root.is_symlink() or not source_root.is_dir():
        raise ManagedPiRuntimeError("bundled Pi Package source is missing")
    if catalog.is_symlink() or not catalog.is_file():
        raise ManagedPiRuntimeError("bundled Pi Package catalog is missing")
    for item in source_root.rglob("*"):
        if item.is_symlink():
            raise ManagedPiRuntimeError(
                f"bundled Pi Package source contains a symlink: {item}"
            )
    extension_packages = _discover_extension_app_pi_packages(extension_apps_root)
    try:
        catalog_payload = json.loads(
            catalog.read_text(encoding="utf-8")
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ManagedPiRuntimeError(
            "bundled Pi Package catalog cannot be parsed before copying"
        ) from error
    if not isinstance(catalog_payload, dict):
        raise ManagedPiRuntimeError("bundled Pi Package catalog must be an object")
    packages = catalog_payload.get("packages")
    if not isinstance(packages, list):
        raise ManagedPiRuntimeError("bundled Pi Package catalog has no packages list")

    catalog_directories: set[str] = set()
    catalog_names: set[str] = set()
    for package in packages:
        if not isinstance(package, dict):
            continue
        directory = package.get("directory")
        if not isinstance(directory, str) or not directory:
            continue
        if directory in catalog_directories:
            raise ManagedPiRuntimeError(
                f"bundled Pi Package catalog has duplicate directory: {directory}"
            )
        catalog_directories.add(directory)
        package_name = package.get("name")
        if isinstance(package_name, str) and package_name:
            if package_name in catalog_names:
                raise ManagedPiRuntimeError(
                    f"bundled Pi Package catalog has duplicate name: {package_name}"
                )
            catalog_names.add(package_name)
    source_directories = {
        item.name
        for item in source_root.iterdir()
        if item.is_dir() and not item.is_symlink()
    }
    existing_names = _bundled_pi_package_names(source_root) | catalog_names
    extension_directories: set[str] = set()
    extension_names: set[str] = set()
    extension_entries: list[dict[str, object]] = []
    for slug, _package_root, manifest, app_manifest in extension_packages:
        if slug in extension_directories or slug in catalog_directories or slug in source_directories:
            raise ManagedPiRuntimeError(
                f"Extension App Pi Package has duplicate directory: {slug}"
            )
        package_name = manifest["name"]
        if package_name in extension_names or package_name in existing_names:
            raise ManagedPiRuntimeError(
                f"Extension App Pi Package has duplicate name: {package_name}"
            )
        extension_directories.add(slug)
        extension_names.add(package_name)
        display_name = manifest.get("displayName") or package_name
        entry: dict[str, object] = {
            "directory": slug,
            "displayName": str(display_name),
        }
        if app_manifest is not None:
            entry["extensionApp"] = {
                key: value
                for key, value in app_manifest.items()
                if key != "manifest"
            }
        extension_entries.append(entry)
    if destination.exists():
        raise ManagedPiRuntimeError(
            f"bundled Pi Package destination already exists: {destination}"
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.mkdir(parents=True, exist_ok=False)
    for item in source_root.iterdir():
        if item.name in DISABLED_BUNDLED_PI_PACKAGE_DIRS:
            continue
        target = destination / item.name
        if item.is_dir():
            shutil.copytree(item, target)
        else:
            shutil.copy2(item, target)

    filtered_packages = [
        package
        for package in packages
        if not isinstance(package, dict)
        or package.get("directory") not in DISABLED_BUNDLED_PI_PACKAGE_DIRS
    ]
    for slug, package_root, _manifest, _app_manifest in extension_packages:
        shutil.copytree(package_root, destination / slug)
    if len(filtered_packages) != len(packages) or extension_entries:
        catalog_payload["packages"] = filtered_packages + extension_entries
        (destination / "catalog.json").write_text(
            json.dumps(catalog_payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )


def _runtime_host_banner(
    skills_root: Path,
    collision_policy: object | None = None,
) -> str:
    skill_names = [item.name for item in _product_skill_dirs(skills_root)]
    if collision_policy is None:
        collision_policy = json.loads(
            SKILL_ROUTING_CARDS.read_text(encoding="utf-8")
        ).get("collisionPolicy")
    bundled_wins = _validated_collision_policy(collision_policy)
    policy_json = {
        "default": "reject",
        "bundledWins": {
            source: sorted(names) for source, names in bundled_wins.items()
        },
    }
    return (
        'import { createRequire as __createRequire } from "node:module"; '
        'import { basename as __basename, delimiter as __pathDelimiter, dirname as __dirname, join as __join, resolve as __resolve } from "node:path"; '
        'import { existsSync as __existsSync, readdirSync as __readdirSync, readFileSync as __readFileSync, realpathSync as __realpathSync, statSync as __statSync } from "node:fs"; '
        'import { homedir as __homedir } from "node:os"; '
        'import { fileURLToPath as __fileURLToPath } from "node:url"; '
        'const require = __createRequire(import.meta.url); '
        f'const __ragImeSkillNames = {json.dumps(skill_names, ensure_ascii=True)}; '
        f'const __ragImeSkillCollisionPolicy = {json.dumps(policy_json, ensure_ascii=True)}; '
        'const __ragImeRuntimeDir = __dirname(__fileURLToPath(import.meta.url)); '
        'const __ragImeSkillPaths = __ragImeSkillNames.map((name) => __join(__ragImeRuntimeDir, "skills", name)); '
        'const __ragImeRoutingCards = __join(__ragImeRuntimeDir, "skill-routing-cards.json"); '
        'const __ragImeConfiguredSkills = (process.env.RAG_IME_PI_SKILL_PATHS || "").split(__pathDelimiter).filter(Boolean); '
        'const __ragImePiAgentDir = __resolve(process.env.PI_CODING_AGENT_DIR || __join(__homedir(), ".pi", "agent")); '
        'process.env.JITI_FS_CACHE = __join(__ragImePiAgentDir, "cache", "jiti"); '
        'const __ragImePiInstalledSkills = (process.env.RAG_IME_PI_USER_SKILL_PATHS || __join(__ragImePiAgentDir, "skills")).split(__pathDelimiter).filter(Boolean); '
        'const __ragImeFindSkillFiles = (path) => { if (!__existsSync(path)) return []; const stat = __statSync(path); if (!stat.isDirectory()) return path.endsWith("SKILL.md") ? [path] : []; if (__existsSync(__join(path, "SKILL.md"))) return [__join(path, "SKILL.md")]; return __readdirSync(path, { withFileTypes: true }).sort((a, b) => a.name.localeCompare(b.name)).flatMap((entry) => entry.isDirectory() ? __ragImeFindSkillFiles(__join(path, entry.name)) : []); }; '
        'const __ragImeSourcePaths = { bundled: __ragImeSkillPaths, configured: __ragImeConfiguredSkills, "pi-installed": __ragImePiInstalledSkills }; '
        'const __ragImeCandidates = new Map(); '
        'for (const [source, paths] of Object.entries(__ragImeSourcePaths)) for (const path of paths.flatMap(__ragImeFindSkillFiles)) { const canonical = __realpathSync(path); const body = __readFileSync(canonical, "utf8"); const frontmatter = /^---\\s*\\r?\\n([\\s\\S]*?)\\r?\\n---(?:\\s*\\r?\\n|\\s*$)/.exec(body); const match = frontmatter && /^name:\\s*["\']?([^"\'#\\r\\n]+)["\']?\\s*$/m.exec(frontmatter[1]); if (!match) throw new Error(`Invalid ${source} Skill frontmatter at ${canonical}: missing name`); const name = match[1].trim(); if (name !== __basename(__dirname(canonical))) throw new Error(`Invalid ${source} Skill frontmatter at ${canonical}: name "${name}" does not match directory`); const entries = __ragImeCandidates.get(name) || []; if (!entries.some((entry) => entry.path === canonical)) entries.push({ name, source, path: canonical }); __ragImeCandidates.set(name, entries); } '
        'for (const [name, entries] of [...__ragImeCandidates].sort(([a], [b]) => a.localeCompare(b))) { if (entries.length < 2) continue; const bundled = entries.filter((entry) => entry.source === "bundled"); const otherSources = new Set(entries.filter((entry) => entry.source !== "bundled").map((entry) => entry.source)); const explicitBundledWinner = bundled.length === 1 && [...otherSources].every((source) => (__ragImeSkillCollisionPolicy.bundledWins[source] || []).includes(name)); if (!explicitBundledWinner) throw new Error(`Unresolved Skill name collision for "${name}": ${entries.map((entry) => `${entry.source}=${entry.path}`).join("; ")}; collisionPolicy.default=reject`); } '
        'process.env.RAG_IME_PI_SKILL_PATHS = '
        '[...__ragImeSkillPaths, ...__ragImeConfiguredSkills].filter(Boolean).join(__pathDelimiter); '
        'process.env.RAG_IME_PI_SKILL_ROUTING_CARDS = '
        'process.env.RAG_IME_PI_SKILL_ROUTING_CARDS || __ragImeRoutingCards;'
    )


def _bundle_oauth_runtime_modules(
    *,
    esbuild: Path,
    pi_root: Path,
    runtime_dir: Path,
) -> None:
    """Package the OAuth modules intentionally left opaque to Pi's main bundle.

    Pi keeps these modules behind variable dynamic imports so browser builds do
    not absorb Node-only callback servers and PKCE code. The managed Runtime
    Host is a self-contained Node payload, so its packager must preserve those
    runtime-loaded files next to ``cli.mjs``. Bundling each module independently
    keeps the browser boundary intact while making the installed Host complete.
    """

    for output_name, (relative_source, _export_name) in _OAUTH_RUNTIME_MODULES.items():
        source = pi_root / relative_source
        if not source.is_file() or source.is_symlink():
            raise ManagedPiRuntimeError(
                f"managed Pi OAuth runtime source is missing: {relative_source}"
            )
        output = runtime_dir / output_name
        _run(
            [
                str(esbuild),
                str(source),
                "--bundle",
                "--platform=node",
                "--format=esm",
                "--target=node22",
                f"--outfile={output}",
                (
                    '--banner:js=import { createRequire as __createRequire } '
                    'from "node:module"; const require = __createRequire(import.meta.url);'
                ),
            ],
            cwd=pi_root,
        )
        output.chmod(0o644)
        # The SDK's compiled loader requests .js while source-based entrypoints
        # request .ts. Both are opaque imports inside the same managed bundle.
        # These files contain bundled JavaScript, regardless of the suffix.
        shutil.copy2(output, output.with_suffix(".js"))


def _smoke_oauth_runtime_modules(node: Path, runtime_dir: Path) -> dict[str, object]:
    module_specs = [
        {
            "url": (runtime_dir / Path(output_name).with_suffix(suffix)).as_uri(),
            "exportName": export_name,
        }
        for output_name, (_relative_source, export_name) in _OAUTH_RUNTIME_MODULES.items()
        for suffix in (".ts", ".js")
    ]
    probe = (
        f"const specs = {json.dumps(module_specs, separators=(',', ':'))};"
        "const loaded = new Map();"
        "for (const spec of specs) {"
        " const module = await import(spec.url);"
        " if (!(spec.exportName in module))"
        "  throw new Error(`missing OAuth export ${spec.exportName}`);"
        " loaded.set(spec.exportName, module[spec.exportName]);"
        "}"
        "const codex = loaded.get('openaiCodexOAuth');"
        "const resolved = await codex.toAuth({"
        " type:'oauth',access:'managed-runtime-smoke',refresh:'unused',"
        " expires:Date.now()+60000,accountId:'smoke'"
        "});"
        "if (resolved.apiKey !== 'managed-runtime-smoke')"
        " throw new Error('OpenAI Codex OAuth derivation failed');"
        "process.stdout.write(JSON.stringify({ok:true,moduleCount:specs.length}));"
    )
    completed = subprocess.run(
        [str(node), "--input-type=module", "-e", probe],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=30,
    )
    try:
        response = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise ManagedPiRuntimeError(
            "managed Pi OAuth runtime smoke returned invalid JSON"
        ) from exc
    if response != {"ok": True, "moduleCount": len(_OAUTH_RUNTIME_MODULES) * 2}:
        raise ManagedPiRuntimeError("managed Pi OAuth runtime smoke did not load every module")
    return response


def _smoke_runtime(node: Path, entrypoint: Path) -> dict[str, object]:
    request = {
        "protocolVersion": "2",
        "id": "managed-runtime-build-smoke",
        "method": "hello",
        "params": {},
    }
    with tempfile.TemporaryDirectory(prefix="rag-ime-pi-runtime-smoke-") as app_support:
        environment = dict(os.environ)
        environment.update(
            {
                "RAG_IME_APP_SUPPORT_DIR": app_support,
                "RAG_IME_PLUGIN_APPROVAL_TOKEN": "managed-runtime-build-smoke",
            }
        )
        completed = subprocess.run(
            [str(node), str(entrypoint)],
            input=json.dumps(request, separators=(",", ":")) + "\n",
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=30,
            env=environment,
        )
    lines = [line for line in completed.stdout.splitlines() if line.strip()]
    if len(lines) != 1:
        raise ManagedPiRuntimeError("managed Pi Runtime Host smoke test returned an invalid response")
    response = json.loads(lines[0])
    result = response.get("result") if isinstance(response, dict) else None
    capabilities = result.get("capabilities") if isinstance(result, dict) else None
    if (
        not isinstance(response, dict)
        or not response.get("ok")
        or not isinstance(result, dict)
        or result.get("protocolVersion") != "2"
        or not isinstance(capabilities, dict)
        or not capabilities.get("multiSession")
        or not capabilities.get("sessionSkillAllowlist")
    ):
        raise ManagedPiRuntimeError("managed Pi Runtime Host smoke test did not negotiate protocol v2")
    return response


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build a verified, SDK-backed Pi Runtime Host v2 payload without registry access"
    )
    parser.add_argument(
        "--pi-worktree",
        required=True,
        help=(
            "Explicit clean Pi worktree containing "
            "integrations/rag-ime-runtime-host"
        ),
    )
    parser.add_argument("--output", default="")
    parser.add_argument(
        "--node",
        default=None,
        help="Relocatable Node 22+ binary copied into the managed payload",
    )
    parser.add_argument(
        "--preflight",
        action="store_true",
        help="Verify the explicit Pi worktree without building or touching App Support",
    )
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--skip-smoke", action="store_true")
    args = parser.parse_args(argv)

    pi_root = Path(args.pi_worktree).expanduser().resolve()
    package_root = _runtime_host_root(pi_root)
    package_json = pi_root / "packages" / "coding-agent" / "package.json"
    esbuild = pi_root / "node_modules" / ".bin" / "esbuild"
    try:
        source_commit = _source_revision(pi_root)
        if args.preflight:
            if not package_json.is_file() or not esbuild.is_file():
                raise ManagedPiRuntimeError(
                    "canonical Pi worktree is incomplete "
                    "(missing coding-agent package or esbuild)"
                )
            _verified_session_runtime_contract(pi_root)
    except (OSError, subprocess.SubprocessError, ManagedPiRuntimeError) as exc:
        action = "preflight" if args.preflight else "build"
        print(f"managed Pi runtime {action} failed: {exc}", file=sys.stderr)
        return 1
    if args.preflight:
        print(
            json.dumps(
                {
                    "ok": True,
                    "piWorktree": str(pi_root),
                    "sourceCommit": source_commit,
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    node = (
        Path(args.node).expanduser().resolve()
        if args.node
        else Path(_default_node()).expanduser().resolve()
    )
    if not package_json.is_file() or not esbuild.is_file():
        print(
            "managed Pi runtime build failed: canonical Pi worktree is incomplete "
            "(missing coding-agent package or esbuild)",
            file=sys.stderr,
        )
        return 1
    if not node.is_file():
        print("managed Pi runtime build failed: Node executable is missing", file=sys.stderr)
        return 1
    node_relocation_error = _node_relocation_error(node)
    if node_relocation_error:
        print(
            "managed Pi runtime build failed: "
            f"{node_relocation_error}; set RAG_IME_MANAGED_NODE to a standalone Node binary",
            file=sys.stderr,
        )
        return 1

    try:
        pi_version = str(json.loads(package_json.read_text(encoding="utf-8"))["version"])
        session_runtime_contract, session_runtime_contract_sha256 = _verified_session_runtime_contract(
            pi_root
        )
        product_commit = _run(["git", "rev-parse", "HEAD"], cwd=ROOT)
        product_commit_ms = int(
            _run(["git", "show", "-s", "--format=%ct", product_commit], cwd=ROOT)
        ) * 1_000
        provider_bridge_source = ROOT / "rag_ime" / "node" / "pi_provider_bridge_bundled.ts"
        product_skills = ROOT / "integrations" / "pi" / "skills"
        skill_routing_cards = SKILL_ROUTING_CARDS
        routing_catalog = _validated_skill_routing_catalog(
            skill_routing_cards,
            product_skills,
        )
        routing_catalog_bytes = (
            json.dumps(routing_catalog, ensure_ascii=False, indent=2) + "\n"
        ).encode("utf-8")
        packager_digest = hashlib.sha256(
            provider_bridge_source.read_bytes()
            + (ROOT / "rag_ime" / "node" / "pi_chatgpt_accounts.mjs").read_bytes()
            + (ROOT / "licenses" / "jose-LICENSE").read_bytes()
            + Path(__file__).read_bytes()
            + (ROOT / "scripts" / "runtime-host-session-resource-policy.ts").read_bytes()
            + _hash_tree(product_skills)
            + _hash_extension_app_pi_packages(EXTENSION_APPS_ROOT)
            + routing_catalog_bytes
            + SESSION_RUNTIME_CONTRACT.read_bytes()
            + json.dumps(CONTROL_TOOL_IDS, separators=(",", ":")).encode("utf-8")
            + product_commit.encode("ascii")
            # One HEAD can have several explicit development source trees.
            # Their full verified +dirty.<digest> identity must name new payloads.
            + b"\0pi-source\0"
            + source_commit.encode("ascii")
        ).hexdigest()[:10]
        commit_prefix = source_commit.split("+", 1)[0][:12]
        runtime_version = f"pi-{pi_version}-{commit_prefix}-raghost-{packager_digest}"
        destination = (
            Path(args.output).expanduser().resolve()
            if args.output
            else ROOT / "build" / "managed-pi-runtime" / runtime_version
        )
        if destination.exists():
            if not args.force:
                raise ManagedPiRuntimeError(f"output already exists: {destination}")
            shutil.rmtree(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        staging = destination.parent / f".{destination.name}.staging-{uuid.uuid4().hex}"
        staging.mkdir(mode=0o700)
        try:
            runtime_dir = staging / "runtime-host"
            bin_dir = staging / "bin"
            runtime_dir.mkdir(mode=0o700)
            bin_dir.mkdir(mode=0o700)
            # Jiti loads its Babel transform relative to its own module URL.
            # Keep that package intact instead of relocating its dynamic require.
            shutil.copytree(pi_root / "node_modules" / "jiti", runtime_dir / "node_modules" / "jiti")
            _copy_bundled_pi_packages(
                package_root / "pi-packages",
                staging / "pi-packages",
            )
            _copy_product_skills(product_skills, runtime_dir / "skills")
            (runtime_dir / "skill-routing-cards.json").write_bytes(
                routing_catalog_bytes
            )
            shutil.copy2(
                SESSION_RUNTIME_CONTRACT,
                runtime_dir / "session-runtime-host-contract.json",
            )
            bundled_entrypoint = runtime_dir / "cli.mjs"
            with tempfile.TemporaryDirectory(
                prefix="rag-ime-runtime-host-overlay-"
            ) as overlay_root:
                overlay_package_root = _prepare_runtime_host_overlay(
                    package_root,
                    Path(overlay_root) / "runtime-host",
                    pi_root=pi_root,
                )
                sdk_overlay = _prepare_sdk_prompt_overlay(
                    pi_root, overlay_package_root / "sdk-prompt-overlay",
                )
                _run(
                    [
                        str(esbuild),
                        str(overlay_package_root / "src" / "cli.ts"),
                        "--bundle",
                        "--platform=node",
                        "--format=esm",
                        "--target=node22",
                        "--external:jiti",
                        f"--alias:@earendil-works/pi-coding-agent={sdk_overlay / 'dist' / 'index.js'}",
                        f"--outfile={bundled_entrypoint}",
                        f'--banner:js={_runtime_host_banner(product_skills, routing_catalog["collisionPolicy"])}',
                    ],
                    cwd=pi_root,
                )
                _normalize_bundled_overlay_paths(
                    bundled_entrypoint,
                    overlay_package_root,
                )
            bundled_entrypoint.chmod(0o755)

            # The Runtime Host is the managed Session RPC entrypoint, not the
            # coding-agent CLI.  The bundled subagent Package launches
            # isolated child Sessions through Pi's JSON print mode, so ship a
            # sibling CLI explicitly instead of letting it recursively invoke
            # runtime-host/cli.mjs (which exits 0 with no output).
            bundled_pi_cli = runtime_dir / "pi-cli.mjs"
            _run(
                [
                    str(esbuild),
                    str(pi_root / "packages" / "coding-agent" / "src" / "cli.ts"),
                    "--bundle",
                    "--platform=node",
                    "--format=esm",
                    "--target=node22",
                    f"--outfile={bundled_pi_cli}",
                    "--external:jiti",
                    f'--banner:js={_runtime_host_banner(product_skills, routing_catalog["collisionPolicy"])}',
                ],
                cwd=pi_root,
            )
            bundled_pi_cli.chmod(0o755)
            bundled_pi_theme_dir = runtime_dir / "dist" / "modes" / "interactive" / "theme"
            bundled_pi_theme_dir.mkdir(parents=True, exist_ok=True)
            for theme_file in (pi_root / "packages" / "coding-agent" / "src" / "modes" / "interactive" / "theme").glob("*.json"):
                shutil.copy2(theme_file, bundled_pi_theme_dir / theme_file.name)
            provider_bridge = runtime_dir / "provider-bridge.mjs"
            _run(
                [
                    str(esbuild),
                    str(provider_bridge_source),
                    "--bundle",
                    "--platform=node",
                    "--format=esm",
                    "--target=node22",
                    f"--outfile={provider_bridge}",
                    f"--alias:rag-ime-pi-auth-storage={pi_root / 'packages' / 'coding-agent' / 'src' / 'core' / 'auth-storage.ts'}",
                    f"--alias:rag-ime-pi-model-runtime={pi_root / 'packages' / 'coding-agent' / 'src' / 'core' / 'model-runtime.ts'}",
                    f"--alias:rag-ime-pi-openai-codex-oauth={pi_root / 'packages' / 'ai' / 'src' / 'auth' / 'oauth' / 'openai-codex.ts'}",
                    f"--alias:rag-ime-pi-openai-chatgpt-oauth={pi_root / 'packages' / 'ai' / 'src' / 'auth' / 'oauth' / 'openai-chatgpt.ts'}",
                    '--banner:js=import { createRequire as __createRequire } from "node:module"; const require = __createRequire(import.meta.url);',
                ],
                cwd=pi_root,
            )
            provider_bridge.chmod(0o755)
            licenses_dir = runtime_dir / "licenses"
            licenses_dir.mkdir(exist_ok=True)
            shutil.copy2(ROOT / "licenses" / "jose-LICENSE", licenses_dir / "jose-LICENSE")
            _bundle_oauth_runtime_modules(
                esbuild=esbuild,
                pi_root=pi_root,
                runtime_dir=runtime_dir,
            )
            packaged_node = bin_dir / "node"
            shutil.copy2(node, packaged_node)
            packaged_node.chmod(0o755)
            placeholder = runtime_dir / "extension-placeholder.mjs"
            placeholder.write_text(
                "// Protocol v2 loads product tools and managed plugins through the Runtime Host.\n"
                "export default function managedRuntimeV2Placeholder() {}\n",
                encoding="ascii",
            )
            if args.skip_smoke:
                smoke: dict[str, object] = {}
                oauth_smoke: dict[str, object] = {}
            else:
                oauth_smoke = _smoke_oauth_runtime_modules(packaged_node, runtime_dir)
                smoke = _smoke_runtime(packaged_node, bundled_entrypoint)
            manifest = build_managed_pi_runtime_manifest(
                staging,
                runtime_version=runtime_version,
                pi_version=pi_version,
                launch_kind="node",
                pi_entrypoint="runtime-host/cli.mjs",
                node_entrypoint="bin/node",
                extension_entrypoint="runtime-host/extension-placeholder.mjs",
                tools=CONTROL_TOOL_IDS,
                source_repository=str(session_runtime_contract["sourceRepository"]),
                source_commit=source_commit,
                source_package=str(session_runtime_contract["sourcePackage"]),
                protocol_version="2",
                runtime_methods=tuple(session_runtime_contract["requiredMethods"]),
                source_contract_sha256=session_runtime_contract_sha256,
                handlers_commit=source_commit.split("+", 1)[0],
            )
            manifest["source"] = {
                **dict(manifest["source"]),
                "productRepository": str(ROOT),
                "productCommit": product_commit,
            }
            # The runtime version is content-addressed. Keep its manifest
            # deterministic so reinstalling the same product commit is an
            # idempotent verification instead of a version collision.
            manifest["createdAtMs"] = product_commit_ms
            write_managed_pi_runtime_manifest(staging / MANIFEST_NAME, manifest)
            os.replace(staging, destination)
        finally:
            if staging.exists():
                shutil.rmtree(staging)
    except (KeyError, json.JSONDecodeError, OSError, subprocess.SubprocessError, ManagedPiRuntimeError) as exc:
        print(f"managed Pi runtime build failed: {exc}", file=sys.stderr)
        return 1

    result = {
        "ok": True,
        "runtimeVersion": runtime_version,
        "piVersion": pi_version,
        "protocolVersion": "2",
        "sourceCommit": source_commit,
        "sourceContractSha256": session_runtime_contract_sha256,
        "runtimeMethods": session_runtime_contract["requiredMethods"],
        "productCommit": product_commit,
        "payload": str(destination),
        "manifest": str(destination / MANIFEST_NAME),
        "fileCount": len(manifest["files"]),
        "smoke": smoke.get("result", {}) if smoke else {"skipped": True},
        "oauthSmoke": oauth_smoke if oauth_smoke else {"skipped": True},
    }
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
