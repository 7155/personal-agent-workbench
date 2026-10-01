from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from scripts.build_managed_pi_runtime_v2 import (
    BUNDLED_SKILL_SUPPORT_DIRS,
    MAX_ROUTING_CARD_CHARS,
    PROJECT_ROUTING_SKILLS,
    ROUTING_CARD_FIELDS,
    SKILL_ROUTING_CARDS,
    _OAUTH_RUNTIME_MODULES,
    ROOT,
    REQUIRED_PI_RUNTIME_BASE_COMMIT,
    REQUIRED_RUNTIME_METHODS,
    SESSION_RUNTIME_CONTRACT,
    _SESSION_RUNTIME_SOURCE_KEYS,
    _copy_bundled_pi_packages,
    _bundle_codemode_runtime_assets,
    _hash_extension_app_pi_packages,
    _copy_product_skills,
    _normalize_bundled_overlay_paths,
    _compact_card_length,
    _default_node,
    _product_skill_dirs,
    _prepare_runtime_host_overlay,
    _resolve_skill_source_collisions,
    _runtime_host_banner,
    _source_revision,
    _skill_routing_projection,
    _smoke_oauth_runtime_modules,
    _verify_pi_worktree,
    _validated_skill_routing_catalog,
    _verified_session_runtime_contract,
    main as build_runtime_payload,
)
from rag_ime.managed_pi_runtime import ManagedPiRuntimeError
from rag_ime.agent_extensions import (
    extension_app_binding_capability,
    extension_app_binding_sha256,
)
from rag_ime.pi.protocols import normalize_protocol_version


SESSION_FLOW_SKILLS = {
    "alignment-and-decision",
    "facilitate-room",
    "implementation-planning",
    "independent-review",
    "orchestrate-session",
    "organize-work-documents",
    "systematic-debugging",
    "test-driven-implementation",
}


def _write_extension_app_fixture(
    root: Path,
    *,
    slug: str = "zhanggui-wenshu",
    app_version: str = "0.1.0",
    package_version: str | None = None,
    skill_ref: str = "zhanggui-wenshu",
    suite_id: str = "sgg",
    suite_revision: str = "fixture-v2",
) -> tuple[Path, Path, dict[str, object], dict[str, object]]:
    package_version = package_version or app_version
    app_root = root / "extension-apps" / slug
    package = app_root / "pi-package"
    skill = package / "skills" / skill_ref
    skill.mkdir(parents=True)
    skill_file = skill / "SKILL.md"
    skill_file.write_text(
        f"---\nname: {skill_ref}\ndescription: test\n---\n",
        encoding="utf-8",
    )
    skill_sha256 = hashlib.sha256(skill_file.read_bytes()).hexdigest()
    app_manifest: dict[str, object] = {
        "schemaVersion": "pawos.extension-app.v1",
        "id": f"extension:{slug}",
        "version": app_version,
        "packageId": f"@paw/{slug}",
        "label": "掌柜问数",
        "shortLabel": "问数",
        "tagline": "test",
        "route": f"/extensions/{slug}",
        "presentation": "conversation",
        "accent": "green",
        "icon": {"symbol": "analytics", "background": "#087F68"},
        "skillRef": skill_ref,
        "skillSha256": skill_sha256,
        "verticalSuiteId": suite_id,
        "verticalSuiteRevision": suite_revision,
        "bindingSha256": "0" * 64,
    }
    binding_sha256 = extension_app_binding_sha256(
        app_manifest,
        skill_sha256=skill_sha256,
        package_version=package_version,
    )
    app_manifest["bindingSha256"] = binding_sha256
    binding_capability = extension_app_binding_capability(binding_sha256)
    package_manifest: dict[str, object] = {
        "name": f"@paw/{slug}",
        "version": package_version,
        "displayName": "掌柜问数",
        "pi": {"skills": ["./skills"]},
        "paw": {
            "capabilities": [f"pawos.extension.{slug}", binding_capability],
            "extensionApp": {
                "id": f"extension:{slug}",
                "packageId": f"@paw/{slug}",
                "version": package_version,
                "bindingSha256": binding_sha256,
                "skillRef": skill_ref,
                "skillSha256": skill_sha256,
                "verticalSuiteId": suite_id,
                "verticalSuiteRevision": suite_revision,
                "manifest": app_manifest,
            },
        },
    }
    (app_root / "pawos-app.json").write_text(
        json.dumps(app_manifest, ensure_ascii=False), encoding="utf-8"
    )
    (package / "package.json").write_text(
        json.dumps(package_manifest, ensure_ascii=False), encoding="utf-8"
    )
    (package / "index.ts").write_text("export {};\n", encoding="utf-8")
    return app_root, package, app_manifest, package_manifest


