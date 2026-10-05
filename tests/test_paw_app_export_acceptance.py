from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from contextlib import ExitStack
from unittest.mock import patch

from tests.subprocess_startup import StartupCapture

from scripts.eval_paw_app_export_acceptance import (
    ExportAcceptanceError,
    run_paw_app_export_acceptance,
)


ROOT = Path(__file__).resolve().parents[1]
SOURCE_APP = ROOT / "control-center-web" / "extension-apps" / "zhanggui-wenshu"


class PawAppExportAcceptanceTests(unittest.TestCase):
    def _write_frontend_fixture(self, root: Path, *, production: bool = True) -> Path:
        dist = root / "frontend-dist"
        assets = dist / "assets"
        assets.mkdir(parents=True)
        (dist / "index.html").write_text(
            '<!doctype html><html><body><div id="root"></div>'
            '<script type="module" src="./assets/index.js"></script></body></html>\n',
            encoding="utf-8",
        )
        (assets / "index.js").write_text(
            "export const app = 'extension:zhanggui-wenshu 掌柜问数';\n",
            encoding="utf-8",
        )
        (dist / "rag-ime-control-web-build.json").write_text(
            json.dumps(
                {
                    "schemaVersion": "rag-ime.control-web-build.v1",
                    "buildChannel": "production" if production else "preview",
                    "transport": "http",
                    "nativeOnly": False,
                    "httpOnly": True,
                    "forbiddenTransportModulesExcluded": production,
                    "previewFixturesExcluded": production,
                    "sourceCommit": "fixture",
                }
            ),
            encoding="utf-8",
        )
        return dist

    def test_cli_help_runs_from_repository_root(self) -> None:
        completed = subprocess.run(
            [sys.executable, "scripts/eval_paw_app_export_acceptance.py", "--help"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertIn("--app-directory", completed.stdout)
        self.assertIn("--output", completed.stdout)

    def test_exports_real_app_source_package_and_frontend_with_clean_start_and_eval_parity(self) -> None:
        with tempfile.TemporaryDirectory(prefix="paw-app-export-test-") as temporary, ExitStack() as resources:
            root = Path(temporary)
            capture = StartupCapture(root / "startup-diagnostics", label="clean-static-export", isolated=True,
                match=lambda command: isinstance(command, (list, tuple))
                    and list(command[1:5]) == ["-I", "-u", "-m", "http.server"])
            resources.callback(capture.close)
            with capture, patch("scripts.eval_paw_app_export_acceptance.subprocess.Popen", side_effect=capture.popen):
                report = run_paw_app_export_acceptance(
                    SOURCE_APP,
                    root / "zhanggui-wenshu.pawos.zip",
                    frontend_dist=self._write_frontend_fixture(root),
                    run_build=False,
                    run_ui_tests=False,
                )

            self.assertEqual("passed", report["status"])
            self.assertEqual("extension:zhanggui-wenshu", report["source"]["appId"])
            self.assertEqual("passed", report["acceptance"]["cleanStart"]["status"])
            self.assertEqual("not_requested", report["acceptance"]["browserSmoke"]["status"])
            self.assertFalse(report["acceptance"]["parity"]["functionality"]["browserSmokePassed"])
            self.assertFalse(report["acceptance"]["parity"]["functionality"]["sourceUiPassed"])
            self.assertTrue(report["acceptance"]["parity"]["evaluation"]["metricsEqual"])
            self.assertEqual(
                report["source"]["candidateEval"]["result"]["metrics"],
                report["acceptance"]["exportedCandidateEval"]["result"]["metrics"],
            )

            with zipfile.ZipFile(root / "zhanggui-wenshu.pawos.zip") as archive:
                names = set(archive.namelist())
                prefix = "pawos-app-export.v1/"
                self.assertIn(prefix + "web/index.html", names)
                self.assertIn(prefix + "web/assets/index.js", names)
                self.assertIn(prefix + "app/zhanggui-wenshu/App.tsx", names)
                self.assertIn(prefix + "app/zhanggui-wenshu/pi-package/package.json", names)
                self.assertIn(prefix + "dependencies/control-center-web.package.json", names)
                self.assertIn(prefix + "dependencies/pnpm-lock.yaml", names)
                self.assertFalse(any("node_modules/" in name for name in names))
                self.assertFalse(any(name.endswith(".env") for name in names))

            encoded = json.dumps(report, ensure_ascii=False)
            self.assertNotIn(str(root / "frontend-dist"), encoded)
            self.assertNotIn(str(root / "workspace"), encoded)

    def test_rejects_preview_frontend_as_a_release_export(self) -> None:
        with tempfile.TemporaryDirectory(prefix="paw-app-export-preview-") as temporary:
            root = Path(temporary)
            with self.assertRaisesRegex(ExportAcceptanceError, "production frontend"):
                run_paw_app_export_acceptance(
                    SOURCE_APP,
                    root / "preview.zip",
                    frontend_dist=self._write_frontend_fixture(root, production=False),
                    run_build=False,
                    run_ui_tests=False,
                )


if __name__ == "__main__":
    unittest.main()
