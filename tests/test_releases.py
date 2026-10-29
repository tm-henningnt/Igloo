"""Release tests use disposable local repositories; no public resources are created."""
import copy
import contextlib
import io
import hashlib
import importlib.util
import json
import os
import plistlib
import sys
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from tests.test_updates import compile_update_check

spec = importlib.util.spec_from_file_location("release_tools", Path(__file__).resolve().parents[1] / "scripts/release.py")
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)

class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root / "private"
        self.repo.mkdir()
        self.git(self.repo, "init", "-q", "-b", "main")
        self.configure(self.repo)
        self.info = {"version": "0.1.0", "build": 13, "minimumSystemVersion": "14.0", "repository": release.PUBLIC_REPO}
        (self.repo / "release.json").write_text(json.dumps(self.info))
        (self.repo / "README.md").write_text("Public source")
        (self.repo / ".env").write_text("private history")
        (self.repo / "public-files.json").write_text(json.dumps({"files": ["README.md", "release.json", "public-files.json"]}))
        self.commit(self.repo)
        (self.repo / ".env").write_text("another private revision")
        self.commit(self.repo)
    def tearDown(self):
        self.temp.cleanup()
    def git(self, cwd, *args, check=True):
        result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
        if check:
            self.assertEqual(result.returncode, 0, result.stderr)
        return result
    def configure(self, repo):
        self.git(repo, "config", "user.name", "Release Fixture")
        self.git(repo, "config", "user.email", "release@example.invalid")
    def commit(self, repo):
        self.git(repo, "add", "--all")
        self.git(repo, "commit", "-qm", "fixture")
    def exported(self, name="snapshot"):
        destination = self.root / name
        release.export_snapshot(self.repo, destination)
        return destination
    def prepared(self):
        folder = self.root / "prepared"
        folder.mkdir()
        release.export_snapshot(self.repo, folder / "source")
        assets = folder / "assets"
        assets.mkdir()
        archive = assets / "Igloo-0.1.0-universal.zip"
        archive.write_bytes(b"fixture archive")
        image = assets / "Igloo-0.1.0-universal.dmg"
        image.write_bytes(b"fixture disk image")
        manifest = release.manifest_for(self.info, "universal", archive)
        release.write_json(assets / "igloo-update.json", manifest)
        (folder / "notes.md").write_text("A reviewed release.")
        review = {"sourceCommit": self.git(self.repo, "rev-parse", "HEAD").stdout.strip(),
                  "files": release.inventory(folder / "source"),
                  "manifestHash": release.digest(assets / "igloo-update.json"),
                  "notesHash": release.digest(folder / "notes.md"), "distributionReady": False,
                  "diskImages": [{"filename": image.name, "sha256": release.digest(image), "size": image.stat().st_size, "runtime": "bundled"}]}
        release.write_json(folder / "review.json", review)
        return folder
    def build_plist(self, cwd, env):
        script = (Path(__file__).resolve().parents[1] / "build.command").read_text()
        start = script.index("\"$IGLOO_PYTHON\" -c '") + len("\"$IGLOO_PYTHON\" -c '")
        end = script.index("' \"$APP_BUNDLE/Contents/Info.plist\" \"$BUILD_RUNTIME\"", start)
        output = cwd / "Info.plist"
        subprocess.run([sys.executable, "-c", script[start:end], str(output), "system"],
                       cwd=cwd, env=env, check=True, capture_output=True)
        return plistlib.loads(output.read_bytes())

    def test_default_build_plist_uses_dev_identity(self):
        env = dict(os.environ)
        env.pop("IGLOO_RELEASE_BUILD", None)
        info = self.build_plist(self.repo, env)
        self.assertEqual((info["CFBundleIdentifier"], info["CFBundleName"], info["CFBundleDisplayName"]),
                         ("local.igloo.dev", "Igloo Dev", "Igloo Dev"))

    def test_prepare_builds_with_release_identity(self):
        notes = self.root / "notes.md"
        notes.write_text("A reviewed release.")
        destination = self.root / "distribution"
        def export(repo, source):
            source.mkdir()
            (source / "release.json").write_text(json.dumps(self.info))
            return "fixture", []
        def build(args, cwd, env):
            self.assertEqual(args[:2], ["/bin/zsh", "build.command"])
            info = self.build_plist(cwd, env)
            self.assertEqual((info["CFBundleIdentifier"], info["CFBundleName"], info["CFBundleDisplayName"]),
                             ("local.igloo", "Igloo", "Igloo"))
            raise RuntimeError("identity checked")
        with patch.object(release, "export_snapshot", side_effect=export), patch.object(release, "run", side_effect=build):
            with self.assertRaisesRegex(RuntimeError, "identity checked"):
                release.prepare(self.repo, destination, notes)

    def test_snapshot_has_current_allowlisted_files_without_private_history(self):
        snapshot = self.exported()
        self.assertEqual(set(release.inventory(snapshot)), {"README.md", "release.json", "public-files.json"})
        self.assertFalse((snapshot / ".git").exists())
        self.assertFalse((snapshot / ".env").exists())
        self.assertEqual((snapshot / "README.md").read_text(), "Public source")
    def test_prepare_refuses_an_allowlisted_private_development_path(self):
        private_name = "docs/private/plan.md"
        private_file = self.repo / private_name
        private_file.parent.mkdir(parents=True)
        private_file.write_text("Private development notes.")
        policy = release.read_json(self.repo / "public-files.json")
        policy["files"].append(private_name)
        release.write_json(self.repo / "public-files.json", policy)
        release.write_json(self.repo / "private-paths.json", {"paths": ["docs/private/*"]})
        self.commit(self.repo)
        notes = self.root / "notes.md"
        notes.write_text("A reviewed release.")
        execute = release.run
        def git_only(args, *positional, **kwargs):
            self.assertEqual(args[0], "git", "Private-path rejection must precede building or network access.")
            return execute(args, *positional, **kwargs)
        destination = self.root / "refused-release"
        with patch.object(release, "run", side_effect=git_only), self.assertRaises(ValueError) as stopped:
            release.prepare(self.repo, destination, notes)
        self.assertEqual(str(stopped.exception), "A private development path was found in the export: " + private_name + ".")
        self.assertFalse((destination / "source").exists())

    def test_stage_refuses_private_paths_in_source_tree_or_review_inventory(self):
        folder = self.prepared()
        private_name = "docs/private/plan.md"
        policy_file = self.repo / "private-paths.json"
        release.write_json(policy_file, {"paths": ["docs/private/*"]})
        original_review = release.read_json(folder / "review.json")
        for location in ["tree", "inventory"]:
            with self.subTest(location=location):
                review = copy.deepcopy(original_review)
                path = folder / "source" / private_name
                if location == "tree":
                    path.parent.mkdir(parents=True)
                    path.write_text("Unreviewed private development notes.")
                else:
                    review["files"][private_name] = "0" * 64
                release.write_json(folder / "review.json", review)
                with patch.object(release, "PRIVATE_PATHS_FILE", policy_file), \
                     patch.object(release, "run") as execute, patch.object(release, "verify_app") as verify, \
                     self.assertRaises(ValueError) as stopped:
                    release.stage(folder, accept=True)
                self.assertEqual(str(stopped.exception), "A private development path was found in the export: " + private_name + ".")
                execute.assert_not_called()
                verify.assert_not_called()
                path.unlink(missing_ok=True)

    def test_clean_export_and_prepared_source_pass_private_path_policy(self):
        policy_file = self.repo / "private-paths.json"
        release.write_json(policy_file, {"paths": ["docs/private/*"]})
        self.commit(self.repo)
        folder = self.prepared()
        self.assertEqual(set(release.inventory(folder / "source")), {"README.md", "release.json", "public-files.json"})
        self.assertFalse((folder / "source/private-paths.json").exists())
        with patch.object(release, "PRIVATE_PATHS_FILE", policy_file):
            self.assertEqual(release.validate_prepared(folder)[0], self.info)

    def test_absent_private_path_policy_allows_export_and_validation(self):
        policy_file = self.repo / "private-paths.json"
        self.assertFalse(policy_file.exists())
        folder = self.prepared()
        with patch.object(release, "PRIVATE_PATHS_FILE", policy_file):
            self.assertEqual(release.validate_prepared(folder)[0], self.info)

    def test_dirty_checkout_is_refused(self):
        (self.repo / "README.md").write_text("unreviewed")
        with self.assertRaisesRegex(ValueError, "Commit"):
            self.exported()
    def test_symlink_in_public_policy_is_refused(self):
        (self.repo / "leak").symlink_to(".env")
        (self.repo / "public-files.json").write_text(json.dumps({"files": ["leak"]}))
        self.commit(self.repo)
        with self.assertRaisesRegex(ValueError, "ordinary"):
            self.exported()
    def test_private_path_and_key_patterns_stop_export(self):
        for content in [b"/Users/" + b"someone/Secrets/", b"ghp_" + b"x" * 40,
                        b"-----BEGIN " + b"PRIVATE KEY-----"]:
            with self.subTest(content=content[:8]):
                with self.assertRaises(ValueError):
                    release.scan("README.md", content)
    def test_path_traversal_and_sensitive_filenames_are_refused(self):
        for name in ["../escape", "/tmp/escape", ".git/config", "nested/.env", "Igloo.app/file"]:
            with self.subTest(name=name), self.assertRaises(ValueError):
                release.safe_name(name)
    def test_manifest_uses_exact_repository_version_size_and_digest(self):
        folder = self.prepared()
        value = release.read_json(folder / "assets/igloo-update.json")
        asset = value["assets"][0]
        self.assertEqual(asset["sha256"], hashlib.sha256(b"fixture archive").hexdigest())
        self.assertEqual(asset["size"], len(b"fixture archive"))
        self.assertEqual(asset["url"], "https://github.com/tm-henningnt/Igloo/releases/download/v0.1.0/Igloo-0.1.0-universal.zip")
        self.assertEqual(release.validate_prepared(folder)[0], self.info)
    @unittest.skipUnless(sys.platform == "darwin", "zip_app uses ditto")
    def test_zip_app_writes_no_bare_macosx_entry(self):
        import zipfile
        with tempfile.TemporaryDirectory(prefix="igloo-zip-app-") as temporary:
            app = Path(temporary) / "Igloo.app"
            (app / "Contents").mkdir(parents=True)
            (app / "Contents/Info.plist").write_bytes(b"plist")
            subprocess.run(["/usr/bin/xattr", "-w", "com.apple.test", "x", app / "Contents/Info.plist"], check=True)
            archive = Path(temporary) / "Igloo.zip"
            release.zip_app(app, archive)
            names = zipfile.ZipFile(archive).namelist()
        self.assertIn("Igloo.app/Contents/Info.plist", names)
        self.assertNotIn("__MACOSX/", names)
        self.assertNotIn("__MACOSX", names)
    def test_changed_source_notes_zip_or_dmg_invalidate_review(self):
        for name in ["source/README.md", "notes.md", "assets/Igloo-0.1.0-universal.zip", "assets/Igloo-0.1.0-universal.dmg"]:
            with self.subTest(name=name):
                folder = self.prepared()
                (folder / name).write_bytes(b"changed")
                with self.assertRaises(ValueError):
                    release.validate_prepared(folder)
                import shutil
                shutil.rmtree(folder)
    def test_unlisted_source_file_invalidates_review(self):
        folder = self.prepared()
        (folder / "source/local.txt").write_text("unreviewed")
        with self.assertRaises(ValueError):
            release.validate_prepared(folder)
    def test_preview_cannot_stage_and_staging_requires_acceptance(self):
        folder = self.prepared()
        with patch.object(release, "run") as execute:
            with self.assertRaisesRegex(ValueError, "accept-publication"):
                release.stage(folder)
            with self.assertRaisesRegex(ValueError, "local preview"):
                release.stage(folder, accept=True)
            execute.assert_not_called()
    def test_unnotarized_staging_requires_preparation_and_explicit_opt_in(self):
        folder = self.prepared()
        with patch.object(release, "run") as execute:
            with self.assertRaisesRegex(ValueError, "local preview"):
                release.stage(folder, accept=True, allow_unnotarized=True)
            execute.assert_not_called()
        review = release.read_json(folder / "review.json")
        review["unnotarizedDistribution"] = True
        release.write_json(folder / "review.json", review)
        with patch.object(release, "run") as execute:
            with self.assertRaisesRegex(ValueError, "allow-unnotarized"):
                release.stage(folder, accept=True)
            execute.assert_not_called()
        with patch.object(release, "run") as execute:
            with self.assertRaisesRegex(ValueError, "release notes"):
                release.stage(folder, accept=True, allow_unnotarized=True)
            execute.assert_not_called()
        (folder / "notes.md").write_text("This release is not notarized. See Privacy & Security to approve its first launch.")
        review["notesHash"] = release.digest(folder / "notes.md")
        release.write_json(folder / "review.json", review)
        remote = {"nameWithOwner": release.PUBLIC_REPO, "isPrivate": False, "defaultBranchRef": {"name": "main"}}
        with patch.object(release, "verify_app") as verify, patch.object(release, "run") as execute, \
             patch.object(release, "output", return_value=json.dumps(remote)):
            execute.return_value.returncode = 0
            with self.assertRaisesRegex(ValueError, "release already exists"):
                release.stage(folder, accept=True, allow_unnotarized=True)
            self.assertFalse(verify.call_args.kwargs["distribution"])
            self.assertFalse(any(call.args[0][0] in {"git", "/usr/bin/xcrun", "/usr/sbin/spctl"}
                                 for call in execute.call_args_list))

    def test_unnotarized_preparation_requires_notice_and_excludes_signing(self):
        notes = self.root / "notes.md"
        notes.write_text("A release without an installation notice.")
        destination = self.root / "distribution"
        with self.assertRaisesRegex(ValueError, "release notes"):
            release.prepare(self.repo, destination, notes, unnotarized=True)
        self.assertFalse(destination.exists())
        notes.write_text("This release is not notarized.")
        with self.assertRaisesRegex(ValueError, "not both"):
            release.prepare(self.repo, destination, notes, identity="fixture", profile="fixture", unnotarized=True)
        self.assertFalse(destination.exists())

    def test_public_repository_begins_with_only_snapshot_history(self):
        public = self.root / "public"
        public.mkdir()
        self.git(public, "init", "-q", "-b", "main")
        self.configure(public)
        release.sync_public(self.exported(), public, "v0.1.0")
        self.assertEqual(self.git(public, "rev-list", "--count", "HEAD").stdout.strip(), "1")
        self.assertEqual(self.git(public, "log", "--format=%s").stdout.strip(), "Igloo 0.1.0")
        self.assertFalse((public / ".env").exists())
        self.assertNotEqual(self.git(public, "rev-parse", "HEAD").stdout, self.git(self.repo, "rev-parse", "HEAD").stdout)
        # Subsequent releases preserve the public snapshot history, without force pushes.
        (self.repo / "README.md").write_text("Next public release")
        self.commit(self.repo)
        release.sync_public(self.exported("next"), public, "v0.1.1")
        self.assertEqual(self.git(public, "rev-list", "--count", "HEAD").stdout.strip(), "2")
    def test_existing_public_tag_or_dirty_tree_is_not_overwritten(self):
        public = self.root / "public"
        public.mkdir()
        self.git(public, "init", "-q", "-b", "main")
        self.configure(public)
        snapshot = self.exported()
        release.sync_public(snapshot, public, "v0.1.0")
        with self.assertRaisesRegex(ValueError, "tag"):
            release.sync_public(snapshot, public, "v0.1.0")
        (public / "README.md").write_text("unsaved public work")
        with self.assertRaisesRegex(ValueError, "clean"):
            release.sync_public(snapshot, public, "v0.1.1")
    def test_both_runtime_variants_share_one_reviewed_manifest(self):
        folder = self.prepared()
        assets = folder / "assets"
        archive = assets / "Igloo-0.1.0-universal-system-python.zip"
        archive.write_bytes(b"system variant")
        image = assets / "Igloo-0.1.0-universal-system-python.dmg"
        image.write_bytes(b"system disk image")
        manifest = release.read_json(assets / "igloo-update.json")
        manifest["assets"].extend(release.manifest_for(self.info, "universal", archive, "system")["assets"])
        release.write_json(assets / "igloo-update.json", manifest)
        review = release.read_json(folder / "review.json")
        review["manifestHash"] = release.digest(assets / "igloo-update.json")
        review["diskImages"].append({"filename": image.name, "sha256": release.digest(image),
                                    "size": image.stat().st_size, "runtime": "system"})
        release.write_json(folder / "review.json", review)
        self.assertEqual(len(release.validate_prepared(folder)[1]["assets"]), 2)

    def test_staging_refuses_private_or_wrong_destination(self):
        for remote in [{"nameWithOwner": release.PUBLIC_REPO, "isPrivate": True},
                       {"nameWithOwner": "someone/else", "isPrivate": False}]:
            folder = self.prepared()
            review = release.read_json(folder / "review.json")
            review["distributionReady"] = True
            release.write_json(folder / "review.json", review)
            with patch.object(release, "verify_app"), patch.object(release, "run") as execute, \
                 patch.object(release, "output", return_value=json.dumps(remote)):
                with self.assertRaisesRegex(ValueError, "already exist and be public"):
                    release.stage(folder, accept=True)
                self.assertFalse(any(call.args[0][0] == "git" for call in execute.call_args_list))
            import shutil
            shutil.rmtree(folder)

    def test_empty_public_repository_can_reach_first_release_check(self):
        folder = self.prepared()
        review = release.read_json(folder / "review.json")
        review["distributionReady"] = True
        release.write_json(folder / "review.json", review)
        for branch in [None, {"name": ""}, {"name": "main"}]:
            remote = {"nameWithOwner": release.PUBLIC_REPO, "isPrivate": False, "defaultBranchRef": branch}
            with self.subTest(branch=branch), patch.object(release, "verify_app"), \
                 patch.object(release, "run") as execute, \
                 patch.object(release, "output", return_value=json.dumps(remote)):
                execute.return_value.returncode = 0
                with self.assertRaisesRegex(ValueError, "release already exists"):
                    release.stage(folder, accept=True)
                self.assertFalse(any(call.args[0][0] == "git" for call in execute.call_args_list))

    def test_stage_uploads_every_asset_and_stops_at_draft(self):
        folder = self.prepared()
        folder = folder.rename(folder.with_name("prepared release"))
        review = release.read_json(folder / "review.json")
        review["distributionReady"] = True
        release.write_json(folder / "review.json", review)
        remote = {"nameWithOwner": release.PUBLIC_REPO, "isPrivate": False, "defaultBranchRef": {"name": "main"}}
        calls = []
        def execute(args, cwd=None, **kwargs):
            args = [str(arg) for arg in args]
            calls.append(args)
            url = "https://github.com/" + release.PUBLIC_REPO + "/releases/tag/untagged-fixture"
            data = (url + "\n").encode() if args[:3] == ["gh", "release", "create"] else b""
            return subprocess.CompletedProcess(args, 1 if args[:3] == ["gh", "release", "view"] else 0, data, b"")
        with patch.object(release, "verify_app") as verify, patch.object(release, "run", side_effect=execute), \
             patch.object(release, "output", side_effect=[json.dumps(remote), "main"]), \
             patch.object(release, "sync_public") as sync, \
             patch.object(release, "public_commit_environment", return_value={"fixture": "identity"}):
            result = release.stage(folder, accept=True)
        verify.assert_called_once()
        self.assertTrue(verify.call_args.kwargs["distribution"])
        self.assertEqual(sync.call_args.kwargs["commit_env"], {"fixture": "identity"})
        push = next(args for args in calls if args[:2] == ["git", "push"])
        self.assertIn("--atomic", push)
        self.assertFalse(any("force" in arg for arg in push))
        create = next(args for args in calls if args[:3] == ["gh", "release", "create"])
        self.assertIn("--draft", create)
        self.assertIn("--verify-tag", create)
        for path in (folder / "assets").iterdir():
            self.assertIn(str(path.resolve()), create)
        self.assertFalse(any(args[:3] == ["gh", "release", "edit"] for args in calls))
        self.assertEqual(result["tag"], "v0.1.0")
        self.assertEqual(result["draftUrl"], "https://github.com/" + release.PUBLIC_REPO + "/releases/tag/untagged-fixture")
        self.assertEqual(result["nextStep"], "Review the draft and request publication approval.")

    def test_publish_only_refuses_without_running_commands(self):
        message = "Publication is disabled here. Stage a draft and use the approved publication process."
        for args in [[], ["unused"], ["unused", "--accept-publication", "--allow-unnotarized"]]:
            stderr = io.StringIO()
            with patch("sys.argv", ["release.py", "publish", *args]), patch.object(release, "run") as execute, \
                 contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit) as stopped:
                release.main()
            self.assertEqual(stopped.exception.code, 1)
            self.assertIn(message, stderr.getvalue())
            execute.assert_not_called()

    def test_public_snapshot_uses_github_noreply_identity(self):
        import os
        public = self.root / "public"
        public.mkdir()
        self.git(public, "init", "-q", "-b", "main")
        self.configure(public)
        with patch.object(release, "output", return_value=json.dumps({"login": "example-user", "id": 12345})), \
             patch.dict(os.environ, {"GIT_AUTHOR_EMAIL": "private@example.invalid", "GIT_COMMITTER_EMAIL": "private@example.invalid"}):
            environment = release.public_commit_environment()
        release.sync_public(self.exported(), public, "v0.1.0", commit_env=environment)
        emails = self.git(public, "log", "-1", "--format=%ae%n%ce").stdout.splitlines()
        self.assertEqual(emails, ["12345+example-user@users.noreply.github.com"] * 2)

    def test_bad_version_metadata_is_refused(self):
        for change in [{"version": "../x"}, {"version": "00.1.0"}, {"build": True},
                       {"repository": "someone/another"}, {"minimumSystemVersion": "new"}]:
            with self.subTest(change=change), self.assertRaises(ValueError):
                release.metadata(dict(self.info, **change))

