"""Verification failures use disposable trees and never build the app."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("verify", ROOT / "scripts/verify.py")
verify = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(verify)


class VerifyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.env = verify.command_environment()
        subprocess.run(["git", "init", "-q", str(self.root)], env=self.env, check=True)
        self.write("tests/__init__.py", "")
        self.write("tests/test_example.py", "import unittest\nclass Example(unittest.TestCase):\n    def test_ok(self):\n        self.assertTrue(True)\n")
        self.write("release.json", json.dumps({"version": "0.1.0"}))
        self.write("package.json", json.dumps({"version": "0.1.0"}))
        self.write("public-files.json", json.dumps({"files": ["release.json"]}))
        for name in ("docs/development.md", "private-check.md"):
            self.write(name, "Run `python3 scripts/verify.py fast`.\n")
        self.test_names = ("tests.test_example.Example",)

    def write(self, name, content):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def fast(self, args=None, **environment):
        # The subprocess exercises the exit status and the real test-runner boundary.
        bootstrap = (
            "import importlib.util, pathlib, sys; "
            "spec = importlib.util.spec_from_file_location('verify', sys.argv[1]); "
            "verify = importlib.util.module_from_spec(spec); spec.loader.exec_module(verify); "
            "raise SystemExit(verify.main(sys.argv[2:], root=pathlib.Path.cwd(), "
            "test_names=('tests.test_example.Example',)))"
        )
        return subprocess.run(
            [sys.executable, "-B", "-c", bootstrap, str(ROOT / "scripts/verify.py"), *(args or ["fast"])],
            cwd=self.root, env={**self.env, **environment}, capture_output=True, text=True,
        )

    def test_fast_passes_in_a_complete_tree(self):
        result = self.fast()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("PASS test suite:", result.stdout)

    def test_unittest_home_is_temporary_and_ignores_caller_config(self):
        caller_home = self.root / "caller-home"
        self.write("caller-home/.config/igloo/config.json", "not-json\n")
        self.write("tests/test_example.py", """import json
import os
from pathlib import Path
import sys
import unittest

sys.path.insert(0, os.environ["VERIFY_BACKEND"])
import organizer

class Example(unittest.TestCase):
    def test_home(self):
        home = Path(os.environ["HOME"])
        self.assertTrue(home.is_absolute())
        self.assertTrue(home.is_dir())
        self.assertFalse((home / ".config/igloo/config.json").exists())
        Path("child-home.json").write_text(json.dumps({"home": str(home)}))
