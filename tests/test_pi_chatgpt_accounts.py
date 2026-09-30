from pathlib import Path
import shutil
import subprocess
import unittest


class ChatGPTAccountTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node is unavailable")
    def test_account_selection_and_protected_vm_transfer(self) -> None:
        result = subprocess.run(
            ["node", "--test", str(Path(__file__).with_name("pi_chatgpt_accounts.test.mjs"))],
            capture_output=True, text=True, timeout=30, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
