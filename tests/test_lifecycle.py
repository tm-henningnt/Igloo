"""Lifecycle tests use only disposable Git repositories, files and local bare remotes."""
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import unittest
from unittest.mock import patch

from tests.test_organizer import GuardTests, o
import lifecycle as life


class LifecycleTests(unittest.TestCase):
    setUpBase = GuardTests.setUp
    tearDownBase = GuardTests.tearDown
    git = GuardTests.git
    make_bare = GuardTests.make_bare
    bare_ref = GuardTests.bare_ref

    def setUp(self):
        self.setUpBase()
        life.configure(o)
        def snapshot():
            trees = [{"path": w["worktree"], "exists": Path(w["worktree"]).exists()}
                     for w in o.worktree_list(str(self.repo)) if isinstance(w.get("worktree"), str)]
            return {"projects": [{"git": True, "path": str(self.repo), "worktrees": trees}]}
        self.extra = [patch.object(o, "snapshot", side_effect=snapshot),
                      patch.object(o, "CONFIG", dict(o.CONFIG, freezerBackupRoot="",
                                                    freezerBackupCloudAcceptedRoot="")),
                      patch.object(o, "process_cwds", return_value=[]),
                      patch.object(life, "open_files", return_value=None)]
        for p in self.extra: p.start()
        self.bare = self.make_bare()
        self.git("remote", "add", "origin", str(self.bare))
        with open(self.repo / ".gitignore", "a") as f:
            f.write("\n.env\nlocal/\n")
        self.git("add", ".gitignore")
        self.git("commit", "-qm", "local preservation rules")

    def tearDown(self):
        for p in reversed(self.extra): p.stop()
        self.tearDownBase()

    def freeze(self):
        preview = life.preview_freeze(str(self.repo), "origin")
        life.execute_freeze(preview["id"])
        return preview["id"], life.archive_record(preview["id"])

    def enable_backup(self):
        home = self.root / "home"
        home.mkdir()
        home_patch = patch.object(Path, "home", return_value=home)
        home_patch.start()
        self.extra.append(home_patch)
        destination = home / "Backup"
        destination.mkdir()
        o.CONFIG["freezerBackupRoot"] = str(destination)
        return destination

    def backup_copy(self, root, identifier):
        return root / "Igloo-Freezer-Backup" / identifier

    def test_preserve_creates_verified_private_backup(self):
        destination = self.enable_backup()
        identifier, (location, _) = self.freeze()
        copy = self.backup_copy(destination, identifier)
        for name in ("manifest.json", "source.bundle", "git-metadata.tar.gz", "local-files.tar.gz"):
            self.assertEqual(life.digest(copy / name), life.digest(location / name))
            self.assertEqual((copy / name).stat().st_mode & 0o777, 0o600)
        self.assertEqual(copy.stat().st_mode & 0o777, 0o700)
        self.assertEqual(copy.parent.stat().st_mode & 0o777, 0o700)
        status = life.backup_status()
        self.assertTrue(status["archives"][0]["backedUp"])
        self.assertFalse(status["archives"][0]["missing"])
        self.assertIsNotNone(status["archives"][0]["lastVerified"])
        self.assertTrue(life.vault_entries()[0]["backup"]["backedUp"])
        before = {p.name: p.read_bytes() for p in copy.iterdir() if p.name != "backup.json"}
        life.backup_run()
        self.assertEqual(before, {p.name: p.read_bytes() for p in copy.iterdir() if p.name != "backup.json"})

    def test_missing_backup_folder_never_blocks_preserve(self):
        destination = self.enable_backup()
        destination.rmdir()
        review = life.preview_freeze(str(self.repo), "origin")
        result = life.execute_freeze(review["id"])
        self.assertEqual(result["state"], "ready")
        self.assertTrue((self.repo / ".git").is_dir())
        self.assertFalse(destination.exists())
        self.assertEqual(result["backup"]["status"], "Not backed up")
        row = life.backup_status()["archives"][0]
        self.assertFalse(row["backedUp"])
        self.assertTrue(row["missing"])
        self.assertIn("missing", row["reason"])

    def test_unwritable_backup_never_blocks_preserve(self):
        self.enable_backup()
        review = life.preview_freeze(str(self.repo), "origin")
        with patch.object(life, "backup_run", side_effect=PermissionError("fixture")):
            result = life.execute_freeze(review["id"])
        self.assertEqual(result["state"], "ready")
        self.assertFalse(result["backup"]["backedUp"])
        self.assertTrue(result["backup"]["reason"])

    def test_backup_verify_detects_corruption_and_run_does_not_replace_copy(self):
        destination = self.enable_backup()
        identifier, _ = self.freeze()
        copy = self.backup_copy(destination, identifier)
        (copy / "local-files.tar.gz").write_bytes(b"corrupted fixture")
        with self.assertRaisesRegex(o.Refusal, "verification failed"):
            life.backup_verify()
        row = life.backup_status()["archives"][0]
        self.assertFalse(row["backedUp"])
        self.assertFalse(row["missing"])
        self.assertTrue(row["lastVerified"])
        with self.assertRaises(o.Refusal):
            life.backup_run()
        self.assertEqual((copy / "local-files.tar.gz").read_bytes(), b"corrupted fixture")

    def test_backup_verify_detects_source_corruption(self):
        self.enable_backup()
        _, (location, _) = self.freeze()
        (location / "source.bundle").write_bytes(b"changed source archive")
        with self.assertRaises(o.Refusal):
            life.backup_verify()
        self.assertFalse(life.backup_status()["archives"][0]["backedUp"])

    def test_backup_verify_records_time_and_missing_files(self):
        destination = self.enable_backup()
        identifier, _ = self.freeze()
        verified = "2030-01-02T03:04:05+00:00"
        with patch.object(o, "now", return_value=verified):
            life.backup_verify()
        self.assertEqual(life.backup_status()["archives"][0]["lastVerified"], verified)
        (self.backup_copy(destination, identifier) / "source.bundle").unlink()
        row = life.backup_status()["archives"][0]
        self.assertFalse(row["backedUp"])
        self.assertTrue(row["missing"])

    def test_backup_refuses_linked_recovery_files_and_invalid_receipts(self):
        destination = self.enable_backup()
        o.CONFIG["freezerBackupRoot"] = ""
        identifier, (location, _) = self.freeze()
        payload = (location / "source.bundle").read_bytes()
        target = self.root / "outside-copy"
        target.write_bytes(payload)
        (location / "source.bundle").unlink()
        (location / "source.bundle").symlink_to(target)
        o.CONFIG["freezerBackupRoot"] = str(destination)
        with self.assertRaises(o.Refusal):
            life.backup_run()
        self.assertFalse(self.backup_copy(destination, identifier).exists())
        (location / "source.bundle").unlink()
        (location / "source.bundle").write_bytes(payload)
        life.backup_run()
        (self.backup_copy(destination, identifier) / "backup.json").write_text('["invalid"]')
        self.assertFalse(life.backup_status()["archives"][0]["backedUp"])
        with self.assertRaises(o.Refusal):
            life.backup_verify()
        self.assertEqual(target.read_bytes(), payload)

    def test_backup_verify_command_fails_with_json_error(self):
        destination = self.enable_backup()
        identifier, _ = self.freeze()
        (self.backup_copy(destination, identifier) / "source.bundle").write_bytes(b"changed")
        output = io.StringIO()
        with patch.object(sys, "argv", ["organizer.py", "backup-verify"]), \
             patch.object(o, "lifecycle_module", return_value=life), patch.object(sys, "stdout", output):
            code = o.main()
        self.assertEqual(code, 1)
        self.assertIn("verification failed", json.loads(output.getvalue())["error"])

    def test_backup_after_thaw_is_an_orphan_and_removal_is_explicit(self):
        destination = self.enable_backup()
        identifier, _ = self.freeze()
        with self.assertRaisesRegex(o.Refusal, "still in the Freezer"):
            life.backup_remove(identifier)
        life.freeze_retire(identifier)
        # A lifecycle-only manifest change does not invalidate recovery files.
        life.backup_verify()
        life.rehydrate(identifier, offline=True)
        copy = self.backup_copy(destination, identifier)
        self.assertTrue(copy.is_dir())
        status = life.backup_status()
        self.assertEqual(status["archives"], [])
        self.assertEqual([r["id"] for r in status["orphans"]], [identifier])
        life.backup_run()
        life.backup_verify()
        self.assertTrue(copy.is_dir())
        life.backup_remove(identifier)
        self.assertFalse(copy.exists())
        self.assertTrue((self.repo / "source.txt").is_file())

    def test_removing_source_archive_leaves_backup_for_explicit_removal(self):
        destination = self.enable_backup()
        identifier, (location, _) = self.freeze()
        import shutil
        shutil.rmtree(location)
        self.assertEqual(life.backup_status()["orphans"][0]["id"], identifier)
        life.backup_verify()
        life.backup_remove(identifier)
        self.assertFalse(self.backup_copy(destination, identifier).exists())

    def test_cloud_backup_requires_matching_destination_acceptance(self):
        self.enable_backup()
        home = Path.home()
        for relative in ("Library/Mobile Documents/com~apple~CloudDocs/Backup", "Library/CloudStorage/Fixture/Backup"):
            cloud = home / relative
            cloud.mkdir(parents=True)
            o.CONFIG["freezerBackupRoot"] = str(cloud)
            with self.assertRaisesRegex(o.Refusal, "unencrypted"):
                life.backup_run()
            self.assertEqual(list(cloud.iterdir()), [])
            o.CONFIG["freezerBackupCloudAcceptedRoot"] = str(cloud.resolve()).casefold()
            life.backup_run()
            o.CONFIG["freezerBackupCloudAcceptedRoot"] = ""
            alias = home / "alias"
            alias.symlink_to(cloud, target_is_directory=True)
            o.CONFIG["freezerBackupRoot"] = str(alias)
            with self.assertRaises(o.Refusal):
                life.backup_run()
            alias.unlink()

    def test_firmlink_cloud_paths_require_destination_consent(self):
        # Model macOS's /System/Volumes/Data firmlink without touching a real home.
        home = Path("/Users/demo")
        prefix = "/System/Volumes/Data"
        for relative in ("Library/CloudStorage/Fixture/Backup", "Library/Mobile Documents/Fixture/Backup"):
            canonical = home / relative
            alternate = Path(prefix + str(canonical))
            for home_path, target in ((home, alternate), (Path(prefix + str(home)), canonical)):
                with self.subTest(home=home_path, target=target), \
                     patch.object(Path, "home", return_value=home_path), \
                     patch.object(Path, "resolve", lambda path, *args, **kwargs: path):
                    o.CONFIG["freezerBackupRoot"] = str(target)
                    o.CONFIG["freezerBackupCloudAcceptedRoot"] = ""
                    destination = life.backup_destination()
                    self.assertTrue(destination["cloud"])
                    self.assertFalse(destination["cloudAccepted"])
                    with self.assertRaisesRegex(o.Refusal, "unencrypted"):
                        life.backup_base()
                    o.CONFIG["freezerBackupCloudAcceptedRoot"] = str(target).casefold()
                    self.assertTrue(life.backup_destination()["cloudAccepted"])

    def test_mixed_case_cloud_paths_require_acceptance(self):
        self.enable_backup()
        for relative in ("library/cloudstorage/Fixture/Backup", "Library/mobile documents/Fixture/Backup"):
            cloud = Path.home() / relative
            cloud.mkdir(parents=True)
            o.CONFIG["freezerBackupRoot"] = str(cloud)
            with self.assertRaisesRegex(o.Refusal, "unencrypted"):
                life.backup_run()
            status = life.backup_status()
            self.assertTrue(status["cloud"])
            self.assertFalse(status["cloudAccepted"])
            self.assertEqual(status["acceptanceRoot"], str(cloud.resolve()).casefold())
            self.assertEqual(list(cloud.iterdir()), [])
            o.CONFIG["freezerBackupCloudAcceptedRoot"] = status["acceptanceRoot"]
            self.assertTrue(life.backup_run()["cloudAccepted"])

    def test_cloud_acceptance_does_not_follow_destination_changes(self):
        self.enable_backup()
        first = Path.home() / "Library/CloudStorage/Fixture/First"
        second = first.parent / "Second"
        first.mkdir(parents=True)
        second.mkdir()
        o.CONFIG["freezerBackupRoot"] = str(first)
        o.CONFIG["freezerBackupCloudAcceptedRoot"] = str(first.resolve()).casefold()
        life.backup_run()
        o.CONFIG["freezerBackupRoot"] = str(second)
        with self.assertRaisesRegex(o.Refusal, "unencrypted"):
            life.backup_run()
        self.assertEqual(list(second.iterdir()), [])
        o.CONFIG["freezerBackupCloudAcceptedRoot"] = str(second.resolve()).casefold()
        life.backup_run()

    def test_cloud_detection_resolves_destination_and_cloud_folder_aliases(self):
        self.enable_backup()
        home = Path.home()
        provider = home / "provider"
        (provider / "Fixture/Backup").mkdir(parents=True)
        library = home / "Library"
        library.mkdir()
        (library / "CloudStorage").symlink_to(provider, target_is_directory=True)
        alias = home / "alias"
        alias.symlink_to(library / "CloudStorage/Fixture", target_is_directory=True)
        for cloud in (provider / "Fixture/Backup", alias / "Backup"):
            o.CONFIG["freezerBackupRoot"] = str(cloud)
            o.CONFIG["freezerBackupCloudAcceptedRoot"] = ""
            with self.assertRaisesRegex(o.Refusal, "unencrypted"):
                life.backup_run()
            o.CONFIG["freezerBackupCloudAcceptedRoot"] = str(cloud.resolve()).casefold()
            life.backup_run()

    def test_vault_list_retains_archives_when_backup_status_fails(self):
        self.enable_backup()
        identifier, _ = self.freeze()
        with patch.object(life, "backup_status", side_effect=PermissionError("fixture")):
            rows = life.vault_entries()
        self.assertEqual([row["id"] for row in rows], [identifier])
        self.assertTrue(rows[0]["backup"]["error"])
        self.assertFalse(rows[0]["backup"]["backedUp"])

    def test_vault_list_reports_unreadable_freezer_as_status_error(self):
        self.enable_backup()
        self.freeze()
        original_iterdir = Path.iterdir
        def unreadable(path):
            if path == o.VAULT:
                raise PermissionError("fixture")
            return original_iterdir(path)
        with patch.object(Path, "iterdir", unreadable):
            result = life.dispatch("vault-list", [])
        self.assertEqual(result["archives"], [])
        self.assertTrue(result["backup"]["error"])
        self.assertFalse(result["backup"]["available"])

    def test_vault_list_reports_linked_freezer_as_status_error(self):
        self.enable_backup()
        target = self.root / "freezer-target"
        target.mkdir()
        o.VAULT.parent.mkdir(parents=True)
        o.VAULT.symlink_to(target, target_is_directory=True)
        result = life.dispatch("vault-list", [])
        self.assertEqual(result["archives"], [])
        self.assertTrue(result["backup"]["error"])

    def test_backup_run_saves_key_errors_per_archive(self):
        destination = self.enable_backup()
        o.CONFIG["freezerBackupRoot"] = ""
        identifier, _ = self.freeze()
        o.CONFIG["freezerBackupRoot"] = str(destination)
        with patch.object(life, "write_backup_copy", side_effect=KeyError("fixture")):
            with self.assertRaisesRegex(o.Refusal, "verification failed"):
                life.backup_run()
        self.assertIn(identifier, life.backup_failures())
        self.assertFalse(life.backup_status()["archives"][0]["backedUp"])

    def test_backup_refuses_overlap_symlinks_and_traversal(self):
        destination = self.enable_backup()
        identifier, (location, _) = self.freeze()
        for root in (o.VAULT, location):
            o.CONFIG["freezerBackupRoot"] = str(root)
            with self.assertRaises(o.Refusal):
                life.backup_run()
        o.CONFIG["freezerBackupRoot"] = str(destination)
        for identifier in ("../outside", "", "a" * 31):
            with self.assertRaises(o.Refusal):
                life.backup_remove(identifier)
        managed = destination / "Igloo-Freezer-Backup"
        import shutil
        shutil.rmtree(managed)
        managed.symlink_to(o.VAULT, target_is_directory=True)
        with self.assertRaises(o.Refusal):
            life.backup_run()

    def test_backup_copy_is_not_published_when_checksum_or_rename_fails(self):
        destination = self.enable_backup()
        o.CONFIG["freezerBackupRoot"] = ""
        identifier, _ = self.freeze()
        o.CONFIG["freezerBackupRoot"] = str(destination)
        for target in ("backup_hash", "rename_exclusive"):
            effect = (lambda path: "0" * 64) if target == "backup_hash" else OSError("fixture")
            with self.subTest(target=target), patch.object(life, target, side_effect=effect):
                with self.assertRaises(o.Refusal):
                    life.backup_run()
            self.assertFalse(self.backup_copy(destination, identifier).exists())
            self.assertEqual(list((destination / "Igloo-Freezer-Backup").iterdir()), [])
        life.backup_run()
        self.assertTrue(self.backup_copy(destination, identifier).is_dir())

    def test_backup_commands_dispatch_and_reject_unexpected_arguments(self):
        self.enable_backup()
        for command in ("backup-run", "backup-verify", "backup-status"):
            self.assertIn(command, life.COMMANDS)
            self.assertIn("archives", life.dispatch(command, []))
            with self.assertRaises(o.Refusal):
                life.dispatch(command, ["unexpected"])
        self.assertIn("backup-remove", life.COMMANDS)
        with self.assertRaises(o.Refusal):
            life.dispatch("backup-remove", [])

    def test_vault_creates_nested_private_storage_on_first_use(self):
        location = life.vault()
        self.assertEqual(location, o.VAULT)
        self.assertEqual(location.stat().st_mode & 0o777, 0o700)
        self.assertEqual(location.parent.stat().st_mode & 0o777, 0o700)

    def test_commit_only_reviewed_files_and_never_ignored_env(self):
        (self.repo / "source.txt").write_text("reviewed source")
        (self.repo / "another.txt").write_text("leave uncommitted")
        (self.repo / ".env").write_text("synthetic private fixture")
        p = life.preview_commit(str(self.repo), ["source.txt"])
        life.execute_commit(p["id"], "Reviewed source change")
        self.assertEqual(self.git("show", "HEAD:source.txt"), "reviewed source")
        self.assertEqual(self.git("status", "--porcelain", "--", "another.txt"), "?? another.txt")
        self.assertNotIn(".env", self.git("ls-files"))

    def test_commit_filename_secrets_are_refused(self):
        (self.repo / "credentials.json").write_text("{}")
        with self.assertRaisesRegex(o.Refusal, "stay local"):
            life.preview_commit(str(self.repo), ["credentials.json"])

    def test_commit_rejects_stale_content(self):
        (self.repo / "source.txt").write_text("review")
        p = life.preview_commit(str(self.repo), ["source.txt"])
        old = self.git("rev-parse", "HEAD")
        (self.repo / "source.txt").write_text("unreviewed later")
        with self.assertRaisesRegex(o.Refusal, "changed"):
            life.execute_commit(p["id"], "Should not commit")
        self.assertEqual(self.git("rev-parse", "HEAD"), old)

    def test_commit_respects_unreviewed_staging(self):
        (self.repo / "source.txt").write_text("review")
        (self.repo / "other.txt").write_text("staged elsewhere")
        self.git("add", "other.txt")
        with self.assertRaisesRegex(o.Refusal, "already staged"):
            life.preview_commit(str(self.repo), ["source.txt"])

    def test_connect_remote_does_not_replace_existing(self):
        with self.assertRaisesRegex(o.Refusal, "already exists"):
            life.connect_remote(str(self.repo), "origin", str(self.bare))
        life.connect_remote(str(self.repo), "backup", str(self.bare))
        self.assertEqual(self.git("remote", "get-url", "backup"), str(self.bare))

    def test_new_github_repository_is_private_and_owner_is_explicit(self):
        with patch.object(life, "gh", return_value=(0, "")) as github:
            life.create_private_repo(str(self.repo), "personal-owner/folder-independent", "new-remote")
        args = github.call_args.args
        self.assertIn("personal-owner/folder-independent", args)
        self.assertIn("--private", args)
        self.assertNotIn("--push", args)
        self.assertNotIn("--public", args)

    def test_dirty_checkout_blocks_freeze(self):
        (self.repo / "source.txt").write_text("uncommitted")
        with self.assertRaisesRegex(o.Refusal, "Commit or resolve"):
            life.preview_freeze(str(self.repo), "origin")
        self.assertTrue((self.repo / "source.txt").exists())

    def test_empty_repository_needs_files_before_an_initial_commit(self):
        empty = self.root / "empty-project"
        empty.mkdir()
        subprocess.run(["/usr/bin/git", "-C", str(empty), "init", "-q"], check=True,
                       capture_output=True, env=o.ENV)
        snapshot = {"projects": [{"git": True, "path": str(empty), "worktrees": []}]}
        with patch.object(o, "snapshot", return_value=snapshot):
            rows = life.freeze_blockers(str(empty))["blockers"]
            by_code = {row["code"]: row for row in rows}
            self.assertIn("no-files", by_code)
            self.assertEqual(by_code["no-files"]["message"], "Add files to this project, then commit them.")
            self.assertIsNone(by_code["no-files"]["fix"])
            self.assertNotIn("no-commit", by_code)
            self.assertNotIn("changed-source", by_code)
            with self.assertRaisesRegex(o.Refusal, "Add files to this project"):
                life.preview_freeze(str(empty), "origin")

            (empty / ".env").write_text("fixture only")
            rows = life.freeze_blockers(str(empty))["blockers"]
            self.assertIn("no-files", {row["code"] for row in rows})
            self.assertFalse(any(row["fix"] == "commit" for row in rows))

            (empty / "source.txt").write_text("fixture source")
            rows = life.freeze_blockers(str(empty))["blockers"]
            by_code = {row["code"]: row for row in rows}
            self.assertIn("no-commit", by_code)
            self.assertEqual(by_code["no-commit"]["fix"], "commit")
            self.assertNotIn("no-files", by_code)
            self.assertNotIn("changed-source", by_code)
            with self.assertRaisesRegex(o.Refusal, "initial commit"):
                life.preview_freeze(str(empty), "origin")

    def test_cheap_blockers_match_review_refusals(self):
        def check(code, fix):
            blockers = life.freeze_blockers(str(self.repo))["blockers"]
            matches = [b for b in blockers if b["code"] == code]
            self.assertEqual(len(matches), 1, blockers)
            self.assertEqual(matches[0]["fix"], fix)
            with self.assertRaises(o.Refusal) as stopped:
                life.preview_freeze(str(self.repo), "origin")
            self.assertEqual(str(stopped.exception), matches[0]["message"])
        self.assertEqual(life.freeze_blockers(str(self.repo))["blockers"], [])
        self.git("remote", "remove", "origin")
        check("no-remote", "connect-remote")
        self.git("remote", "add", "origin", str(self.bare))
        (self.repo / "source.txt").write_text("changed source")
        check("changed-source", "commit")
        self.git("checkout", "--", "source.txt")
        (self.repo / "new-source.txt").write_text("untracked source")
        check("changed-source", "commit")
        (self.repo / "new-source.txt").unlink()
        with patch.object(o, "process_cwds", return_value=[{"path": str(self.repo / "source.txt"), "pid": "fixture"}]):
            check("open-processes", None)
        with patch.object(o, "APP", self.repo):
            check("own-source", None)
        self.git("checkout", "--orphan", "unborn")
        self.git("rm", "-rf", ".")
        self.cache.joinpath("package.js").unlink()
        self.cache.rmdir()
        check("no-files", None)

    def test_blockers_reuse_snapshot_facts_without_hashing_or_remote_queries(self):
        states = [o.git_state(str(self.repo))]
        with patch.object(life, "capture", side_effect=AssertionError("must not hash")), \
             patch.object(o, "run_git", side_effect=AssertionError("must not query Git")), \
             patch.object(life, "open_files", side_effect=AssertionError("must not walk open files")):
            self.assertEqual(life.blockers_for_states(states, []), [])

    def test_freeze_review_still_runs_full_open_file_guard(self):
        with patch.object(life, "open_files", side_effect=o.Refusal("Close the open file.")) as full_check:
            with self.assertRaisesRegex(o.Refusal, "Close the open file"):
                life.preview_freeze(str(self.repo), "origin")
        full_check.assert_called_once_with([self.repo])

    def test_review_alone_finds_no_publishable_refs(self):
        self.git("branch", "-m", "backup/private-only")
        self.assertEqual(life.freeze_blockers(str(self.repo))["blockers"], [])
        with self.assertRaisesRegex(o.Refusal, "No ordinary branches or tags"):
            life.preview_freeze(str(self.repo), "origin")

    def test_project_freeze_uses_one_acceptance_and_records_frozen(self):
        review = life.preview_freeze(str(self.repo), "origin", "freeze")
        self.assertEqual(review["mode"], "freeze")
        result = life.execute_freeze(review["id"])
        self.assertEqual(result["state"], "frozen")
        self.assertFalse((self.repo / ".git").exists())
        self.assertEqual(life.archive_record(review["id"])[1]["state"], "frozen")

    def test_preserve_and_freeze_outcomes_match_observed_operations(self):
        for mode in ("preserve", "freeze"):
            with self.subTest(mode=mode):
                before = o.physical_size(self.repo)
                review = life.preview_freeze(str(self.repo), "origin", mode)
                estimate = review["outcome"]
                self.assertEqual(estimate["checkoutsRemoved"]["count"], int(mode == "freeze"))
                self.assertEqual(estimate["archivesWritten"]["count"], 1)
                self.assertEqual(estimate["archivesWritten"]["location"], str(o.VAULT))
                self.assertIsNone(estimate["archivesWritten"]["paths"])
                self.assertGreaterEqual(estimate["bytesFreedEstimate"], 0)
                self.assertGreaterEqual(estimate["bytesUsedEstimate"], 0)
                if mode == "preserve":
                    self.assertEqual(estimate["bytesFreedEstimate"], 0)
                    self.assertGreater(estimate["bytesUsedEstimate"], 0)
                self.assertEqual(estimate["refsPushed"], [dict(ref=r["ref"], sha=r["sha"], destination=str(self.bare))
                                                        for r in review["publish"]])
                result = life.execute_freeze(review["id"])["outcome"]
                for field in ("checkoutsRemoved", "refsPushed", "movedToTrash"):
                    self.assertEqual(result[field], estimate[field])
                for field in ("count", "location"):
                    self.assertEqual(result["archivesWritten"][field], estimate["archivesWritten"][field])
                self.assertEqual(result["archivesWritten"]["paths"], [str(o.VAULT / review["id"])])
                self.assertTrue(result["observed"])
                location = o.VAULT / review["id"]
                observed = (before - o.physical_size(self.repo) if mode == "freeze" else 0) - o.physical_size(location)
                self.assertEqual(result["bytesFreed"], observed)
                self.assertEqual(result["bytesFreedEstimate"], estimate["bytesFreedEstimate"])
                self.assertEqual(result["bytesUsedEstimate"], estimate["bytesUsedEstimate"])

    def test_legacy_preserved_manifest_without_allocation_fields_can_freeze(self):
        identifier, (location, manifest) = self.freeze()
        for role in manifest["capture"]["roles"]:
            for entry in role["entries"].values():
                entry.pop("allocatedSize", None)
        for entry in manifest["capture"]["git"].values():
            entry.pop("allocatedSize", None)
        manifest.pop("outcome", None)
        o.save(location / "manifest.json", manifest)
        life.freeze_retire(identifier)
        self.assertEqual(life.archive_record(identifier)[1]["state"], "frozen")
        self.assertFalse((self.repo / ".git").exists())
        life.rehydrate(identifier, offline=True)
        self.assertEqual((self.repo / "source.txt").read_text(), "source\n")

    def test_failed_preserve_reports_only_observed_archive_effects(self):
        review = life.preview_freeze(str(self.repo), "origin", "freeze")
        with patch.object(life, "prove_recovery", side_effect=o.Refusal("Recovery verification failed.")):
            with self.assertRaises(o.Refusal) as stopped:
                life.execute_freeze(review["id"])
        outcome = stopped.exception.outcome
        self.assertTrue(outcome["observed"])
        self.assertEqual(outcome["checkoutsRemoved"]["count"], 0)
        self.assertEqual(outcome["archivesWritten"]["count"], 1)
        self.assertEqual(outcome["refsPushed"], [])
        self.assertEqual(life.archive_record(review["id"])[1]["state"], "incomplete")
        self.assertTrue((self.repo / ".git").is_dir())

    def test_preserve_review_cannot_be_executed_as_freeze(self):
        review = life.preview_freeze(str(self.repo), "origin", "preserve")
        with self.assertRaisesRegex(o.Refusal, "mode"):
            life.execute_freeze(review["id"], "freeze")
        self.assertTrue((self.repo / ".git").is_dir())
        self.assertFalse((o.VAULT / review["id"]).exists())
        result = life.execute_freeze(review["id"], "preserve")
        self.assertEqual(result["state"], "ready")
        self.assertTrue((self.repo / ".git").is_dir())

    def test_freeze_removal_stops_leave_preserved_entry_and_reason(self):
        original = life.freeze_retire
        def verify_failure(identifier):
            with patch.object(life, "verify_archives", side_effect=o.Refusal("Recovery verification failed.")):
                return original(identifier)
        def process_open(identifier):
            with patch.object(life, "open_files", side_effect=o.Refusal(life.BLOCKERS["open-processes"][0])):
                return original(identifier)
        def file_changed(identifier):
            (self.repo / ".env").write_text("new work after review")
            return original(identifier)
        for failure, reason in ((verify_failure, "verification failed"), (process_open, "Close the terminals"),
                                (file_changed, "changed")):
            with self.subTest(failure=failure.__name__):
                review = life.preview_freeze(str(self.repo), "origin", "freeze")
                with patch.object(life, "freeze_retire", side_effect=failure):
                    result = life.execute_freeze(review["id"])
                manifest = life.archive_record(review["id"])[1]
                self.assertEqual(result["state"], "ready")
                self.assertEqual(manifest["state"], "ready")
                self.assertIn(reason, result["message"])
                self.assertIn(reason, manifest["message"])
                self.assertTrue((self.repo / ".git").is_dir())

    def test_freeze_archives_ignored_files_preserves_stashes_and_private_refs(self):
        (self.repo / ".env").write_text("synthetic fixture values")
        (self.repo / "local").mkdir()
        (self.repo / "local" / "data.bin").write_bytes(b"\x00\x01payload")
        (self.repo / "local" / "empty").mkdir()
        (self.repo / "source.txt").write_text("stashed work")
        self.git("stash", "push", "-qm", "synthetic stash")
        sha = self.git("rev-parse", "HEAD")
        self.git("branch", "backup/private-local", sha)
        identifier, (location, manifest) = self.freeze()
        self.assertEqual(manifest["state"], "ready")
        self.assertTrue((self.repo / ".git").is_dir())
        self.assertEqual(self.bare_ref(self.bare, "refs/heads/" + self.git("branch", "--show-current")), sha)
        self.assertIsNone(self.bare_ref(self.bare, "refs/heads/backup/private-local"))
        self.assertTrue(any(r["ref"] == "refs/stash" for r in manifest["localRefs"]))
        life.freeze_retire(identifier)
        self.assertEqual({p.name for p in self.repo.iterdir()}, {life.MARKER})
        life.rehydrate(identifier, offline=True)
        self.assertEqual((self.repo / ".env").read_text(), "synthetic fixture values")
        self.assertEqual((self.repo / "local" / "data.bin").read_bytes(), b"\x00\x01payload")
        self.assertTrue((self.repo / "local" / "empty").is_dir())
        self.assertFalse((self.repo / "node_modules").exists())
        self.assertEqual(self.git("stash", "list").count("synthetic stash"), 1)
        self.assertEqual(self.git("rev-parse", "backup/private-local"), sha)

    def test_rehydrate_clones_remote_and_restores_local_overlay(self):
        (self.repo / ".env").write_text("local fixture")
        identifier, _ = self.freeze()
        life.freeze_retire(identifier)
        life.rehydrate(identifier, offline=False)
        self.assertEqual((self.repo / "source.txt").read_text(), "source\n")
        self.assertEqual((self.repo / ".env").read_text(), "local fixture")
        self.assertEqual(self.git("status", "--porcelain"), "")

    def test_changed_local_file_after_archive_blocks_removal(self):
        (self.repo / ".env").write_text("before")
        identifier, _ = self.freeze()
        (self.repo / ".env").write_text("new work after archiving")
        with self.assertRaisesRegex(o.Refusal, "changed"):
            life.freeze_retire(identifier)
        self.assertTrue((self.repo / ".git").is_dir())
        self.assertEqual((self.repo / ".env").read_text(), "new work after archiving")

    def test_corrupt_archive_blocks_removal(self):
        identifier, (location, _) = self.freeze()
        with open(location / "local-files.tar.gz", "ab") as f: f.write(b"corrupt")
        with self.assertRaisesRegex(o.Refusal, "changed"):
            life.freeze_retire(identifier)
        self.assertTrue((self.repo / ".git").is_dir())

    def test_restore_never_overwrites_an_occupied_source_path(self):
        identifier, _ = self.freeze()
        life.freeze_retire(identifier)
        (self.repo / "new-file.txt").write_text("keep this")
        with self.assertRaisesRegex(o.Refusal, "occupied"):
            life.rehydrate(identifier, offline=True)
        self.assertEqual((self.repo / "new-file.txt").read_text(), "keep this")

    def test_cache_environment_file_is_kept_while_dependency_is_excluded(self):
        (self.cache / ".env.local").write_text("synthetic cache-local settings")
        identifier, _ = self.freeze()
        life.freeze_retire(identifier)
        life.rehydrate(identifier, offline=True)
        self.assertEqual((self.cache / ".env.local").read_text(), "synthetic cache-local settings")
        self.assertFalse((self.cache / "package.js").exists())

    def test_linked_worktree_roundtrip(self):
        linked = self.root / "linked"
        self.git("worktree", "add", "-qb", "feature", str(linked))
        (linked / ".env").write_text("linked local fixture")
        identifier, _ = self.freeze()
        life.freeze_retire(identifier)
        self.assertFalse(linked.exists())
        life.rehydrate(identifier, offline=True)
        self.assertEqual((linked / ".env").read_text(), "linked local fixture")
        result = subprocess.run(["/usr/bin/git", "-C", str(linked), "branch", "--show-current"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), "feature")
        self.assertIn(str(linked), self.git("worktree", "list", "--porcelain"))

    def test_nested_worktree_roundtrip(self):
        linked = self.repo / ".claude" / "worktrees" / "feature"
        linked.parent.mkdir(parents=True)
        self.git("worktree", "add", "-qb", "feature", str(linked))
        (linked / ".env").write_text("nested local fixture")
        identifier, _ = self.freeze()
        life.freeze_retire(identifier)
        life.rehydrate(identifier, offline=True)
        self.assertEqual((linked / ".env").read_text(), "nested local fixture")
        result = subprocess.run(["/usr/bin/git", "-C", str(linked), "status", "--porcelain"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), "")

    def test_archive_extraction_refuses_parent_traversal(self):
        archive = self.root / "unsafe.tar"
        with tarfile.open(archive, "w") as t:
            entry = tarfile.TarInfo("../escape")
            entry.size = 1
            t.addfile(entry, io.BytesIO(b"x"))
        with self.assertRaisesRegex(o.Refusal, "unsafe"):
            life.extract_safe(archive, self.root / "stage")
        self.assertFalse((self.root / "escape").exists())

    def test_archive_extraction_never_writes_through_a_symlink(self):
        archive = self.root / "unsafe.tar"
        outside = self.root / "outside"
        outside.mkdir()
        with tarfile.open(archive, "w") as t:
            entry = tarfile.TarInfo("link")
            entry.type = tarfile.SYMTYPE
            entry.linkname = str(outside)
            t.addfile(entry)
            child = tarfile.TarInfo("link/escape")
            child.size = 1
            t.addfile(child, io.BytesIO(b"x"))
        with self.assertRaisesRegex(o.Refusal, "traverse"):
            life.extract_safe(archive, self.root / "stage")
        self.assertFalse((outside / "escape").exists())

    def test_frozen_archive_survives_missing_remote_with_offline_recovery(self):
        identifier, _ = self.freeze()
        life.freeze_retire(identifier)
        self.bare.rename(self.root / "unavailable-remote.git")
        with self.assertRaisesRegex(o.Refusal, "Remote clone failed"):
            life.rehydrate(identifier, offline=False)
        self.assertTrue((self.repo / life.MARKER).is_file())
        life.rehydrate(identifier, offline=True)
        self.assertTrue((self.repo / "source.txt").is_file())


    def test_exclusive_rename_never_replaces_an_existing_directory(self):
        source, target = self.root / "rename-source", self.root / "rename-target"
        source.mkdir(); target.mkdir()
        (source / "keep.txt").write_text("keep")
        with self.assertRaises(OSError):
            life.rename_exclusive(source, target)
        self.assertTrue((source / "keep.txt").exists())
        self.assertTrue(target.is_dir())

    def test_changed_file_during_retirement_is_rolled_back_with_new_content(self):
        (self.repo / ".env").write_text("before")
        identifier, _ = self.freeze()
        original_rename = life.rename_exclusive
        def late_edit(source, destination):
            original_rename(source, destination)
            if Path(source) == self.repo:
                (Path(destination) / ".env").write_text("late user work")
        with patch.object(life, "rename_exclusive", side_effect=late_edit):
            with self.assertRaisesRegex(o.Refusal, "changed"):
                life.freeze_retire(identifier)
        self.assertTrue((self.repo / ".git").is_dir())
        self.assertEqual((self.repo / ".env").read_text(), "late user work")

    def test_interrupted_hydration_can_finish_without_overwriting_restored_roots(self):
        linked = self.root / "linked"
        self.git("worktree", "add", "-qb", "feature", str(linked))
        (linked / ".env").write_text("linked fixture")
        identifier, _ = self.freeze()
        life.freeze_retire(identifier)
        original_rename = life.rename_exclusive
        def fail_second_root(source, destination):
            if Path(destination) == linked:
                raise OSError("fixture interruption")
            return original_rename(source, destination)
        with patch.object(life, "rename_exclusive", side_effect=fail_second_root):
            with self.assertRaises(OSError):
                life.rehydrate(identifier, offline=True)
        self.assertTrue((self.repo / ".git").is_dir())
        self.assertFalse(linked.exists())
        self.assertEqual(life.archive_record(identifier)[1]["state"], "rehydrating")
        before_inode = (self.repo / "source.txt").stat().st_ino
        life.rehydrate(identifier, offline=True)
        self.assertEqual((self.repo / "source.txt").stat().st_ino, before_inode)
        self.assertEqual((linked / ".env").read_text(), "linked fixture")

    def test_interrupted_hydration_preserves_new_edits_and_stops(self):
        linked = self.root / "linked"
        self.git("worktree", "add", "-qb", "feature", str(linked))
        identifier, _ = self.freeze()
        life.freeze_retire(identifier)
        original_rename = life.rename_exclusive
        def fail_second_root(source, destination):
            if Path(destination) == linked:
                raise OSError("fixture interruption")
            return original_rename(source, destination)
        with patch.object(life, "rename_exclusive", side_effect=fail_second_root):
            with self.assertRaises(OSError):
                life.rehydrate(identifier, offline=True)
        (self.repo / "source.txt").write_text("new work after interruption")
        with self.assertRaisesRegex(o.Refusal, "verification failed"):
            life.rehydrate(identifier, offline=True)
        self.assertEqual((self.repo / "source.txt").read_text(), "new work after interruption")

    def test_sparse_checkout_missing_files_remain_missing_on_restore(self):
        extra = self.repo / "large" / "tracked.txt"
        extra.parent.mkdir()
        extra.write_text("tracked outside sparse selection")
        self.git("add", "large/tracked.txt")
        self.git("commit", "-qm", "sparse fixture")
        self.git("sparse-checkout", "set", "--no-cone", "/source.txt", "/.gitignore")
        self.assertFalse(extra.exists())
        identifier, _ = self.freeze()
        life.freeze_retire(identifier)
        life.rehydrate(identifier, offline=True)
        self.assertTrue((self.repo / "source.txt").is_file())
        self.assertFalse(extra.exists())
        self.assertEqual(self.git("status", "--porcelain"), "")

    def test_partial_removal_keeps_leftovers_and_can_restore_from_archive(self):
        identifier, _ = self.freeze()
        original_remove = life.shutil.rmtree
        def stop_removal(path, *args, **kwargs):
            if Path(path).name.startswith(".freeze-removing-"):
                raise OSError("fixture interrupted removal")
            return original_remove(path, *args, **kwargs)
        with patch.object(life.shutil, "rmtree", side_effect=stop_removal):
            with self.assertRaises(OSError):
                life.freeze_retire(identifier)
        manifest = life.archive_record(identifier)[1]
        self.assertEqual(manifest["state"], "removing")
        leftover = Path(manifest["moves"][0]["temporary"])
        self.assertTrue(leftover.is_dir())
        life.rehydrate(identifier, offline=True)
        self.assertTrue((self.repo / "source.txt").is_file())
        self.assertTrue(leftover.is_dir())

    def test_detached_linked_worktree_history_is_kept_local(self):
        linked = self.root / "linked"
        self.git("worktree", "add", "--detach", "-q", str(linked))
        subprocess.run(["/usr/bin/git", "-C", str(linked), "-c", "user.name=Fixture",
                        "-c", "user.email=fixture@example.invalid", "commit", "--allow-empty", "-qm", "detached fixture"], check=True)
        detached = subprocess.run(["/usr/bin/git", "-C", str(linked), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
        identifier, (_, manifest) = self.freeze()
        self.assertTrue(any(r["sha"] == detached and r["ref"].startswith("detached:") for r in manifest["localRefs"]))
        life.freeze_retire(identifier)
        life.rehydrate(identifier, offline=True)
        restored = subprocess.run(["/usr/bin/git", "-C", str(linked), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
        self.assertEqual(restored, detached)

    def test_local_vault_and_archives_have_private_permissions(self):
        identifier, (location, manifest) = self.freeze()
        self.assertEqual(location.stat().st_mode & 0o777, 0o700)
        self.assertEqual(location.parent.stat().st_mode & 0o777, 0o700)
        for name in manifest["checksums"]:
            self.assertEqual((location / name).stat().st_mode & 0o777, 0o600)

    def init_organizer_source_fixture(self):
        for args in [("init", "-q"), ("config", "user.name", "Fixture"),
                     ("config", "user.email", "fixture@example.invalid")]:
            subprocess.run(["/usr/bin/git", "-C", str(self.app), *args], check=True)
        (self.app / "app.py").write_text("print('fixture')\n")

    def test_organizer_source_can_make_initial_reviewed_commit(self):
        self.init_organizer_source_fixture()
        plan = life.preview_commit(str(self.app), ["app.py"])
        life.execute_commit(plan["id"], "Initial fixture source")
        rc, data = o.run_git(self.app, "show", "HEAD:app.py")
        self.assertEqual(rc, 0)
        self.assertEqual(data, "print('fixture')\n")

    def test_organizer_source_can_connect_and_push_itself(self):
        self.init_organizer_source_fixture()
        plan = life.preview_commit(str(self.app), ["app.py"])
        life.execute_commit(plan["id"], "Initial fixture source")
        life.connect_remote(str(self.app), "origin", str(self.bare))
        _, branch = o.run_git(self.app, "branch", "--show-current")
        _, sha = o.run_git(self.app, "rev-parse", "HEAD")
        o.push_branch(str(self.app), branch.strip(), sha.strip(), "origin", str(self.bare))
        self.assertEqual(self.bare_ref(self.bare, "refs/heads/" + branch.strip()), sha.strip())

    def test_organizer_source_stays_protected_from_freeze(self):
        self.init_organizer_source_fixture()
        with self.assertRaisesRegex(o.Refusal, "does not freeze its own source"):
            life.preview_freeze(str(self.app), "origin")

    def test_organizer_source_stays_protected_from_cache_cleanup(self):
        self.init_organizer_source_fixture()
        cache = self.app / "node_modules"
        cache.mkdir()
        with self.assertRaises(o.Refusal):
            o.validate_cache(str(cache), str(self.app), check_processes=False)

    def test_github_url_matching_handles_https_ssh_and_case(self):
        self.assertEqual(life.github_identity("https://github.com/Owner/Repo.git"), "owner/repo")
        self.assertEqual(life.github_identity("git@github.com:OWNER/repo.git"), "owner/repo")
        self.assertEqual(life.github_identity("git@github.com:owner/repo.git.git"), "owner/repo.git")
        self.assertIsNone(life.github_identity("https://example.invalid/owner/repo"))

    def test_repository_listing_marks_local_https_and_ssh_clones(self):
        values = [{"name":"sample","nameWithOwner":"personal/sample","url":"https://github.com/personal/sample",
                   "isPrivate":True,"isArchived":False,"description":"fixture","pushedAt":""},
                  {"name":"other","nameWithOwner":"personal/other","url":"https://github.com/personal/other",
                   "isPrivate":False,"isArchived":False,"description":"","pushedAt":""}]
        snapshot = {"projects":[{"git":True,"path":str(self.repo),"remotes":[{"url":"git@github.com:Personal/Sample.git"}]}]}
        with patch.object(life,"gh",return_value=(0,json.dumps(values))), patch.object(o,"snapshot",return_value=snapshot):
            listing = life.github_repositories("personal")
        records = {r["slug"]:r for r in listing["repositories"]}
        self.assertTrue(records["personal/sample"]["alreadyLocal"])
        self.assertFalse(records["personal/other"]["alreadyLocal"])

    def test_clone_preview_refuses_existing_and_duplicate_destinations(self):
        (self.root/"occupied").mkdir()
        with self.assertRaisesRegex(o.Refusal,"already exists"):
            life.preview_clones(str(self.root),["personal/occupied"])
        with self.assertRaisesRegex(o.Refusal,"same folder name"):
            life.preview_clones(str(self.root),["one/sample","two/Sample"])

    def clone_fixture_git(self, path, *args, **kwargs):
        original = self.original_lifecycle_git
        if args and args[0]=="clone":
            # Replace network transport only; still execute a real clone and identity read-back.
            target = Path(args[-1])
            rc, out = original(path,"clone","--no-hardlinks","--",str(self.bare),str(target))
            if not rc:
                original(target,"remote","set-url","origin",args[-2])
            return rc,out
        return original(path,*args,**kwargs)

    def test_reviewed_multi_clone_creates_distinct_new_checkouts(self):
        self.git("push","-q","origin",self.git("branch","--show-current"))
        review = life.preview_clones(str(self.root),["personal/first","personal/second"])
        self.original_lifecycle_git = life.git
        with patch.object(life,"git",side_effect=self.clone_fixture_git):
            reply = life.execute_clones(review["id"])
        self.assertTrue(all(r["ok"] for r in reply["clones"]))
        for name in ("first","second"):
            self.assertTrue((self.root/name/".git").is_dir())
            self.assertEqual((self.root/name/"source.txt").read_text(),"source\n")
        self.assertEqual(len(o.load(o.STATE/"clones.json")),2)

    def test_successful_clone_survives_auxiliary_index_write_failure(self):
        self.git("push","-q","origin",self.git("branch","--show-current"))
        review=life.preview_clones(str(self.root),["personal/sample"])
        self.original_lifecycle_git=life.git
        save=o.save
        def guarded_save(path,value):
            if Path(path).name=="clones.json": raise OSError("fixture write failure")
            return save(path,value)
        with patch.object(life,"git",side_effect=self.clone_fixture_git), patch.object(o,"save",side_effect=guarded_save):
            reply=life.execute_clones(review["id"])
        self.assertTrue(reply["clones"][0]["ok"])
        self.assertTrue((self.root/"sample/.git").is_dir())
        self.assertIn("index update failed",reply["clones"][0]["message"])

    def test_clone_destination_race_preserves_existing_files(self):
        review = life.preview_clones(str(self.root),["personal/sample"])
        target = self.root/"sample";target.mkdir()
        (target/"keep.txt").write_text("new user work")
        with patch.object(life,"git") as git:
            reply = life.execute_clones(review["id"])
        git.assert_not_called()
        self.assertFalse(reply["clones"][0]["ok"])
        self.assertEqual((target/"keep.txt").read_text(),"new user work")

    def test_failed_clone_does_not_prevent_other_selected_repositories(self):
        self.git("push","-q","origin",self.git("branch","--show-current"))
        review = life.preview_clones(str(self.root),["personal/failing","personal/working"])
        self.original_lifecycle_git=life.git
        def transport(path,*args,**kwargs):
            if args and args[0]=="clone" and "failing" in args[-2]:
                return 128,""
            return self.clone_fixture_git(path,*args,**kwargs)
        with patch.object(life,"git",side_effect=transport):
            reply=life.execute_clones(review["id"])
        self.assertEqual([r["ok"] for r in reply["clones"]],[False,True])
        self.assertFalse((self.root/"failing").exists())
        self.assertTrue((self.root/"working/.git").is_dir())

    def test_clone_review_expiration_stops_before_network(self):
        review=life.preview_clones(str(self.root),["personal/sample"])
        p=life.plan_path("clone",review["id"]); value=o.load(p)
        value["created"]="2000-01-01T00:00:00+00:00";o.save(p,value)
        with patch.object(life,"git") as git, self.assertRaisesRegex(o.Refusal,"expired"):
            life.execute_clones(review["id"])
        git.assert_not_called()

    def test_clone_cannot_target_app_or_archive_storage(self):
        o.VAULT.mkdir(parents=True)
        for path in (self.app,o.VAULT):
            with self.subTest(path=path),self.assertRaises(o.Refusal):
                life.preview_clones(str(path),["personal/sample"])

if __name__ == "__main__":
    unittest.main()