class PreflightTests(unittest.TestCase):
    git = ReleaseTests.git
    configure = ReleaseTests.configure
    commit = ReleaseTests.commit
    tearDown = ReleaseTests.tearDown
    def setUp(self):
        ReleaseTests.setUp(self)
        (self.repo / "LICENSE").write_text("Fixture source license")
        (self.repo / "release-notes").mkdir()
        self.notes = self.repo / "release-notes/0.1.0.md"
        self.notes.write_text("This app is not notarized. Approve its first launch in Privacy & Security.")
        self.commit(self.repo)
        self.remote = {"nameWithOwner": release.PUBLIC_REPO, "isPrivate": False, "defaultBranchRef": {"name": "main"}}
        self.latest = {"tagName": "v0.0.9", "isDraft": False, "isPrerelease": False}
        self.published = dict(self.info, version="0.0.9", build=12)
        self.environment = {"deployment_branch_policy": {"protected_branches": False, "custom_branch_policies": True}}
        self.policies = [{"branch_policies": [{"type": "branch", "name": "main"}, {"type": "tag", "name": "v*"}]}]
        self.calls = []
        self.real_run = release.run
        self.failures = {}
    def execute(self, args, cwd=None, **kwargs):
        args = [str(arg) for arg in args]
        self.calls.append(args)
        if args[:1] == ["git"]:
            self.assertEqual(args[1:], ["status", "--porcelain"])
            return self.real_run(args, cwd, **kwargs)
        key = tuple(args[:3])
        if key in self.failures:
            return subprocess.CompletedProcess(args, 1, b"", b"unreadable")
        if args[:3] == ["gh", "auth", "status"]:
            value = ""
        elif args[:3] == ["gh", "repo", "view"]:
            value = self.remote
        elif args[:3] == ["gh", "release", "view"]:
            self.assertIn("--repo", args)
            self.assertIn(release.PUBLIC_REPO, args)
            value = self.latest
        elif args[:3] == ["gh", "release", "download"]:
            self.assertIn("igloo-update.json", args)
            self.assertEqual(args[-2:], ["--output", "-"])
            value = self.published
        elif args[:2] == ["gh", "api"]:
            self.assertIn("GET", args)
            endpoint = next(arg for arg in args if arg.startswith("repos/"))
            if "deployment-branch-policies" in endpoint:
                self.assertIn("--paginate", args)
                value = self.policies
            elif endpoint.endswith("github-pages"):
                value = self.environment
            elif "releases?" in endpoint:
                value = ""
            else:
                self.fail("Unexpected API read: " + endpoint)
        else:
            self.fail("Unexpected command: " + repr(args))
        data = value if isinstance(value, str) else json.dumps(value)
        return subprocess.CompletedProcess(args, 0, data.encode(), b"")
    def preflight(self, unnotarized=False):
        self.calls = []
        with patch.object(release, "run", side_effect=self.execute), patch.object(release.shutil, "which", return_value="/fixture/tool"):
            result = release.preflight(self.repo, unnotarized=unnotarized)
        self.assertEqual(result["passed"], all(item["passed"] for item in result["checks"]))
        return {item["check"]: item for item in result["checks"]}
    def test_preflight_passes_without_public_writes(self):
        before = release.inventory(self.repo / "release-notes")
        checks = self.preflight(unnotarized=True)
        self.assertTrue(all(item["passed"] for item in checks.values()), checks)
        self.assertEqual(before, release.inventory(self.repo / "release-notes"))
        self.assertEqual(self.git(self.repo, "status", "--porcelain").stdout, "")
        self.assertFalse(any(args[:3] in [["gh", "release", "create"], ["gh", "release", "edit"]] or
                             args[:2] == ["git", "push"] for args in self.calls))
    def test_preflight_dirty_tree_fails(self):
        (self.repo / "README.md").write_text("unreviewed")
        self.assertFalse(self.preflight()["working tree"]["passed"])
    def test_preflight_requires_both_version_and_build_to_increase(self):
        for change in [{"version": "0.1.0"}, {"version": "0.10.0"}, {"build": 13}, {"build": 14}]:
            with self.subTest(change=change):
                self.published = dict(self.info, version="0.0.9", build=12)
                self.published.update(change)
                self.latest["tagName"] = "v" + self.published["version"]
                self.assertFalse(self.preflight()["release progression"]["passed"])
    def test_preflight_compares_versions_numerically(self):
        release.write_json(self.repo / "release.json", dict(self.info, version="0.10.0", build=14))
        self.published = dict(self.info, version="0.9.0", build=13)
        self.latest["tagName"] = "v0.9.0"
        self.assertTrue(self.preflight()["release progression"]["passed"])
    def test_preflight_invalid_metadata_and_unreadable_manifest_fail(self):
        release.write_json(self.repo / "release.json", dict(self.info, build=True))
        checks = self.preflight()
        self.assertFalse(checks["release metadata"]["passed"])
        self.assertFalse(checks["release progression"]["passed"])
        release.write_json(self.repo / "release.json", self.info)
        self.failures[("gh", "release", "download")] = True
        self.assertFalse(self.preflight()["release progression"]["passed"])
    def test_preflight_rejects_drafts_prereleases_and_mismatched_manifests(self):
        for change in [{"isDraft": True}, {"isPrerelease": True}, {"tagName": "v0.0.8"}]:
            with self.subTest(change=change):
                self.latest = dict(tagName="v0.0.9", isDraft=False, isPrerelease=False)
                self.latest.update(change)
                self.assertFalse(self.preflight()["release progression"]["passed"])
    def test_preflight_missing_notes_fails(self):
        self.notes.unlink()
        self.assertFalse(self.preflight()["release notes"]["passed"])
    def test_preflight_unnotarized_requires_notice(self):
        self.notes.write_text("An ordinary release.")
        self.assertTrue(self.preflight()["release notes"]["passed"])
        self.assertFalse(self.preflight(unnotarized=True)["release notes"]["passed"])
    def test_preflight_private_or_non_main_repository_fails(self):
        for change in [{"isPrivate": True}, {"defaultBranchRef": {"name": "other"}}, {"nameWithOwner": "someone/else"}]:
            with self.subTest(change=change):
                self.remote = {"nameWithOwner": release.PUBLIC_REPO, "isPrivate": False, "defaultBranchRef": {"name": "main"}}
                self.remote.update(change)
                self.assertFalse(self.preflight()["public repository"]["passed"])
    def test_preflight_missing_allowlist_path_or_license_fails(self):
        (self.repo / "README.md").unlink()
        (self.repo / "LICENSE").unlink()
        checks = self.preflight()
        self.assertFalse(checks["public allowlist"]["passed"])
        self.assertFalse(checks["source license"]["passed"])
    def test_preflight_no_release_is_distinct_from_unreadable(self):
        self.failures[("gh", "release", "view")] = True
        check = self.preflight()["release progression"]
        self.assertTrue(check["passed"])
        self.assertIn("no published release", check["message"].lower())
        self.failures[("gh", "api", "--method")] = True
        self.assertFalse(self.preflight()["release progression"]["passed"])
    def test_preflight_pages_rules_and_unreadable_fail_closed(self):
        for policies in [[{"branch_policies": [{"type": "branch", "name": "v*"}, {"type": "tag", "name": "main"}]}],
                         [{"branch_policies": [{"type": "branch", "name": "main"}]}]]:
            self.policies = policies
            self.assertFalse(self.preflight()["Pages environment"]["passed"])
        self.failures[("gh", "api", "--method")] = True
        check = self.preflight()["Pages environment"]
        self.assertFalse(check["passed"])
        self.assertIn("unreadable", check["message"].lower())
    def test_preflight_authentication_and_tools_fail_individually(self):
        self.failures[("gh", "auth", "status")] = True
        with patch.object(release, "run", side_effect=self.execute), patch.object(release.shutil, "which", return_value=None):
            result = release.preflight(self.repo)
        checks = {item["check"]: item for item in result["checks"]}
        self.assertFalse(checks["GitHub authentication"]["passed"])
        for tool in ["git", "gh", "codesign", "xcrun", "python3"]:
            self.assertFalse(checks["tool " + tool]["passed"])
    def test_preflight_cli_prints_every_result_and_fails(self):
        checks = [{"check": "working tree", "passed": False, "message": "dirty"},
                  {"check": "source license", "passed": True, "message": "exists"}]
        stdout = io.StringIO()
        with patch("sys.argv", ["release.py", "preflight", "--unnotarized"]), \
             patch.object(release, "preflight", return_value={"passed": False, "checks": checks}) as check, \
             contextlib.redirect_stdout(stdout), self.assertRaises(SystemExit) as stopped:
            release.main()
        self.assertEqual(stopped.exception.code, 1)
        self.assertTrue(check.call_args.kwargs["unnotarized"])
        self.assertIn("FAIL working tree: dirty", stdout.getvalue())
        self.assertIn("PASS source license: exists", stdout.getvalue())

