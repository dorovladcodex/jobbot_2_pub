import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from jobbot.codex import codex_binary, installed_desktop_codex


class CodexRuntimeTests(unittest.TestCase):
    def tearDown(self):
        installed_desktop_codex.cache_clear()

    @unittest.skipUnless(os.name == "nt", "Windows desktop runtime selection")
    def test_desktop_runtime_precedes_outdated_npm_runtime(self):
        with TemporaryDirectory() as directory:
            binary = Path(directory) / "codex.exe"
            binary.touch()
            with patch.dict("os.environ", {"JOBBOT_CODEX_BIN": "", "CODEX_ELECTRON_RESOURCES_PATH": directory}), \
                    patch("jobbot.codex.shutil.which", return_value="old-npm-codex.exe"):
                self.assertEqual(codex_binary(), str(binary))

    def test_explicit_binary_override_keeps_priority(self):
        with patch.dict("os.environ", {"JOBBOT_CODEX_BIN": "operator-selected-codex"}), \
                patch("jobbot.codex.installed_desktop_codex") as discover:
            self.assertEqual(codex_binary(), "operator-selected-codex")
            discover.assert_not_called()

    def test_desktop_discovery_for_regular_powershell_is_cached(self):
        installed_desktop_codex.cache_clear()
        with TemporaryDirectory() as directory:
            binary = Path(directory) / "app" / "resources" / "codex.exe"
            binary.parent.mkdir(parents=True)
            binary.touch()
            with patch("jobbot.codex.shutil.which", return_value="powershell.exe"), \
                    patch("jobbot.codex.subprocess.run", return_value=subprocess.CompletedProcess(
                        [], 0, directory + "\n", "")) as query:
                self.assertEqual(installed_desktop_codex(), str(binary))
                self.assertEqual(installed_desktop_codex(), str(binary))
                query.assert_called_once()
                self.assertIn("Get-AppxPackage", query.call_args.args[0][-1])

    def test_missing_desktop_package_allows_cli_fallback(self):
        installed_desktop_codex.cache_clear()
        with patch("jobbot.codex.shutil.which", return_value="powershell.exe"), \
                patch("jobbot.codex.subprocess.run", side_effect=subprocess.CalledProcessError(1, "powershell")):
            self.assertIsNone(installed_desktop_codex())