""")
        result = self.fast(HOME=str(caller_home), VERIFY_BACKEND=str(ROOT / "backend"))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        child_home = Path(json.loads((self.root / "child-home.json").read_text())["home"])
        self.assertNotEqual(child_home, caller_home)
        self.assertFalse(child_home.exists(), "The temporary test HOME must be cleaned up")
        self.assertEqual((caller_home / ".config/igloo/config.json").read_text(), "not-json\n")

    def test_failing_test_makes_fast_exit_nonzero(self):
        self.write("tests/test_example.py", "import unittest\nclass Example(unittest.TestCase):\n    def test_failure(self):\n        self.fail('deliberate verification failure')\n")
        result = self.fast()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("FAIL test suite:", result.stdout)
        self.assertIn("deliberate verification failure", result.stdout)
        self.assertIn("PASS public files:", result.stdout)

    def test_test_outputs_use_temporary_storage_outside_source(self):
        self.write("tests/test_example.py", "import pathlib, tempfile, unittest\nclass Example(unittest.TestCase):\n    def test_output_location(self):\n        with tempfile.TemporaryDirectory() as output:\n            self.assertFalse(pathlib.Path(output).resolve().is_relative_to(pathlib.Path.cwd()))\n")
        temporary = self.root / "tmp"
        temporary.mkdir()
        result = self.fast(TMPDIR=str(temporary), TMP=str(temporary), TEMP=str(temporary))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_uncovered_test_module_is_reported(self):
        self.write("tests/test_new.py", "")
        result = self.fast()
        self.assertEqual(result.returncode, 1)
        self.assertIn("FAIL test coverage:", result.stdout)
        self.assertIn("tests.test_new", result.stdout)

    def test_version_mismatch_is_reported(self):
        self.write("package.json", json.dumps({"version": "0.2.0"}))
        result = self.fast()
        self.assertEqual(result.returncode, 1)
        self.assertIn("FAIL versions:", result.stdout)
        self.assertIn("package.json", result.stdout)

    def test_public_tree_skips_absent_private_package_metadata(self):
        (self.root / "package.json").unlink()
        result = self.fast()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("SKIPPED package.json version comparison:", result.stdout)

    def test_in_app_version_must_agree_when_present(self):
        path = self.root / "app/Info.plist"
        path.parent.mkdir()
        path.write_bytes(plistlib.dumps({"CFBundleShortVersionString": "0.2.0"}))
        result = self.fast()
        self.assertEqual(result.returncode, 1)
        self.assertIn("FAIL versions:", result.stdout)
        self.assertIn("app/Info.plist", result.stdout)

    def test_public_content_skips_without_private_policy(self):
        result = self.fast()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("SKIPPED public content:", result.stdout)
        self.assertNotIn("PASS public content:", result.stdout)

    def test_public_content_uses_policy_terms_and_exact_allowed_lines(self):
        self.write("README.md", "confidential-marker allowed\nconfidential-marker forbidden\n")
        self.write("public-files.json", json.dumps({"files": ["README.md", "release.json"]}))
        self.write("private-paths.json", json.dumps({"paths": [], "terms": ["confidential-marker"],
                                                     "allowedLines": {"README.md": ["confidential-marker allowed"]}}))
        result = self.fast()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("FAIL public content:", result.stdout)
        self.assertIn("README.md:2", result.stdout)
        self.assertNotIn("README.md:1", result.stdout)

    def test_public_content_rejects_incomplete_policy(self):
        self.write("private-paths.json", json.dumps({"paths": []}))
        result = self.fast()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("FAIL public content:", result.stdout)

    def test_public_content_rejects_private_repository_reference(self):
        self.write("private-paths.json", json.dumps({"paths": [], "terms": ["igloo" + "-dev"], "allowedLines": {}}))
        self.write("README.md", "The development repository is igloo" + "-dev.\n")
        self.write("public-files.json", json.dumps({"files": ["README.md", "release.json"]}))
        result = self.fast()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("FAIL public content:", result.stdout)
        self.assertIn("README.md:1", result.stdout)

    def test_public_content_rejects_relative_link_to_private_architecture(self):
        self.write("private-paths.json", json.dumps({"paths": ["docs/architecture" + ".md"], "terms": [], "allowedLines": {}}))
        self.write("docs/public.md", "[Architecture](architecture" + ".md)\n")
        self.write("public-files.json", json.dumps({"files": ["docs/public.md", "release.json"]}))
        result = self.fast()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("FAIL public content:", result.stdout)
        self.assertIn("docs/public.md:1", result.stdout)

    def test_public_content_rejects_process_role_from_policy(self):
        terms = json.loads((ROOT / "private-paths.json").read_text())["terms"]
        role = "orch" + "estrator"
        self.assertIn(role, terms)
        self.write("private-paths.json", json.dumps({"paths": [], "terms": terms, "allowedLines": {}}))
        self.write("README.md", "The " + role + " checks this page.\n")
        self.write("public-files.json", json.dumps({"files": ["README.md", "release.json"]}))
        result = self.fast()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("FAIL public content:", result.stdout)
        self.assertIn("README.md:1", result.stdout)

    def test_public_content_rejects_draft_release_notes(self):
        for text, line in (("Igloo 9.9.9\n" + "Dr" + "aft notes.\n", 2), ("Igloo 9.9.9\n\n- " + "TO" + "DO list fixes\n", 3)):
            with self.subTest(line=line):
                self.write("private-paths.json", json.dumps({"paths": [], "terms": [], "allowedLines": {}}))
                self.write("release-notes/9.9.9.md", text)
                self.write("public-files.json", json.dumps({"files": ["release-notes/9.9.9.md", "release.json"]}))
                result = self.fast()
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn("draft text in public release notes: release-notes/9.9.9.md:%d" % line, result.stdout)
                self.assertNotIn("list fixes", result.stdout)

    def test_public_updater_tests_import_without_private_release_tests(self):
        self.write("tests/test_updates.py", (ROOT / "tests/test_updates.py").read_text())
        result = subprocess.run([sys.executable, "-B", "-c", "import tests.test_updates"],
                                cwd=self.root, env=self.env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.root / "tests/test_releases.py").exists())

    def test_missing_public_file_is_reported(self):
        self.write("public-files.json", json.dumps({"files": ["missing.txt"]}))
        result = self.fast()
        self.assertEqual(result.returncode, 1)
        self.assertIn("FAIL public files:", result.stdout)
        self.assertIn("missing.txt", result.stdout)

    def test_public_entry_matching_private_pattern_fails(self):
        self.write("private-paths.json", json.dumps({"paths": ["notes/*"], "terms": [], "allowedLines": {}}))
        self.write("notes/internal.md", "Temporary private note.\n")
        self.write("public-files.json", json.dumps({"files": ["release.json", "notes/internal.md"]}))
        result = self.fast()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("PASS public files:", result.stdout)
        self.assertIn("FAIL private paths:", result.stdout)
        self.assertIn("notes/internal.md", result.stdout)

    def test_public_entries_without_private_matches_pass(self):
        self.write("private-paths.json", json.dumps({"paths": ["notes/*"], "terms": [], "allowedLines": {}}))
        result = self.fast()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("PASS private paths:", result.stdout)

    def test_absent_private_path_policy_is_skipped(self):
        result = self.fast()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("SKIPPED private paths:", result.stdout)
        self.assertIn("private-paths.json is absent", result.stdout)
        self.assertNotIn("PASS private paths:", result.stdout)

    def test_legacy_test_commands_are_reported_in_each_document(self):
        self.write("private-paths.json", json.dumps({"paths": [], "terms": [], "allowedLines": {}, "documentationFiles": ["private-check.md"]}))
        for name in ("docs/development.md", "private-check.md"):
            with self.subTest(document=name):
                self.write(name, "```sh\npython3 -B -m unittest -q tests.test_example\n```\n")
                result = self.fast()
                self.assertEqual(result.returncode, 1)
                self.assertIn("FAIL documentation:", result.stdout)
                self.assertIn(name, result.stdout)
                self.write(name, "Run `python3 scripts/verify.py fast`.\n")

    def test_public_tree_reports_absent_private_documentation_as_skipped(self):
        result = self.fast()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("SKIPPED private documentation:", result.stdout)

    def release(self, build_status=0, signature_status=0, platform="darwin", rosetta_status=True):
        calls = []
        def rosetta_result(root):
            status, reason = (("SKIPPED", "requires macOS") if verify.sys.platform != "darwin"
                              else (("PASS", "mocked test") if rosetta_status else ("FAIL", "mocked failure")))
            verify.report(status, "Rosetta", reason)
            return rosetta_status
        self.rosetta_check = Mock(side_effect=rosetta_result)

        def run(command, **kwargs):
            calls.append((command, kwargs))
            code, output = 0, ""
            if command == ["git", "rev-parse", "HEAD"]:
                output = "a" * 40 + "\n"
            elif command[:2] == ["git", "status"]:
                output = "?? sample.txt\n"
            elif command == ["./build.command"]:
                code = build_status
            elif command[0] == "codesign":
                code = signature_status
            return subprocess.CompletedProcess(command, code, output, "")

        output = io.StringIO()
        with patch.object(verify.subprocess, "run", side_effect=run), patch.object(verify.sys, "platform", platform), patch.object(verify, "rosetta_check", self.rosetta_check), contextlib.redirect_stdout(output):
            code = verify.main(["release"], root=self.root, test_names=self.test_names)
        return code, output.getvalue(), calls

    def test_release_names_commit_tree_and_every_unverified_check(self):
        code, output, calls = self.release()
        self.assertEqual(code, 0, output)
        self.assertIn("a" * 40, output)
        self.assertIn("dirty", output)
        for name in ("native capture launcher", "native window-state review", "Intel execution", "live updater acceptance", "installed-app update"):
            self.assertIn("SKIPPED " + name + ":", output)
            self.assertNotIn("PASS " + name, output)
        self.assertIn("PASS Rosetta: mocked test", output)
        self.rosetta_check.assert_called_once_with(self.root)
        commands = [command for command, kwargs in calls]
        self.assertLess(commands.index(["./build.command"]), commands.index(["codesign", "--verify", "--deep", "--strict", "Igloo.app"]))
        self.assertTrue(all(kwargs["cwd"] == self.root for command, kwargs in calls))

    def test_failed_build_does_not_verify_a_stale_bundle(self):
        code, output, calls = self.release(build_status=1)
        self.assertEqual(code, 1)
        self.assertIn("FAIL native build:", output)
        self.assertIn("SKIPPED signature:", output)
        self.assertFalse(any(command[0] == "codesign" for command, kwargs in calls))

    def test_signature_failure_makes_release_red(self):
        code, output, _ = self.release(signature_status=1)
        self.assertEqual(code, 1)
        self.assertIn("FAIL signature:", output)

    def test_rosetta_failure_makes_release_red(self):
        code, output, _ = self.release(rosetta_status=False)
        self.assertEqual(code, 1)
        self.assertIn("FAIL Rosetta: mocked failure", output)

    def test_non_macos_release_skips_platform_checks(self):
        code, output, calls = self.release(platform="linux")
        self.assertEqual(code, 0, output)
        self.assertIn("SKIPPED native build:", output)
        self.assertIn("SKIPPED signature:", output)
        self.assertIn("SKIPPED Rosetta:", output)
        self.assertFalse(any(command[0] in ("./build.command", "codesign") for command, kwargs in calls))

    def test_release_skips_rosetta_when_translation_is_unavailable(self):
        result = subprocess.CompletedProcess(["arch"], 1, "", "")
        output = io.StringIO()
        with patch.object(verify.sys, "platform", "darwin"), \
             patch.object(verify.platform, "machine", return_value="arm64"), \
             patch.object(verify.subprocess, "run", return_value=result) as run, \
             contextlib.redirect_stdout(output):
            self.assertTrue(verify.rosetta_check(self.root))
        run.assert_called_once()
        self.assertIn("SKIPPED Rosetta: x86_64 translation is not installed", output.getvalue())

    def test_release_help_does_not_start_a_build(self):
        with patch.object(verify.subprocess, "run") as run, contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as result:
                verify.main(["release", "--help"], root=self.root)
        self.assertEqual(result.exception.code, 0)
        run.assert_not_called()

    def test_install_hook_uses_clone_local_config(self):
        for name in ("pre-push", "commit-msg"):
            self.write(".githooks/" + name, "#!/bin/sh\nexit 0\n")
            (self.root / ".githooks" / name).chmod(0o755)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = verify.main(["install-hook"], root=self.root)
        self.assertEqual(code, 0, output.getvalue())
        result = subprocess.run(["git", "config", "--local", "--get", "core.hooksPath"], cwd=self.root, env=self.env, capture_output=True, text=True, check=True)
        self.assertEqual(result.stdout.strip(), ".githooks")

    def test_install_hook_refuses_missing_hook_files(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = verify.main(["install-hook"], root=self.root)
        self.assertEqual(code, 1, output.getvalue())
        self.assertIn("FAIL install hook:", output.getvalue())
        result = subprocess.run(["git", "config", "--local", "--get", "core.hooksPath"], cwd=self.root, env=self.env, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)

    def git_fixture(self, *args, check=True, input=None):
        env = dict(self.env, HOME=str(self.root), CFFIXED_USER_HOME=str(self.root))
        result = subprocess.run(["git", *args], cwd=self.root, env=env, input=input, capture_output=True, text=True)
        if check:
            self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def configure_git_fixture(self):
        self.git_fixture("config", "user.name", "Fixture")
        self.git_fixture("config", "user.email", "fixture@example.invalid")
        self.git_fixture("config", "core.hooksPath", str(self.root / "empty-hooks"))
        (self.root / "empty-hooks").mkdir(exist_ok=True)

    def fixture_commit(self, message):
        self.git_fixture("commit", "--allow-empty", "-m", message)
        return self.git_fixture("rev-parse", "HEAD").stdout.strip()

    def test_commit_message_command_accepts_issue_record_merge_and_rejects_other(self):
        for message, valid in (("Fix #12", True), ("Fix typo\n\nRefs #12", True), ("Record Owner decision", True), ("Merge branch x", True),
                               ("Fix typo", False), ("fixup! Fix typo", False), ("squash! Fix typo", False)):
            with self.subTest(message=message):
                self.write("message.txt", message + "\n")
                result = subprocess.run([sys.executable, "-B", str(ROOT / "scripts/verify.py"), "commit-msg", str(self.root / "message.txt")],
                                        cwd=self.root, env=self.env, capture_output=True, text=True)
                self.assertEqual(result.returncode == 0, valid, result.stdout + result.stderr)
                if not valid:
                    self.assertIn("#N", result.stdout + result.stderr)
                    self.assertIn("Record", result.stdout + result.stderr)
                    self.assertIn("Merge", result.stdout + result.stderr)

    def test_install_hook_installs_both_and_checks_real_fixture_commits(self):
        self.configure_git_fixture()
        self.write("scripts/verify.py", (ROOT / "scripts/verify.py").read_text())
        for name in ("pre-push", "commit-msg"):
            source = ROOT / ".githooks" / name
            self.assertTrue(source.is_file(), name)
            self.assertTrue(source.stat().st_mode & 0o111, name)
            self.write(".githooks/" + name, source.read_text())
            (self.root / ".githooks" / name).chmod(0o755)
        for _ in range(2):
            result = subprocess.run([sys.executable, "-B", "scripts/verify.py", "install-hook"], cwd=self.root,
                                    env=self.env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.git_fixture("config", "--local", "--get", "core.hooksPath").stdout.strip(), ".githooks")
        for message in ("Fix #12", "Record Owner decision", "Merge branch x"):
            self.fixture_commit(message)
        head = self.git_fixture("rev-parse", "HEAD").stdout.strip()
        refused = self.git_fixture("commit", "--allow-empty", "-m", "Fix typo", check=False)
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("#N", refused.stdout + refused.stderr)
        self.assertEqual(self.git_fixture("rev-parse", "HEAD").stdout.strip(), head)
        branch = self.git_fixture("branch", "--show-current").stdout.strip()
        self.git_fixture("checkout", "-b", "fixture-side")
        self.fixture_commit("Side #12")
        self.git_fixture("checkout", branch)
        self.fixture_commit("Main #13")
        self.git_fixture("merge", "--no-ff", "--no-edit", "fixture-side")
        subject = self.git_fixture("log", "-1", "--format=%s").stdout.strip()
        self.assertTrue(subject.startswith("Merge"), subject)

    def test_fast_checks_every_commit_in_the_requested_range(self):
        self.configure_git_fixture()
        base = self.fixture_commit("Record baseline")
        bad = self.fixture_commit("Fix typo")
        self.fixture_commit("Fix #12")
        result = self.fast(args=["fast", "--base", base])
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("FAIL commit messages:", result.stdout)
        self.assertIn(bad[:7], result.stdout)

    def test_push_check_checks_intermediate_commits_without_network(self):
        self.configure_git_fixture()
        base = self.fixture_commit("Record baseline")
        bad = self.fixture_commit("Fix typo")
        head = self.fixture_commit("Fix #12")
        line = f"refs/heads/main {head} refs/heads/main {base}\n"
        self.write("scripts/verify.py", (ROOT / "scripts/verify.py").read_text())
        result = subprocess.run([sys.executable, "-B", "scripts/verify.py", "check-push", "origin"],
                                cwd=self.root, env=self.env, input=line, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(bad[:7], result.stdout + result.stderr)

    def test_command_environment_removes_foreign_git_locations(self):
        with patch.dict(os.environ, {"GIT_DIR": "/foreign/repo", "GIT_WORK_TREE": "/foreign/tree", "GIT_INDEX_FILE": "/foreign/index", "GIT_COMMON_DIR": "/foreign/common"}):
            env = verify.command_environment()
        for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR"):
            self.assertNotIn(name, env)


if __name__ == "__main__":
    unittest.main()