class UpdatePolicyTests(unittest.TestCase):
    def test_production_swift_update_policy(self):
        checks = r'''
func rejects(_ f: () throws -> Void) { do { try f(); fatalError("Expected refusal") } catch {} }
let payload = Data("verified app download".utf8)
let sha = SHA256.hash(data: payload).map { String(format: "%02x", $0) }.joined()
let filename = "Igloo-0.1.0-universal.zip"
let url = UpdatePolicy.assetURL(tag: "v0.1.0", filename: filename)
let asset = UpdateAsset(architecture: "universal", filename: filename, url: url, size: Int64(payload.count), sha256: sha)
var manifest = UpdateManifest(schemaVersion: 1, repository: UpdatePolicy.repository, tag: "v0.1.0", version: "0.1.0", build: 13,
                              minimumSystemVersion: "14.0", assets: [asset])
var release = GitHubRelease(tag_name: "v0.1.0", html_url: UpdatePolicy.releaseURL("v0.1.0"), body: "Notes", draft: false,
                            prerelease: false, assets: [ReleaseAsset(name: filename, size: asset.size, browser_download_url: url)])
func validate(_ m: UpdateManifest, _ r: GitHubRelease = release, version: String = "0.0.9", build: Int = 12,
              arch: String = "arm64", system: String = "14.0", runtime: String = "bundled") throws -> AvailableUpdate? {
    try UpdatePolicy.validate(m, release: r, architecture: arch, currentVersion: version, currentBuild: build, systemVersion: system, runtime: runtime)
}
precondition(try AppVersion("0.10.0") > AppVersion("0.9.0"))
precondition(try AppVersion("0.1") == AppVersion("0.1.0"))
rejects { _ = try AppVersion("0.1.0-beta") }
rejects { _ = try AppVersion("00.1.0") }
precondition(try validate(manifest)?.asset.architecture == "universal")
precondition(try validate(manifest, version: "0.1.0", build: 13) == nil)
precondition(try validate(manifest, version: "0.1.1", build: 1) == nil)
precondition(try validate(manifest, version: "0.1.0", build: 12) != nil)
var next = manifest
next.version = "0.1.1"; next.tag = "v0.1.1"; next.build = 14
next.assets[0].filename = "Igloo-0.1.1-universal.zip"
next.assets[0].url = UpdatePolicy.assetURL(tag: next.tag, filename: next.assets[0].filename)
var nextRelease = release
nextRelease.tag_name = next.tag; nextRelease.html_url = UpdatePolicy.releaseURL(next.tag)
nextRelease.assets = [ReleaseAsset(name: next.assets[0].filename, size: asset.size, browser_download_url: next.assets[0].url)]
precondition(try validate(next, nextRelease, version: "0.1.0", build: 13)?.manifest.version == "0.1.1")
precondition(try validate(next, nextRelease, version: "0.1.1", build: 14) == nil)
rejects { _ = try validate(manifest, system: "13.0") }
var bad = manifest; bad.repository = "someone/else"
rejects { _ = try validate(bad) }
bad = manifest; bad.assets[0].url = "https://example.com/app.zip"
rejects { _ = try validate(bad) }
bad = manifest; bad.assets[0].sha256 = "bad"
rejects { _ = try validate(bad) }
bad = manifest; bad.assets.append(asset)
rejects { _ = try validate(bad) }
bad = manifest; bad.assets[0].architecture = "arm64"; bad.assets[0].filename = "Igloo-0.1.0-arm64.zip"
rejects { _ = try validate(bad, arch: "x86_64") }
var wrongRelease = release; wrongRelease.prerelease = true
rejects { _ = try validate(manifest, wrongRelease) }
wrongRelease = release; wrongRelease.draft = true
rejects { _ = try validate(manifest, wrongRelease) }
wrongRelease = release; wrongRelease.tag_name = "v0.1.1"
rejects { _ = try validate(manifest, wrongRelease) }
wrongRelease = release; wrongRelease.assets[0].size += 1
rejects { _ = try validate(manifest, wrongRelease) }
var dual = manifest
var systemAsset = asset
systemAsset.runtime = "system"
systemAsset.filename = "Igloo-0.1.0-universal-system-python.zip"
systemAsset.url = UpdatePolicy.assetURL(tag: "v0.1.0", filename: systemAsset.filename)
dual.assets.append(systemAsset)
var dualRelease = release
dualRelease.assets.append(ReleaseAsset(name: systemAsset.filename, size: systemAsset.size, browser_download_url: systemAsset.url))
precondition(try validate(dual, dualRelease)?.asset.runtime == "bundled")
precondition(try validate(dual, dualRelease, runtime: "system")?.asset.runtime == "system")
rejects { _ = try validate(manifest, runtime: "system") }
precondition(!UpdatePolicy.safeTransport(URL(string: "http://github.com/app")!))
precondition(!UpdatePolicy.safeTransport(URL(string: "https://github.com.evil.invalid/app")!))
precondition(!UpdatePolicy.safeTransport(URL(string: "https://user:pass@github.com/app")!))
precondition(UpdatePolicy.safeTransport(URL(string: "https://release-assets.githubusercontent.com/file")!))
let temp = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
try payload.write(to: temp)
try UpdatePolicy.verify(file: temp, asset: asset)
try Data("changed".utf8).write(to: temp)
rejects { try UpdatePolicy.verify(file: temp, asset: asset) }
try FileManager.default.removeItem(at: temp)
print("Production Swift update policy checks passed")
'''
        # precondition uses a nonthrowing autoclosure.
        checks = checks.replace("precondition(try ", "precondition(try! ")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            executable = compile_update_check(root, checks)
            result = subprocess.run([executable], capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)

if __name__ == "__main__":
    unittest.main()