class ManagedPiRuntimeV2BuildTests(unittest.TestCase):
    def _runtime_version_for_source(self, source_commit: str) -> str:
        """Run the real identity calculation and stop before payload writes."""

        class IdentityCaptured(Exception):
            pass

        real_exists = Path.exists

        def capture_destination(path: Path) -> bool:
            if path.parent == ROOT / "build" / "managed-pi-runtime":
                raise IdentityCaptured(path.name)
            return real_exists(path)

        def git_metadata(command: list[str], **_kwargs: object) -> str:
            return "100" if "show" in command else "f" * 40

        with tempfile.TemporaryDirectory(prefix="rag-ime-runtime-identity-") as temporary:
            pi_root = Path(temporary)
            package_json = pi_root / "packages" / "coding-agent" / "package.json"
            package_json.parent.mkdir(parents=True)
            package_json.write_text('{"version":"0.84.2"}', encoding="utf-8")
            esbuild = pi_root / "node_modules" / ".bin" / "esbuild"
            esbuild.parent.mkdir(parents=True)
            esbuild.touch()
            with (
                patch("scripts.build_managed_pi_runtime_v2._runtime_host_root", return_value=pi_root),
                patch("scripts.build_managed_pi_runtime_v2._source_revision", return_value=source_commit),
                patch("scripts.build_managed_pi_runtime_v2._node_relocation_error", return_value=None),
                patch("scripts.build_managed_pi_runtime_v2._verified_session_runtime_contract", return_value=({}, "contract-fixture")),
                patch("scripts.build_managed_pi_runtime_v2._validated_skill_routing_catalog", return_value={}),
                patch("scripts.build_managed_pi_runtime_v2._hash_tree", return_value=b"skills-fixture"),
                patch("scripts.build_managed_pi_runtime_v2._hash_extension_app_pi_packages", return_value=b"apps-fixture"),
                patch("scripts.build_managed_pi_runtime_v2._run", side_effect=git_metadata),
                patch.object(Path, "exists", capture_destination),
            ):
                with self.assertRaises(IdentityCaptured) as captured:
                    build_runtime_payload(["--pi-worktree", str(pi_root), "--node", sys.executable])
                return str(captured.exception)

    def test_runtime_version_distinguishes_dirty_sources_at_the_same_commit(self) -> None:
        commit = "9c3f93c8b1c409e82e14d458510c146088c44561"
        first = self._runtime_version_for_source(f"{commit}+dirty.{'1' * 64}")
        second = self._runtime_version_for_source(f"{commit}+dirty.{'2' * 64}")

        self.assertNotEqual(first, second)

    def test_runtime_version_is_deterministic_for_the_same_complete_source(self) -> None:
        source = f"9c3f93c8b1c409e82e14d458510c146088c44561+dirty.{'1' * 64}"

        self.assertEqual(self._runtime_version_for_source(source), self._runtime_version_for_source(source))

    def test_runtime_version_distinguishes_clean_and_dirty_source(self) -> None:
        commit = "9c3f93c8b1c409e82e14d458510c146088c44561"

        self.assertNotEqual(
            self._runtime_version_for_source(commit),
            self._runtime_version_for_source(f"{commit}+dirty.{'1' * 64}"),
        )

    def test_control_extension_keeps_full_desktop_receipt_out_of_model_context(self) -> None:
        source = (ROOT / "integrations" / "pi" / "rag-ime-control.ts").read_text(
            encoding="utf-8"
        )

        self.assertIn('key !== "auditReceipt"', source)
        self.assertIn("boundedToolResult(toolCallId", source)


    def test_candidate_skill_host_path_guard_executes_without_runtime_or_provider(self) -> None:
        from scripts.build_managed_pi_runtime_v2 import _CANDIDATE_SKILL_PATHS_SOURCE

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace, outside = root / "workspace", root / "outside"
            skill = workspace / "skills" / "candidate"
            skill.mkdir(parents=True)
            outside.mkdir()
            (skill / "SKILL.md").write_text("candidate")
            (outside / "SKILL.md").write_text("outside")
            (workspace / "linked").symlink_to(outside, target_is_directory=True)
            program = root / "guard.ts"
            program.write_text('import { realpath, stat } from "node:fs/promises";\nimport { join, relative, isAbsolute as pathIsAbsolute } from "node:path";\nclass RuntimeProtocolError extends Error { constructor(code:string, message:string) {super(message);} }\nfunction isInside(root:string, target:string) {const p=relative(root,target);return !p.startsWith("..") && !pathIsAbsolute(p);}\n' + _CANDIDATE_SKILL_PATHS_SOURCE + '\nconst root=process.argv[2];\nconst base={piSkillsEnabled:true,noContextFiles:true,skillAllowlist:["candidate"]};\nconst allowed=await optionalCandidateSkillPaths({...base,candidateSkillPaths:[join(root,"skills","candidate")]},root);\nif(allowed?.length!==1)throw new Error("valid candidate rejected");\nfor(const params of [{...base,candidateSkillPaths:[join(root,"..","outside")]},{...base,candidateSkillPaths:[join(root,"linked")]},{...base,noContextFiles:false,candidateSkillPaths:[join(root,"skills","candidate")]}]) {let rejected=false;try{await optionalCandidateSkillPaths(params,root);}catch{rejected=true;}if(!rejected)throw new Error("unsafe candidate accepted");}\nconsole.log("guard passed");\n')
            completed = subprocess.run(["node", "--experimental-strip-types", str(program), str(workspace)], capture_output=True, text=True, timeout=20)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn("guard passed", completed.stdout)

    def test_runtime_host_overlay_adds_guarded_session_skill_allowlist(self) -> None:
        from scripts.build_managed_pi_runtime_v2 import (
            _RUNTIME_HOST_SOURCE_OVERLAYS,
        )

        with tempfile.TemporaryDirectory(
            prefix="rag-ime-runtime-host-overlay-test-"
        ) as temporary:
            pi_root = Path(temporary) / "pi"
            package_root = pi_root / "integrations" / "rag-ime-runtime-host"
            (pi_root / "node_modules").mkdir(parents=True)
            for relative_path, replacements in (
                _RUNTIME_HOST_SOURCE_OVERLAYS.items()
            ):
                source = package_root / relative_path
                source.parent.mkdir(parents=True, exist_ok=True)
                source.write_text(
                    "\n\n".join(before for before, _after in replacements),
                    encoding="utf-8",
                )

            overlay = _prepare_runtime_host_overlay(
                package_root,
                Path(temporary) / "overlay",
                pi_root=pi_root,
            )

            self.assertTrue((overlay / "node_modules").is_symlink())
            session_source = (overlay / "src" / "pi-session.ts").read_text(
                encoding="utf-8"
            )
            host_source = (overlay / "src" / "runtime-host.ts").read_text(
                encoding="utf-8"
            )
            tool_bridge_source = (overlay / "src" / "tool-bridge.ts").read_text(
                encoding="utf-8"
            )
            self.assertIn("skillAllowlist?: string[];", session_source)
            self.assertIn("candidateSkillPaths?: string[];", session_source)
            self.assertIn("options.candidateSkillPaths ?? [", session_source)
            self.assertIn("sessionCandidateSkillPaths: true", host_source)
            self.assertIn("await optionalCandidateSkillPaths(params, cwd)", host_source)
            self.assertIn("allowedSkillNames.has(skill.name)", session_source)
            self.assertNotIn(
                "|| skillPromptFocus.includes(skill.name)",
                session_source,
            )
            self.assertIn("function optionalSkillAllowlist(", host_source)
            self.assertIn("sessionSkillAllowlist: true", host_source)
            self.assertIn(
                "skillAllowlist: optionalSkillAllowlist(params)",
                host_source,
            )
            self.assertIn(
                'toolName !== "room_partner" && toolName !== "agents"',
                tool_bridge_source,
            )
            self.assertIn(
                "DELEGATION_GATEWAY_REQUEST_TIMEOUT_MS",
                tool_bridge_source,
            )
            self.assertNotIn(
                "ROOM_DELEGATION_GATEWAY_REQUEST_TIMEOUT_MS",
                tool_bridge_source,
            )

            (package_root / "src" / "pi-session.ts").write_text(
                "// upstream source drifted\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                ManagedPiRuntimeError,
                "overlay anchor mismatch",
            ):
                _prepare_runtime_host_overlay(
                    package_root,
                    Path(temporary) / "drifted-overlay",
                    pi_root=pi_root,
                )

    def test_bundled_overlay_paths_are_normalized_deterministically(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="rag-ime-runtime-host-bundle-path-"
        ) as temporary:
            root = Path(temporary)
            outputs: list[bytes] = []
            for suffix in ("first", "second"):
                overlay = root / suffix / "runtime-host"
                overlay.mkdir(parents=True)
                bundle = root / f"{suffix}.mjs"
                bundle.write_bytes(
                    b"const source = "
                    + json.dumps(
                        str(overlay / "src" / "upstream-compat.ts")
                    ).encode("utf-8")
                    + b";\n"
                )

                replacements = _normalize_bundled_overlay_paths(
                    bundle,
                    overlay,
                )

                self.assertEqual(replacements, 1)
                outputs.append(bundle.read_bytes())

            self.assertEqual(outputs[0], outputs[1])
            self.assertIn(
                b"/rag-ime-managed/runtime-host/src/upstream-compat.ts",
                outputs[0],
            )

    def test_session_runtime_source_contract_pins_required_session_surfaces(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(
            prefix="rag-ime-session-host-contract-"
        ) as temporary:
            root = Path(temporary)
            relative_sources = {
                "protocol": Path("integrations/rag-ime-runtime-host/src/protocol.ts"),
                "runtimeHost": Path("integrations/rag-ime-runtime-host/src/runtime-host.ts"),
                "contextInspection": Path("integrations/rag-ime-runtime-host/src/debug-context.ts"),
                "toolBridge": Path("integrations/rag-ime-runtime-host/src/tool-bridge.ts"),
                "toolResults": Path("integrations/rag-ime-runtime-host/src/tool-artifact-buffer.ts"),
                "session": Path("integrations/rag-ime-runtime-host/src/pi-session.ts"),
                "pluginManager": Path("integrations/rag-ime-runtime-host/src/plugin-manager.ts"),
                "packageCatalog": Path("integrations/rag-ime-runtime-host/src/bundled-package-catalog.ts"),
                "packageManager": Path("integrations/rag-ime-runtime-host/src/native-package-manager.ts"),
            }
            self.assertEqual(
                set(relative_sources),
                set(_SESSION_RUNTIME_SOURCE_KEYS),
            )
            methods = list(REQUIRED_RUNTIME_METHODS)
            markers = {
                key: [f"marker:{key}"]
                for key in _SESSION_RUNTIME_SOURCE_KEYS
            }
            for key, relative in relative_sources.items():
                source = root / relative
                source.parent.mkdir(parents=True, exist_ok=True)
                extra = ""
                if key == "protocol":
                    extra = (
                        "export type RuntimeMethod = "
                        + " ".join(f'| "{method}"' for method in methods)
                        + ";\n"
                    )
                elif key == "runtimeHost":
                    extra = (
                        "switch (method) { "
                        + " ".join(
                            f'case "{method}": break;'
                            for method in methods
                        )
                        + " }\n"
                    )
                source.write_text(
                    f"// marker:{key}\n{extra}",
                    encoding="utf-8",
                )
            contract_path = root / "session-runtime-host-contract.json"
            contract_path.write_text(
                json.dumps(
                    {
                        "schemaVersion": (
                            "rag-ime.pi-session-runtime-host-contract.v1"
                        ),
                        "sourceRepository": (
                            "https://github.com/7155/pi.git"
                        ),
                        "sourcePackage": (
                            "@earendil-works/pi-rag-ime-runtime-host"
                        ),
                        "protocolVersion": "2",
                        "minimumHandlersCommit": "a" * 40,
                        "requiredMethods": methods,
                        "handlerSources": {
                            key: relative.as_posix()
                            for key, relative in relative_sources.items()
                        },
                        "requiredSourceMarkers": markers,
                    },
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
            with (
                patch(
                    "scripts.build_managed_pi_runtime_v2."
                    "SESSION_RUNTIME_CONTRACT",
                    contract_path,
                ),
                patch(
                    "scripts.build_managed_pi_runtime_v2."
                    "REQUIRED_PI_RUNTIME_BASE_COMMIT",
                    "a" * 40,
                ),
            ):
                contract, digest = (
                    _verified_session_runtime_contract(root)
                )
                (root / relative_sources["session"]).write_text(
                    "// marker removed\n",
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(
                    RuntimeError,
                    "source marker is missing: session",
                ):
                    _verified_session_runtime_contract(root)

        self.assertEqual(
            contract["minimumHandlersCommit"],
            "a" * 40,
        )
        self.assertEqual(len(digest), 64)

    def test_public_pi_pin_uses_session_runtime_contract(self) -> None:
        contract = json.loads(
            SESSION_RUNTIME_CONTRACT.read_text(encoding="utf-8")
        )
        self.assertEqual(
            contract["minimumHandlersCommit"],
            REQUIRED_PI_RUNTIME_BASE_COMMIT,
        )
        self.assertEqual(
            contract["requiredMethods"],
            list(REQUIRED_RUNTIME_METHODS),
        )
        serialized = json.dumps(contract, sort_keys=True)
        self.assertIn("room.dispatch", contract["requiredMethods"])
        self.assertIn("room.cancel", contract["requiredMethods"])
        self.assertIn("session.await_settled", contract["requiredMethods"])
        self.assertIn("plugins.create", contract["requiredMethods"])
        self.assertIn("plugins.validate", contract["requiredMethods"])
        self.assertIn("session.command.invoke", serialized)
        self.assertIn("plugins.catalog", serialized)
        self.assertIn("plugins.package.prepare", serialized)
        self.assertIn("plugins.uninstall", serialized)

    def test_session_contract_accepts_provider_output_budget_parameter(self) -> None:
        contract = json.loads(
            SESSION_RUNTIME_CONTRACT.read_text(encoding="utf-8")
        )

        session_markers = contract["requiredSourceMarkers"]["session"]
        self.assertIn(
            "async setModel(provider: string, modelId: string, maxTokens?: number)",
            session_markers,
        )
        self.assertNotIn(
            "async setModel(provider: string, modelId: string)",
            session_markers,
        )
        self.assertEqual(
            contract["handlerSources"],
            {
                "protocol": "integrations/rag-ime-runtime-host/src/protocol.ts",
                "runtimeHost": "integrations/rag-ime-runtime-host/src/runtime-host.ts",
                "contextInspection": "integrations/rag-ime-runtime-host/src/debug-context.ts",
                "toolBridge": "integrations/rag-ime-runtime-host/src/tool-bridge.ts",
                "toolResults": "integrations/rag-ime-runtime-host/src/tool-artifact-buffer.ts",
                "session": "integrations/rag-ime-runtime-host/src/pi-session.ts",
                "pluginManager": "integrations/rag-ime-runtime-host/src/plugin-manager.ts",
                "packageCatalog": "integrations/rag-ime-runtime-host/src/bundled-package-catalog.ts",
                "packageManager": "integrations/rag-ime-runtime-host/src/native-package-manager.ts",
            },
        )

    def test_public_pi_pin_matches_reviewed_runtime_protocol_order(self) -> None:
        self.assertEqual(
            list(REQUIRED_RUNTIME_METHODS),
            [
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
                "session.codemode.set",
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
            ],
        )
        self.assertEqual(
            REQUIRED_PI_RUNTIME_BASE_COMMIT,
            "9c3f93c8b1c409e82e14d458510c146088c44561",
        )

    def test_builder_requires_an_explicit_pi_worktree(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "build_managed_pi_runtime_v2.py"),
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn("--pi-worktree", result.stderr)
        self.assertIn("required", result.stderr)

    def test_builder_rejects_dirty_pi_worktree(self) -> None:
        with tempfile.TemporaryDirectory(prefix="rag-ime-dirty-pi-") as temporary:
            root = Path(temporary)
            source = root / "integrations" / "rag-ime-runtime-host" / "src"
            source.mkdir(parents=True)
            source_file = source / "runtime-host.ts"
            source_file.write_text("export const runtime = true;\n", encoding="utf-8")
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            subprocess.run(
                ["git", "config", "user.email", "test@example.com"],
                cwd=root,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "Pi Runtime Test"],
                cwd=root,
                check=True,
            )
            subprocess.run(["git", "add", "."], cwd=root, check=True)
            subprocess.run(
                ["git", "commit", "-qm", "initial runtime host"],
                cwd=root,
                check=True,
            )
            subprocess.run(["git", "checkout", "--detach", "-q"], cwd=root, check=True)
            source_file.write_text("export const runtime = false;\n", encoding="utf-8")

            with self.assertRaisesRegex(ManagedPiRuntimeError, "clean"):
                _source_revision(root)

    def test_development_install_identifies_dirty_pi_source_without_blocking_it(self) -> None:
        with tempfile.TemporaryDirectory(prefix="rag-ime-dirty-dev-pi-") as temporary:
            root = Path(temporary)
            source = root / "integrations" / "rag-ime-runtime-host" / "src"
            source.mkdir(parents=True)
            source_file = source / "runtime-host.ts"
            source_file.write_text("export const runtime = true;\n", encoding="utf-8")
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            subprocess.run(
                ["git", "config", "user.email", "test@example.com"],
                cwd=root,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "Pi Runtime Test"],
                cwd=root,
                check=True,
            )
            subprocess.run(["git", "add", "."], cwd=root, check=True)
            subprocess.run(
                ["git", "commit", "-qm", "initial runtime host"],
                cwd=root,
                check=True,
            )
            subprocess.run(["git", "checkout", "--detach", "-q"], cwd=root, check=True)
            commit = subprocess.check_output(
                ["git", "rev-parse", "HEAD"],
                cwd=root,
                text=True,
            ).strip()
            source_file.write_text("export const runtime = false;\n", encoding="utf-8")

            with (
                patch(
                    "scripts.build_managed_pi_runtime_v2.REQUIRED_PI_RUNTIME_BASE_COMMIT",
                    commit,
                ),
                patch.dict(os.environ, {"RAG_IME_ALLOW_DIRTY_INSTALL": "1"}),
            ):
                first_revision = _source_revision(root)
                source_file.write_text("export const runtime = 'changed';\n", encoding="utf-8")
                second_revision = _source_revision(root)

            self.assertRegex(first_revision, rf"^{commit}\+dirty\.[0-9a-f]{{64}}$")
            self.assertNotEqual(first_revision, second_revision)


    def test_builder_requires_the_provider_safe_pi_commit_without_an_evaluation_escape_hatch(self) -> None:
        self.assertEqual(
            "9c3f93c8b1c409e82e14d458510c146088c44561",
            REQUIRED_PI_RUNTIME_BASE_COMMIT,
        )
        builder_source = (
            ROOT / "scripts" / "build_managed_pi_runtime_v2.py"
        ).read_text(encoding="utf-8")
        self.assertNotIn("--evaluation-provider-compat", builder_source)
        self.assertNotIn("evaluationProviderCompatSha256", builder_source)

    def test_builder_requires_reviewed_runtime_ancestry(self) -> None:
        with tempfile.TemporaryDirectory(prefix="rag-ime-unrelated-pi-") as temporary:
            root = Path(temporary)
            source = root / "integrations" / "rag-ime-runtime-host"
            source.mkdir(parents=True)
            (source / "README.md").write_text("runtime host\n", encoding="utf-8")
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            subprocess.run(
                ["git", "config", "user.email", "test@example.com"],
                cwd=root,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "Pi Runtime Test"],
                cwd=root,
                check=True,
            )
            subprocess.run(["git", "add", "."], cwd=root, check=True)
            subprocess.run(
                ["git", "commit", "-qm", "unrelated runtime host"],
                cwd=root,
                check=True,
            )
            subprocess.run(["git", "checkout", "--detach", "-q"], cwd=root, check=True)

            with self.assertRaisesRegex(ManagedPiRuntimeError, "ancestry"):
                _verify_pi_worktree(root)

    def test_builder_rejects_an_attached_pi_worktree(self) -> None:
        with tempfile.TemporaryDirectory(prefix="rag-ime-attached-pi-") as temporary:
            root = Path(temporary)
            source = root / "integrations" / "rag-ime-runtime-host"
            source.mkdir(parents=True)
            (source / "README.md").write_text("runtime host\n", encoding="utf-8")
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            subprocess.run(
                ["git", "config", "user.email", "test@example.com"],
                cwd=root,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "Pi Runtime Test"],
                cwd=root,
                check=True,
            )
            subprocess.run(["git", "add", "."], cwd=root, check=True)
            subprocess.run(
                ["git", "commit", "-qm", "attached runtime host"],
                cwd=root,
                check=True,
            )
            commit = subprocess.check_output(
                ["git", "rev-parse", "HEAD"],
                cwd=root,
                text=True,
            ).strip()

            with patch(
                "scripts.build_managed_pi_runtime_v2.REQUIRED_PI_RUNTIME_BASE_COMMIT",
                commit,
            ):
                with self.assertRaisesRegex(ManagedPiRuntimeError, "detached HEAD"):
                    _verify_pi_worktree(root)

    def test_unknown_protocol_does_not_silently_select_v1(self) -> None:
        with self.assertRaisesRegex(ValueError, "unsupported Pi runtime protocol"):
            normalize_protocol_version("3")

    def test_managed_runtime_rejects_a_legacy_only_pi_worktree(self) -> None:
        with tempfile.TemporaryDirectory(prefix="rag-ime-legacy-pi-") as temporary:
            root = Path(temporary)
            (root / "packages" / "rag-ime-runtime-host").mkdir(parents=True)

            result = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "build_managed_pi_runtime_v2.py"),
                    "--pi-worktree",
                    str(root),
                    "--node",
                    str(root / "missing-node"),
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
            )

        self.assertEqual(result.returncode, 1)
        self.assertIn("unsupported legacy Pi Runtime Host source", result.stderr)
        self.assertIn("integrations/rag-ime-runtime-host", result.stderr)

    def test_session_runtime_contract_rejects_legacy_handler_source_paths(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(
            prefix="rag-ime-session-host-legacy-contract-"
        ) as temporary:
            root = Path(temporary)
            relative_sources = {
                key: Path(
                    "integrations/rag-ime-runtime-host"
                    f"/src/{key.replace('runtimeHost', 'runtime-host').replace('contextInspection', 'debug-context').replace('toolBridge', 'tool-bridge').replace('toolResults', 'tool-artifact-buffer').replace('pluginManager', 'plugin-manager').replace('packageCatalog', 'bundled-package-catalog').replace('packageManager', 'native-package-manager')}.ts"
                )
                for key in _SESSION_RUNTIME_SOURCE_KEYS
            }
            for key, relative in relative_sources.items():
                source = root / relative
                source.parent.mkdir(parents=True, exist_ok=True)
                source.write_text(
                    f"// marker:{key}\n"
                    + (
                        "export type RuntimeMethod = "
                        + " ".join(f'| "{method}"' for method in REQUIRED_RUNTIME_METHODS)
                        + ";\n"
                        if key == "protocol"
                        else ""
                    )
                    + (
                        "switch (method) { "
                        + " ".join(
                            f'case "{method}": break;'
                            for method in REQUIRED_RUNTIME_METHODS
                        )
                        + " }\n"
                        if key == "runtimeHost"
                        else ""
                    ),
                    encoding="utf-8",
                )
            contract = json.loads(
                SESSION_RUNTIME_CONTRACT.read_text(encoding="utf-8")
            )
            contract["minimumHandlersCommit"] = "a" * 40
            contract["handlerSources"] = {
                key: relative.as_posix()
                for key, relative in relative_sources.items()
            }
            contract_path = root / "session-runtime-host-contract.json"
            contract_path.write_text(
                json.dumps(contract, sort_keys=True),
                encoding="utf-8",
            )
            contract["handlerSources"]["session"] = (
                "packages/rag-ime-runtime-host/src/pi-session.ts"
            )
            contract_path.write_text(
                json.dumps(contract, sort_keys=True),
                encoding="utf-8",
            )
            with (
                patch(
                    "scripts.build_managed_pi_runtime_v2.SESSION_RUNTIME_CONTRACT",
                    contract_path,
                ),
                patch(
                    "scripts.build_managed_pi_runtime_v2.REQUIRED_PI_RUNTIME_BASE_COMMIT",
                    "a" * 40,
                ),
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "must be under integrations/rag-ime-runtime-host",
                ):
                    _verified_session_runtime_contract(root)

    def test_codemode_payload_preserves_native_worker_and_wasm(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "packages/coding-agent/src/extensions/codemode/worker.ts"
            source.parent.mkdir(parents=True)
            source.write_text("native worker source")
            package = root / "node_modules/quickjs-wasi"
            package.mkdir(parents=True)
            (package / "quickjs.wasm").write_bytes(b"native-wasm-fixture")
            runtime = root / "payload/runtime-host"
            runtime.mkdir(parents=True)
            with patch("scripts.build_managed_pi_runtime_v2._run") as run:
                self.assertTrue(_bundle_codemode_runtime_assets(
                    esbuild=root / "esbuild", pi_root=root, runtime_dir=runtime))
            self.assertEqual((runtime / "node_modules/quickjs-wasi/quickjs.wasm").read_bytes(), b"native-wasm-fixture")
            command = run.call_args.args[0]
            self.assertIn(str(source), command)
            self.assertIn(f"--outfile={runtime / 'codemode-worker.js'}", command)
            self.assertEqual(json.loads((runtime / "package.json").read_text()), {"type": "module"})
            (package / "quickjs.wasm").unlink()
            with self.assertRaises(ManagedPiRuntimeError):
                _bundle_codemode_runtime_assets(esbuild=root / "esbuild", pi_root=root, runtime_dir=root / "missing")

    def test_oauth_runtime_smoke_loads_every_lazy_module_and_derives_codex_auth(
        self,
    ) -> None:
        node = Path(_default_node())
        if not node.is_file():
            self.skipTest("Node runtime is unavailable")
        with tempfile.TemporaryDirectory(prefix="rag-ime-oauth-runtime-") as temporary:
            runtime_dir = Path(temporary)
            for output_name, (_source, export_name) in _OAUTH_RUNTIME_MODULES.items():
                if export_name == "openaiCodexOAuth":
                    body = (
                        "export const openaiCodexOAuth = {"
                        "async toAuth(credential) { return {apiKey: credential.access}; }"
                        "};\n"
                    )
                elif export_name == "createRadiusOAuth":
                    body = "export function createRadiusOAuth() { return {}; }\n"
                else:
                    body = f"export const {export_name} = {{}};\n"
                (runtime_dir / output_name).write_text(body, encoding="utf-8")
                (runtime_dir / Path(output_name).with_suffix(".js")).write_text(body, encoding="utf-8")

            result = _smoke_oauth_runtime_modules(node, runtime_dir)
            # The installed SDK loads this .js name. A .ts-only smoke used to
            # pass even though real Codex OAuth could not resolve its module.
            (runtime_dir / "openai-codex.js").unlink()
            with self.assertRaises(subprocess.CalledProcessError):
                _smoke_oauth_runtime_modules(node, runtime_dir)

        self.assertEqual(
            result,
            {"ok": True, "moduleCount": len(_OAUTH_RUNTIME_MODULES) * 2},
        )
        self.assertEqual(
            set(_OAUTH_RUNTIME_MODULES),
            {
                "anthropic.ts",
                "github-copilot.ts",
                "openai-codex.ts",
                "openrouter.ts",
                "kimi-coding.ts",
                "xai.ts",
                "radius.ts",
            },
        )

    def test_payload_version_and_manifest_are_bound_to_the_product_commit(self) -> None:
        script = (ROOT / "scripts" / "build_managed_pi_runtime_v2.py").read_text(
            encoding="utf-8"
        )
        contract = SESSION_RUNTIME_CONTRACT.read_text(encoding="utf-8")

        self.assertIn('product_commit.encode("ascii")', script)
        self.assertIn('"productCommit": product_commit', script)
        self.assertIn('manifest["createdAtMs"] = product_commit_ms', script)
        self.assertIn("_verified_session_runtime_contract", script)
        self.assertIn("_copy_bundled_pi_packages", script)
        self.assertIn('package_root / "pi-packages"', script)
        self.assertIn('bundled_pi_cli = runtime_dir / "pi-cli.mjs"', script)
        self.assertIn('packages" / "coding-agent" / "src" / "cli.ts"', script)
        self.assertIn('bundled_pi_theme_dir = runtime_dir / "dist" / "modes" / "interactive" / "theme"', script)
        self.assertIn(
            "source_contract_sha256=session_runtime_contract_sha256",
            script,
        )
        for method in REQUIRED_RUNTIME_METHODS:
            self.assertIn(f'"{method}"', contract)

    def test_bundled_pi_packages_are_copied_next_to_the_runtime_host(self) -> None:
        with tempfile.TemporaryDirectory(prefix="rag-ime-pi-packages-") as temporary:
            root = Path(temporary)
            source = root / "pi-packages"
            package = source / "example"
            package.mkdir(parents=True)
            (source / "catalog.json").write_text(
                '{"schemaVersion":1,"packages":[]}',
                encoding="utf-8",
            )
            (package / "package.json").write_text(
                '{"name":"@paw/example","version":"1.0.0"}',
                encoding="utf-8",
            )
            (package / "index.ts").write_text("export {};\n", encoding="utf-8")
            destination = root / "payload" / "pi-packages"

            _copy_bundled_pi_packages(
                source,
                destination,
                extension_apps_root=root / "no-extension-apps",
            )

            self.assertEqual(
                (destination / "catalog.json").read_text(encoding="utf-8"),
                '{"schemaVersion":1,"packages":[]}',
            )
            self.assertTrue((destination / "example" / "package.json").is_file())
            self.assertTrue((destination / "example" / "index.ts").is_file())

    def test_blocking_subagent_package_is_removed_from_bundled_catalog(self) -> None:
        with tempfile.TemporaryDirectory(prefix="rag-ime-pi-packages-policy-") as temporary:
            root = Path(temporary)
            source = root / "pi-packages"
            (source / "session-workflow").mkdir(parents=True)
            (source / "subagent").mkdir()
            (source / "catalog.json").write_text(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "packages": [
                            {"directory": "session-workflow"},
                            {"directory": "subagent"},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            (source / "session-workflow" / "package.json").write_text(
                '{"name":"@paw/pi-session-workflow"}', encoding="utf-8"
            )
            (source / "subagent" / "package.json").write_text(
                '{"name":"@paw/pi-subagent"}', encoding="utf-8"
            )
            destination = root / "payload" / "pi-packages"

            _copy_bundled_pi_packages(
                source,
                destination,
                extension_apps_root=root / "no-extension-apps",
            )

            catalog = json.loads((destination / "catalog.json").read_text(encoding="utf-8"))
            self.assertEqual(
                [item["directory"] for item in catalog["packages"]],
                ["session-workflow"],
            )
            self.assertTrue((destination / "session-workflow").is_dir())
            self.assertFalse((destination / "subagent").exists())

    def test_extension_app_pi_packages_are_copied_and_added_to_catalog(self) -> None:
        with tempfile.TemporaryDirectory(prefix="rag-ime-extension-pi-package-") as temporary:
            root = Path(temporary)
            source = root / "pi-packages"
            source.mkdir(parents=True)
            (source / "catalog.json").write_text(
                json.dumps({"schemaVersion": 1, "packages": []}),
                encoding="utf-8",
            )
            app_root = root / "extension-apps"
            package = app_root / "zhanggui-wenshu" / "pi-package"
            (package / "skills" / "zhanggui-wenshu").mkdir(parents=True)
            (package / "package.json").write_text(
                json.dumps(
                    {
                        "name": "@paw/zhanggui-wenshu",
                        "version": "0.1.0",
                        "displayName": "掌柜问数",
                        "pi": {"skills": ["./skills"]},
                    }
                ),
                encoding="utf-8",
            )
            (package / "skills" / "zhanggui-wenshu" / "SKILL.md").write_text(
                "---\nname: zhanggui-wenshu\ndescription: test\n---\n",
                encoding="utf-8",
            )
            destination = root / "payload" / "pi-packages"

            _copy_bundled_pi_packages(
                source,
                destination,
                extension_apps_root=app_root,
            )

            catalog = json.loads(
                (destination / "catalog.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                catalog["packages"],
                [
                    {
                        "directory": "zhanggui-wenshu",
                        "displayName": "掌柜问数",
                    }
                ],
            )
            self.assertEqual(
                (destination / "zhanggui-wenshu" / "package.json").read_text(
                    encoding="utf-8"
                ),
                (package / "package.json").read_text(encoding="utf-8"),
            )
            self.assertTrue(
                (destination / "zhanggui-wenshu" / "skills" / "zhanggui-wenshu" / "SKILL.md").is_file()
            )

    def test_extension_app_binding_is_written_to_staged_package_and_catalog(self) -> None:
        with tempfile.TemporaryDirectory(prefix="rag-ime-extension-binding-copy-") as temporary:
            root = Path(temporary)
            source = root / "pi-packages"
            source.mkdir(parents=True)
            (source / "catalog.json").write_text(
                json.dumps({"schemaVersion": 1, "packages": []}),
                encoding="utf-8",
            )
            _app_root, _package, app_manifest, _package_manifest = _write_extension_app_fixture(root)
            destination = root / "payload" / "pi-packages"

            _copy_bundled_pi_packages(
                source,
                destination,
                extension_apps_root=root / "extension-apps",
            )

            staged_package = json.loads(
                (destination / "zhanggui-wenshu" / "package.json").read_text(
                    encoding="utf-8"
                )
            )
            expected_capability = (
                "pawos.extension.binding."
                + str(app_manifest["bindingSha256"])[:40]
            )
            self.assertIn(
                expected_capability,
                staged_package["paw"]["capabilities"],
            )
            catalog = json.loads(
                (destination / "catalog.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                catalog["packages"][0]["extensionApp"]["bindingSha256"],
                app_manifest["bindingSha256"],
            )

    def test_extension_app_pi_package_rejects_duplicate_directory_and_name(self) -> None:
        with tempfile.TemporaryDirectory(prefix="rag-ime-extension-pi-collision-") as temporary:
            root = Path(temporary)
            source = root / "pi-packages"
            existing = source / "existing"
            existing.mkdir(parents=True)
            (source / "catalog.json").write_text(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "packages": [{"directory": "existing", "displayName": "Existing"}],
                    }
                ),
                encoding="utf-8",
            )
            (existing / "package.json").write_text(
                json.dumps(
                    {
                        "name": "@paw/existing",
                        "version": "1.0.0",
                    }
                ),
                encoding="utf-8",
            )
            (existing / "skills").mkdir()
            app_root = root / "extension-apps"
            duplicate_directory = app_root / "existing" / "pi-package"
            (duplicate_directory / "skills").mkdir(parents=True)
            (duplicate_directory / "package.json").write_text(
                json.dumps(
                    {
                        "name": "@paw/other",
                        "version": "1.0.0",
                        "pi": {"skills": ["./skills"]},
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ManagedPiRuntimeError, "directory"):
                _copy_bundled_pi_packages(
                    source,
                    root / "directory-payload" / "pi-packages",
                    extension_apps_root=app_root,
                )

            name_app_root = root / "name-extension-apps"
            first = name_app_root / "first" / "pi-package"
            second = name_app_root / "second" / "pi-package"
            for package in (first, second):
                (package / "skills").mkdir(parents=True)
                (package / "package.json").write_text(
                    json.dumps(
                        {
                            "name": "@paw/duplicate",
                            "version": "1.0.0",
                            "pi": {"skills": ["./skills"]},
                        }
                    ),
                    encoding="utf-8",
                )
            with self.assertRaisesRegex(ManagedPiRuntimeError, "name"):
                _copy_bundled_pi_packages(
                    source,
                    root / "name-payload" / "pi-packages",
                    extension_apps_root=name_app_root,
                )

    def test_extension_app_pi_package_rejects_package_and_package_json_symlinks(self) -> None:
        with tempfile.TemporaryDirectory(prefix="rag-ime-extension-pi-symlink-") as temporary:
            root = Path(temporary)
            source = root / "pi-packages"
            source.mkdir(parents=True)
            (source / "catalog.json").write_text(
                json.dumps({"schemaVersion": 1, "packages": []}),
                encoding="utf-8",
            )
            app_root = root / "extension-apps"
            real_package = root / "real-package"
            (real_package / "skills").mkdir(parents=True)
            (real_package / "package.json").write_text(
                json.dumps(
                    {
                        "name": "@paw/safe",
                        "version": "1.0.0",
                        "pi": {"skills": ["./skills"]},
                    }
                ),
                encoding="utf-8",
            )
            symlinked_directory = app_root / "directory-link" / "pi-package"
            symlinked_directory.parent.mkdir(parents=True)
            symlinked_directory.symlink_to(real_package, target_is_directory=True)
            with self.assertRaisesRegex(ManagedPiRuntimeError, "symlink"):
                _copy_bundled_pi_packages(
                    source,
                    root / "directory-payload" / "pi-packages",
                    extension_apps_root=app_root,
                )

            app_root = root / "json-link-apps"
            package = app_root / "json-link" / "pi-package"
            package.mkdir(parents=True)
            package_json_target = root / "package.json.target"
            package_json_target.write_text(
                json.dumps(
                    {
                        "name": "@paw/json-link",
                        "version": "1.0.0",
                        "pi": {"skills": ["./skills"]},
                    }
                ),
                encoding="utf-8",
            )
            (package / "package.json").symlink_to(package_json_target)
            with self.assertRaisesRegex(ManagedPiRuntimeError, "symlink"):
                _copy_bundled_pi_packages(
                    source,
                    root / "json-payload" / "pi-packages",
                    extension_apps_root=app_root,
                )

    def test_extension_app_pi_package_rejects_invalid_manifest(self) -> None:
        with tempfile.TemporaryDirectory(prefix="rag-ime-extension-pi-manifest-") as temporary:
            root = Path(temporary)
            source = root / "pi-packages"
            source.mkdir(parents=True)
            (source / "catalog.json").write_text(
                json.dumps({"schemaVersion": 1, "packages": []}),
                encoding="utf-8",
            )
            package = root / "extension-apps" / "invalid" / "pi-package"
            package.mkdir(parents=True)
            (package / "package.json").write_text(
                json.dumps(
                    {
                        "name": "@paw/invalid",
                        "version": "1.0.0",
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ManagedPiRuntimeError, "pi"):
                _copy_bundled_pi_packages(
                    source,
                    root / "payload" / "pi-packages",
                    extension_apps_root=root / "extension-apps",
                )

    def test_extension_app_pi_package_tree_changes_packager_digest(self) -> None:
        with tempfile.TemporaryDirectory(prefix="rag-ime-extension-pi-digest-") as temporary:
            root = Path(temporary)
            package = root / "extension-apps" / "digest" / "pi-package"
            (package / "skills").mkdir(parents=True)
            (package / "package.json").write_text(
                json.dumps(
                    {
                        "name": "@paw/digest",
                        "version": "1.0.0",
                        "pi": {"skills": ["./skills"]},
                    }
                ),
                encoding="utf-8",
            )
            first = _hash_extension_app_pi_packages(root / "extension-apps")
            (package / "package.json").write_text(
                json.dumps(
                    {
                        "name": "@paw/digest",
                        "version": "1.0.1",
                        "pi": {"skills": ["./skills"]},
                    }
                ),
                encoding="utf-8",
            )
            second = _hash_extension_app_pi_packages(root / "extension-apps")

        self.assertNotEqual(first, second)
        self.assertEqual(len(first), 32)
        self.assertEqual(len(second), 32)

    def test_extension_app_pi_package_rejects_locale_dependent_paths(self) -> None:
        with tempfile.TemporaryDirectory(prefix="rag-ime-extension-path-order-") as temporary:
            root = Path(temporary)
            source = root / "pi-packages"
            source.mkdir(parents=True)
            (source / "catalog.json").write_text(
                json.dumps({"schemaVersion": 1, "packages": []}),
                encoding="utf-8",
            )
            _app_root, package, _app_manifest, _package_manifest = (
                _write_extension_app_fixture(root)
            )
            (package / "Helper.ts").write_text("export {};\n", encoding="utf-8")

            with self.assertRaisesRegex(ManagedPiRuntimeError, "lowercase ASCII"):
                _copy_bundled_pi_packages(
                    source,
                    root / "payload" / "pi-packages",
                    extension_apps_root=root / "extension-apps",
                )

    def test_extension_app_binding_rejects_package_version_drift(self) -> None:
        with tempfile.TemporaryDirectory(prefix="rag-ime-extension-binding-version-") as temporary:
            root = Path(temporary)
            source = root / "pi-packages"
            source.mkdir(parents=True)
            (source / "catalog.json").write_text(
                json.dumps({"schemaVersion": 1, "packages": []}),
                encoding="utf-8",
            )
            _app_root, package, _app_manifest, package_manifest = _write_extension_app_fixture(root)
            package_manifest["version"] = "0.1.1"
            (package / "package.json").write_text(
                json.dumps(package_manifest, ensure_ascii=False), encoding="utf-8"
            )

            with self.assertRaisesRegex(ManagedPiRuntimeError, "version"):
                _copy_bundled_pi_packages(
                    source,
                    root / "payload" / "pi-packages",
                    extension_apps_root=root / "extension-apps",
                )

    def test_extension_app_binding_requires_declared_capability(self) -> None:
        with tempfile.TemporaryDirectory(prefix="rag-ime-extension-binding-capability-") as temporary:
            root = Path(temporary)
            source = root / "pi-packages"
            source.mkdir(parents=True)
            (source / "catalog.json").write_text(
                json.dumps({"schemaVersion": 1, "packages": []}),
                encoding="utf-8",
            )
            _app_root, package, _app_manifest, package_manifest = (
                _write_extension_app_fixture(root)
            )
            package_manifest["paw"]["capabilities"] = []
            (package / "package.json").write_text(
                json.dumps(package_manifest, ensure_ascii=False), encoding="utf-8"
            )

            with self.assertRaisesRegex(ManagedPiRuntimeError, "binding capability"):
                _copy_bundled_pi_packages(
                    source,
                    root / "payload" / "pi-packages",
                    extension_apps_root=root / "extension-apps",
                )

    def test_extension_app_binding_rejects_manifest_digest_drift(self) -> None:
        with tempfile.TemporaryDirectory(prefix="rag-ime-extension-binding-digest-") as temporary:
            root = Path(temporary)
            source = root / "pi-packages"
            source.mkdir(parents=True)
            (source / "catalog.json").write_text(
                json.dumps({"schemaVersion": 1, "packages": []}),
                encoding="utf-8",
            )
            app_root, _package, app_manifest, _package_manifest = _write_extension_app_fixture(root)
            app_manifest["bindingSha256"] = "f" * 64
            (app_root / "pawos-app.json").write_text(
                json.dumps(app_manifest, ensure_ascii=False), encoding="utf-8"
            )

            with self.assertRaisesRegex(ManagedPiRuntimeError, "bindingSha256"):
                _copy_bundled_pi_packages(
                    source,
                    root / "payload" / "pi-packages",
                    extension_apps_root=root / "extension-apps",
                )

    def test_extension_app_builder_rejects_invalid_visual_contract(self) -> None:
        with tempfile.TemporaryDirectory(prefix="rag-ime-extension-visual-contract-") as temporary:
            root = Path(temporary)
            source = root / "pi-packages"
            source.mkdir(parents=True)
            (source / "catalog.json").write_text(
                json.dumps({"schemaVersion": 1, "packages": []}),
                encoding="utf-8",
            )
            app_root, _package, app_manifest, _package_manifest = (
                _write_extension_app_fixture(root)
            )
            app_manifest["icon"] = {"symbol": "invalid", "background": "#087F68"}
            (app_root / "pawos-app.json").write_text(
                json.dumps(app_manifest, ensure_ascii=False), encoding="utf-8"
            )

            with self.assertRaisesRegex(ManagedPiRuntimeError, "icon"):
                _copy_bundled_pi_packages(
                    source,
                    root / "payload" / "pi-packages",
                    extension_apps_root=root / "extension-apps",
                )

    def test_extension_app_builder_rejects_invalid_sandbox_contract(self) -> None:
        with tempfile.TemporaryDirectory(prefix="rag-ime-extension-sandbox-contract-") as temporary:
            root = Path(temporary)
            source = root / "pi-packages"
            source.mkdir(parents=True)
            (source / "catalog.json").write_text(
                json.dumps({"schemaVersion": 1, "packages": []}),
                encoding="utf-8",
            )
            app_root, _package, app_manifest, _package_manifest = (
                _write_extension_app_fixture(root)
            )
            app_manifest["sandbox"] = {
                "default": "optional",
                "connectorPackageId": "vertical-agent-sandbox",
                "policyId": "host-unrestricted",
            }
            (app_root / "pawos-app.json").write_text(
                json.dumps(app_manifest, ensure_ascii=False), encoding="utf-8"
            )

            with self.assertRaisesRegex(ManagedPiRuntimeError, "sandbox contract"):
                _copy_bundled_pi_packages(
                    source,
                    root / "payload" / "pi-packages",
                    extension_apps_root=root / "extension-apps",
                )

    def test_extension_app_binding_rejects_skill_drift(self) -> None:
        with tempfile.TemporaryDirectory(prefix="rag-ime-extension-binding-skill-") as temporary:
            root = Path(temporary)
            source = root / "pi-packages"
            source.mkdir(parents=True)
            (source / "catalog.json").write_text(
                json.dumps({"schemaVersion": 1, "packages": []}),
                encoding="utf-8",
            )
            app_root, _package, app_manifest, _package_manifest = _write_extension_app_fixture(root)
            app_manifest["skillRef"] = "other-skill"
            (app_root / "pawos-app.json").write_text(
                json.dumps(app_manifest, ensure_ascii=False), encoding="utf-8"
            )

            with self.assertRaisesRegex(ManagedPiRuntimeError, "skillRef"):
                _copy_bundled_pi_packages(
                    source,
                    root / "payload" / "pi-packages",
                    extension_apps_root=root / "extension-apps",
                )

    def test_extension_app_binding_rejects_suite_drift(self) -> None:
        with tempfile.TemporaryDirectory(prefix="rag-ime-extension-binding-suite-") as temporary:
            root = Path(temporary)
            source = root / "pi-packages"
            source.mkdir(parents=True)
            (source / "catalog.json").write_text(
                json.dumps({"schemaVersion": 1, "packages": []}),
                encoding="utf-8",
            )
            app_root, _package, app_manifest, _package_manifest = _write_extension_app_fixture(root)
            app_manifest["verticalSuiteRevision"] = "fixture-v1"
            (app_root / "pawos-app.json").write_text(
                json.dumps(app_manifest, ensure_ascii=False),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ManagedPiRuntimeError, "suite"):
                _copy_bundled_pi_packages(
                    source,
                    root / "payload" / "pi-packages",
                    extension_apps_root=root / "extension-apps",
                )

    def test_staged_smoke_uses_session_lifecycle_contract(self) -> None:
        script = (ROOT / "scripts" / "smoke_pi_session_staged_runtime.py").read_text(
            encoding="utf-8"
        )

        for method in (
            "session.open",
            "session.prompt",
            "session.steer",
            "session.debug.context",
            "session.abort",
            "session.snapshot",
        ):
            self.assertIn(f'"{method}"', script)
        self.assertNotIn('"room.dispatch"', script)
        self.assertNotIn('"room.cancel"', script)

    def test_staged_package_smoke_proves_native_resource_projection(self) -> None:
        script = (
            ROOT / "scripts" / "smoke_pi_packages_staged_runtime.py"
        ).read_text(encoding="utf-8")

        for method in (
            "plugins.catalog",
            "plugins.package.prepare",
            "plugins.install.preview",
            "plugins.install",
            "plugins.enable",
            "plugins.disable",
            "plugins.uninstall",
            "session.commands",
            "tools.list",
        ):
            self.assertIn(f'"{method}"', script)
        self.assertIn("disabledResourcesAbsent", script)
        self.assertIn("DISABLED_PACKAGE_IDS", script)
        self.assertIn("blocked legacy Packages leaked", script)
        self.assertIn("disabledPackagesAbsent", script)
        self.assertIn("independentCapabilityRemoval", script)

    def test_staged_resilience_smoke_covers_compaction_and_tool_failure_stop(self) -> None:
        script = (
            ROOT / "scripts" / "smoke_pi_resilience_staged_runtime.py"
        ).read_text(encoding="utf-8")

        self.assertIn("threshold-continuation", script)
        self.assertIn("THRESHOLD-COMPACTION-CONTINUED-OK", script)
        self.assertIn("tool_loop_no_progress", script)
        self.assertIn("repeated_failure_signature", script)
        self.assertIn('"session.snapshot"', script)

    def test_staged_room_smoke_uses_the_typed_participant_steer_contract(self) -> None:
        script = (ROOT / "scripts" / "smoke_pi_room_composition.py").read_text(
            encoding="utf-8"
        )

        self.assertIn('"action": "steer_participant"', script)
        self.assertNotIn('"action": "steer",', script)
        self.assertIn('"authorityKind": "room_work_item"', script)
        self.assertIn('"partnerWorkDocumentRevision"', script)
        self.assertIn('"partnerWorkItemState"', script)
        self.assertIn('"op": "wait"', script)
        self.assertIn('"op": "accept"', script)
        self.assertIn("def _room_event_status", script)
        self.assertIn('payload.get("data")', script)
        self.assertIn('_room_event_status(event) == "aborted"', script)
        self.assertIn('"operabilityVerdict": "passed"', script)
        self.assertIn('"requirementVerdict": "satisfied"', script)
        self.assertIn("synced_document['documentId']", script)
        self.assertNotIn('child_result.get("status") != "completed"', script)

    def test_product_owns_all_managed_skills(self) -> None:
        skills_root = ROOT / "integrations" / "pi" / "skills"
        skill_dirs = _product_skill_dirs(skills_root)
        skill_names = [item.name for item in skill_dirs]

        self.assertEqual(
            skill_names,
            sorted({
                "agent-eval-room-optimizer",
                "agent-lab-project",
                "alignment-and-decision",
                "bootstrap-project-context",
                "ego-browser",
                "facilitate-room",
                "implementation-planning",
                "improve-codebase-architecture",
                "independent-review",
                "rag-retrieval-optimization",
                "memory-curation",
                "orchestrate-session",
                "organize-work-documents",
                "pawos-app-builder",
                "pawos-system",
                "plugin-creator",
                "project-maintainer",
                "systematic-debugging",
                "test-driven-implementation",
                "trace-agent-diagnostics",
            }),
        )
        for name in skill_names:
            content = (skills_root / name / "SKILL.md").read_text(encoding="utf-8")
            self.assertIn(f"name: {name}", content)
            self.assertIn("\ndescription: ", content)
            if name in SESSION_FLOW_SKILLS:
                frontmatter = yaml.safe_load(content.split("---", 2)[1])
                self.assertEqual(set(frontmatter), {"name", "description"})
                description = frontmatter["description"]
                self.assertIn("Use when", description)
                self.assertIn("Do not use", description)
                self.assertIn("e.g.,", description)
                self.assertLessEqual(len(description), 600)
                self.assertIn("\n## Not For\n", content)
                self.assertIn("\nExample:", content)
                agent_metadata = yaml.safe_load(
                    (skills_root / name / "agents" / "openai.yaml").read_text(
                        encoding="utf-8"
                    )
                )
                self.assertIn(
                    f"${name}",
                    agent_metadata["interface"]["default_prompt"],
                )

        routing_catalog = _validated_skill_routing_catalog(
            SKILL_ROUTING_CARDS,
            skills_root,
        )
        self.assertEqual(
            routing_catalog["schemaVersion"],
            "rag-ime.skill-routing-card-catalog.v1",
        )
        self.assertEqual(
            routing_catalog["collisionPolicy"],
            {
                "default": "reject",
                "bundledWins": {
                    "configured": [],
                    "pi-installed": sorted(skill_names),
                },
            },
        )
        self.assertEqual(
            routing_catalog["scope"],
            {
                "bundledSkillEntries": len(skill_dirs),
                "projectedBundledCards": len(PROJECT_ROUTING_SKILLS),
                "canonicalCards": len(routing_catalog["cards"]),
            },
        )
        cards = routing_catalog["cards"]
        self.assertEqual(len(cards), 41)
        self.assertEqual(len({card["name"] for card in cards}), len(cards))
        self.assertNotIn("structured-result-presentation", {card["name"] for card in cards})
        for card in cards:
            self.assertTrue(card["when"])
            self.assertTrue(card["does"])
            compact = json.dumps(card, ensure_ascii=False, separators=(",", ":"))
            self.assertLessEqual(
                _compact_card_length(card),
                MAX_ROUTING_CARD_CHARS,
                card["name"],
            )
            self.assertNotIn("file://", compact)
            self.assertNotIn(str(ROOT), compact)

        cards_by_name = {card["name"]: card for card in cards}
        for name in PROJECT_ROUTING_SKILLS:
            frontmatter = yaml.safe_load(
                (skills_root / name / "SKILL.md")
                .read_text(encoding="utf-8")
                .split("---", 2)[1]
            )
            self.assertEqual(
                cards_by_name[name],
                _skill_routing_projection(frontmatter, skill_name=name),
            )

        first = json.dumps(routing_catalog, ensure_ascii=False, separators=(",", ":"))
        second = json.dumps(
            _validated_skill_routing_catalog(SKILL_ROUTING_CARDS, skills_root),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        self.assertEqual(first, second)

    def test_orphan_bundled_skill_directories_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="rag-ime-orphan-skill-") as temporary:
            skills_root = Path(temporary) / "skills"
            (skills_root / "valid").mkdir(parents=True)
            (skills_root / "valid" / "SKILL.md").write_text(
                "---\nname: valid\n---\n",
                encoding="utf-8",
            )
            (skills_root / "orphan").mkdir()

            with self.assertRaisesRegex(
                ManagedPiRuntimeError,
                r"bundled Skill discovery failed: .*orphan.*missing SKILL\.md",
            ):
                _product_skill_dirs(skills_root)

            self.assertEqual(BUNDLED_SKILL_SUPPORT_DIRS, frozenset())
            self.assertEqual(
                [item.name for item in _product_skill_dirs(
                    skills_root,
                    allowed_support_dirs=frozenset({"orphan"}),
                )],
                ["valid"],
            )

    def test_unresolved_skill_name_collisions_report_each_source(self) -> None:
        with tempfile.TemporaryDirectory(prefix="rag-ime-skill-collision-") as temporary:
            root = Path(temporary)
            roots: dict[str, Path] = {}
            for source in ("bundled", "configured", "pi-installed"):
                skill_root = root / source / "shared-name"
                skill_root.mkdir(parents=True)
                (skill_root / "SKILL.md").write_text(
                    f"---\nname: shared-name\ndescription: {source}\n---\n{source}\n",
                    encoding="utf-8",
                )
                roots[source] = skill_root

            with self.assertRaises(ManagedPiRuntimeError) as raised:
                _resolve_skill_source_collisions(
                    bundled=(roots["bundled"],),
                    configured=(roots["configured"],),
                    pi_installed=(roots["pi-installed"],),
                    collision_policy={
                        "default": "reject",
                        "bundledWins": {"configured": [], "pi-installed": []},
                    },
                )

        diagnostic = str(raised.exception)
        self.assertIn("unresolved Skill name collision for 'shared-name'", diagnostic)
        self.assertIn("bundled=", diagnostic)
        self.assertIn("configured=", diagnostic)
        self.assertIn("pi-installed=", diagnostic)
        self.assertIn("collisionPolicy.default=reject", diagnostic)

    def test_bundled_skill_wins_only_with_explicit_source_policy(self) -> None:
        skills_root = ROOT / "integrations" / "pi" / "skills"
        bundled_skill = skills_root / "memory-curation"
        with tempfile.TemporaryDirectory(prefix="rag-ime-explicit-skill-winner-") as temporary:
            root = Path(temporary)
            external: dict[str, Path] = {}
            for source in ("configured", "pi-installed"):
                skill_root = root / source / "memory-curation"
                skill_root.mkdir(parents=True)
                (skill_root / "SKILL.md").write_text(
                    f"---\nname: memory-curation\ndescription: {source}\n---\n{source}\n",
                    encoding="utf-8",
                )
                external[source] = skill_root

            resolved = _resolve_skill_source_collisions(
                bundled=(bundled_skill,),
                configured=(external["configured"],),
                pi_installed=(external["pi-installed"],),
                collision_policy={
                    "default": "reject",
                    "bundledWins": {
                        "configured": ["memory-curation"],
                        "pi-installed": ["memory-curation"],
                    },
                },
            )

        winner = resolved["memory-curation"]
        self.assertEqual(winner["source"], "bundled")
        self.assertEqual(Path(winner["path"]), bundled_skill / "SKILL.md")
        self.assertIn(
            "authorized Evidence",
            Path(winner["path"]).read_text(encoding="utf-8"),
        )
        catalog = _validated_skill_routing_catalog(SKILL_ROUTING_CARDS, skills_root)
        bundled_frontmatter = yaml.safe_load(
            (bundled_skill / "SKILL.md").read_text(encoding="utf-8").split("---", 2)[1]
        )
        card = next(
            item for item in catalog["cards"] if item["name"] == "memory-curation"
        )
        self.assertEqual(
            card,
            _skill_routing_projection(
                bundled_frontmatter,
                skill_name="memory-curation",
            ),
        )

    def test_explicit_bundled_winner_shadows_multiple_pi_installed_copies(self) -> None:
        with tempfile.TemporaryDirectory(prefix="rag-ime-skill-shadow-copies-") as temporary:
            root = Path(temporary)
            bundled = root / "bundled" / "plugin-creator"
            installed = root / "pi-installed" / "plugin-creator"
            installed_system = root / "pi-installed" / ".system" / "plugin-creator"
            for path, description in (
                (bundled, "bundled"),
                (installed, "installed"),
                (installed_system, "installed system"),
            ):
                path.mkdir(parents=True)
                (path / "SKILL.md").write_text(
                    f"---\nname: plugin-creator\ndescription: {description}\n---\n",
                    encoding="utf-8",
                )

            resolved = _resolve_skill_source_collisions(
                bundled=(bundled,),
                configured=(),
                pi_installed=(root / "pi-installed",),
                collision_policy={
                    "default": "reject",
                    "bundledWins": {
                        "configured": [],
                        "pi-installed": ["plugin-creator"],
                    },
                },
            )

        self.assertEqual(resolved["plugin-creator"]["source"], "bundled")
        self.assertEqual(
            Path(resolved["plugin-creator"]["path"]),
            (bundled / "SKILL.md").resolve(),
        )

    def test_runtime_banner_starts_with_multiple_authorized_installed_shadows(self) -> None:
        node = _default_node()
        if not node:
            self.skipTest("Node.js is unavailable")
        with tempfile.TemporaryDirectory(prefix="rag-ime-runtime-skill-shadows-") as temporary:
            root = Path(temporary)
            runtime_host = root / "runtime-host"
            bundled = runtime_host / "skills" / "plugin-creator"
            installed = root / "agent" / "skills" / "plugin-creator"
            installed_system = root / "agent" / "skills" / ".system" / "plugin-creator"
            for path, description in (
                (bundled, "bundled"),
                (installed, "installed"),
                (installed_system, "installed system"),
            ):
                path.mkdir(parents=True)
                (path / "SKILL.md").write_text(
                    f"---\nname: plugin-creator\ndescription: {description}\n---\n",
                    encoding="utf-8",
                )
            entrypoint = runtime_host / "collision-smoke.mjs"
            entrypoint.write_text(
                _runtime_host_banner(
                    runtime_host / "skills",
                    {
                        "default": "reject",
                        "bundledWins": {
                            "configured": [],
                            "pi-installed": ["plugin-creator"],
                        },
                    },
                )
                + "\nconsole.log('runtime-host-ready');\n",
                encoding="utf-8",
            )
            completed = subprocess.run(
                [node, str(entrypoint)],
                cwd=runtime_host,
                env={
                    **os.environ,
                    "PI_CODING_AGENT_DIR": str(root / "agent"),
                },
                text=True,
                capture_output=True,
            )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout.strip(), "runtime-host-ready")

    def test_project_routing_card_drift_is_rejected(self) -> None:
        skills_root = ROOT / "integrations" / "pi" / "skills"
        catalog = json.loads(SKILL_ROUTING_CARDS.read_text(encoding="utf-8"))
        project_card = next(
            card for card in catalog["cards"] if card["name"] == "memory-curation"
        )
        project_card["does"] = "drifted duplicate truth"
        with tempfile.TemporaryDirectory(prefix="rag-ime-routing-card-drift-") as temporary:
            cards_path = Path(temporary) / "skill-routing-cards.json"
            cards_path.write_text(
                json.dumps(catalog, ensure_ascii=False),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                ManagedPiRuntimeError,
                "project routing card drift for 'memory-curation'",
            ):
                _validated_skill_routing_catalog(cards_path, skills_root)

    def test_runtime_banner_loads_all_product_skills_without_enabling_global_discovery(self) -> None:
        with tempfile.TemporaryDirectory(prefix="rag-ime-runtime-skills-") as temporary:
            root = Path(temporary)
            for name in ("memory-curation", "plugin-creator"):
                skill_root = root / name
                skill_root.mkdir()
                (skill_root / "SKILL.md").write_text(
                    f"---\nname: {name}\ndescription: test\n---\n",
                    encoding="utf-8",
                )

            banner = _runtime_host_banner(root)

        self.assertIn('"memory-curation"', banner)
        self.assertIn('"plugin-creator"', banner)
        self.assertIn('process.env.RAG_IME_PI_SKILL_PATHS', banner)
        self.assertIn('process.env.RAG_IME_PI_SKILL_ROUTING_CARDS', banner)
        self.assertIn('skill-routing-cards.json', banner)
        self.assertIn('__join(__ragImeRuntimeDir, "skills", name)', banner)
        self.assertIn("RAG_IME_PI_USER_SKILL_PATHS", banner)
        self.assertIn("Unresolved Skill name collision", banner)
        self.assertIn("collisionPolicy.default=reject", banner)
        self.assertIn("explicitBundledWinner", banner)
        self.assertNotIn("--no-skills", banner)

    def test_managed_payload_copies_session_skills_and_memory_contract(self) -> None:
        source_root = ROOT / "integrations" / "pi" / "skills"
        with tempfile.TemporaryDirectory(prefix="rag-ime-runtime-skill-copy-") as temporary:
            runtime_root = Path(temporary) / "runtime-host" / "skills"
            copied = _copy_product_skills(source_root, runtime_root)
            skill_root = runtime_root / "memory-curation"
            skill = (skill_root / "SKILL.md").read_text(encoding="utf-8")
            alignment = (runtime_root / "alignment-and-decision" / "SKILL.md").read_text(
                encoding="utf-8"
            )
            orchestration = (
                runtime_root / "orchestrate-session" / "SKILL.md"
            ).read_text(encoding="utf-8")
            facilitation = (
                runtime_root / "facilitate-room" / "SKILL.md"
            ).read_text(encoding="utf-8")
            bootstrap = (
                runtime_root / "bootstrap-project-context" / "SKILL.md"
            ).read_text(encoding="utf-8")
            organization = (
                runtime_root / "organize-work-documents" / "SKILL.md"
            ).read_text(encoding="utf-8")
            ego_browser = (
                runtime_root / "ego-browser" / "SKILL.md"
            ).read_text(encoding="utf-8")
            independent_review = (
                runtime_root / "independent-review" / "SKILL.md"
            ).read_text(encoding="utf-8")
            agent_prompt = (skill_root / "agents" / "openai.yaml").read_text(
                encoding="utf-8"
            )
            orchestration_prompt = (
                runtime_root / "orchestrate-session" / "agents" / "openai.yaml"
            ).read_text(encoding="utf-8")

        self.assertIn("memory-curation", copied)
        self.assertIn("alignment-and-decision", copied)
        self.assertIn("orchestrate-session", copied)
        self.assertIn("facilitate-room", copied)
        self.assertIn("bootstrap-project-context", copied)
        self.assertIn("organize-work-documents", copied)
        for retired in (
            "implementation-execution",
            "quality-gate",
            "review-feedback-resolution",
            "structured-handoff",
            "work-document-archive",
        ):
            self.assertNotIn(retired, copied)
        self.assertIn("name: alignment-and-decision", alignment)
        self.assertIn("material user-owned choices", alignment)
        self.assertIn("Session's native subagent capability", orchestration)
        self.assertIn("does not define another event bus", orchestration)
        self.assertIn("Batch dispatch", orchestration)
        self.assertIn("fresh/new", orchestration)
        self.assertIn("does not rank either mode", orchestration)
        self.assertIn("A pass on only one axis is not closure", orchestration)
        self.assertIn("AGENTS.md", bootstrap)
        self.assertIn("write's `workDocument` field", bootstrap)
        self.assertIn("live", bootstrap)
        self.assertIn("authorityRevision", bootstrap)
        self.assertIn("never emit `clear`/`passed` for unverified work", independent_review)
        self.assertIn("rewrite unverified work as `clear`/`passed`", independent_review)
        self.assertIn("Never fall through to standalone Google", ego_browser)
        self.assertIn("not permission to drive desktop Chrome or Edge", ego_browser)
        self.assertIn("Partners remain ordinary Sessions", facilitation)
        self.assertIn(
            "Partner assignment exists only after a real delegated dispatch",
            facilitation,
        )
        self.assertIn("prerequisites for submission, not acceptance", facilitation)
        self.assertIn("one `delegate_batch` call", facilitation)
        self.assertIn("never describe consecutive `delegate` calls as parallel", facilitation)
        self.assertIn("emit the best evidence-backed partial or blocked final", facilitation)
        self.assertIn("unfinished, failed, orphaned, partial, and unclosed", facilitation)
        self.assertIn("does not prove requirement satisfaction", facilitation)
        self.assertIn("returns an immediate durable dispatch receipt", facilitation)
        self.assertIn("durable wake", facilitation)
        self.assertIn("`room_partner collect` or `wait`", facilitation)
        self.assertIn("A `wait` timeout never cancels", facilitation)
        self.assertIn("explicitly call `accept` or `return`", facilitation)
        self.assertIn("Do not pause a live Room Goal to wait", facilitation)
        self.assertIn("pause a live Room Goal as a wait", facilitation)
        self.assertIn("If a reviewer reported `unverified`", facilitation)
        self.assertIn("live `authorityRevision`", facilitation)
        self.assertIn("product `browser` tool", facilitation)
        self.assertIn("Before any requirements, business-code, configuration, or test write", facilitation)
        self.assertIn("`agent_goal confirm_setup`", facilitation)
        self.assertIn('workDocument={authorityKind:"session_goal"', facilitation)
        self.assertIn("successful `workDocumentRegistration`", facilitation)
        self.assertIn("do not create an unbound requirements file", facilitation)
        self.assertIn("A plain `docs/agent/requirements.md`, Todo, or Room post is not that binding", facilitation)
        self.assertNotIn("automatically accepted", facilitation)
        self.assertIn("document gardener", organization)
        self.assertIn("both verification axes", organization)
        self.assertIn("$orchestrate-session", orchestration_prompt)
        for required in (
            "authorized Evidence -> one Current Atom",
            "Task Timeline for continuity only",
            "legitimate trigger",
            "durable-information gate",
            "correction",
            "duplicate",
            "conflict",
            "native review boundary",
            "One Atom holds one current claim",
            "Background curation remains bounded",
            "Do not read or write SQLite directly",
            "memory capture and memory curation",
        ):
            self.assertIn(required, skill)
        self.assertIn("durable personal memory", agent_prompt)
        self.assertIn("Preserve provenance", agent_prompt)
        self.assertIn("background auto-apply", agent_prompt)


if __name__ == "__main__":
    unittest.main()
