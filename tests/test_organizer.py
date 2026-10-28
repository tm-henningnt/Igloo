import importlib.util
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("organizer", Path(__file__).resolve().parents[1] / "backend/organizer.py")
o = importlib.util.module_from_spec(spec)
spec.loader.exec_module(o)

class GuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.app = self.root / "Igloo"
        self.app.mkdir()
        self.repo = self.root / "project"
        self.repo.mkdir()
        self.trash = self.root / ".Trash"
        self.trash.mkdir()
        self.patches = [patch.object(o, "ROOT", self.root), patch.object(o, "APP", self.app),
                        patch.object(o, "STATE", self.app / "state"),
                        patch.object(o, "VAULT", self.root / ".igloo/vault")]
        for p in self.patches: p.start()
        self.original_checked = o.checked_path
        self.checked_patch = patch.object(o, "checked_path", side_effect=lambda value, root=None, allow_missing=False:
                                         self.original_checked(value, self.root, allow_missing))
        self.checked_patch.start()
        self.git("init", "-q")
        self.git("config", "user.name", "Fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        (self.repo / ".gitignore").write_text("node_modules/\n.cache/\n")
        (self.repo / "source.txt").write_text("source\n")
        self.git("add", ".")
        self.git("commit", "-qm", "fixture")
        self.cache = self.repo / "node_modules"
        self.cache.mkdir()
        (self.cache / "package.js").write_text("fixture dependency\n")

    def tearDown(self):
        self.checked_patch.stop()
        for p in reversed(self.patches): p.stop()
        self.temp.cleanup()

    def git(self, *args):
        r = subprocess.run(["/usr/bin/git", "-C", str(self.repo), *args],
                           capture_output=True, text=True, env=o.ENV)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.strip()

    def validate(self):
        return o.validate_cache(str(self.cache), str(self.repo), check_processes=False)

    def test_outcome_summary_defaults_and_combines_thaw_fields(self):
        default = o.outcome_summary()
        self.assertEqual(default["checkoutsRestored"], {"count": 0, "paths": []})
        self.assertEqual(default["archivesUsed"], {"count": 0, "location": str(o.VAULT), "paths": []})
        self.assertEqual(default["refsRestored"], [])

        restored_ref = {"path": str(self.repo), "ref": "refs/heads/main", "sha": "abcdef1"}
        thaw = o.outcome_summary(checkouts_restored=[str(self.repo)], archives_used=[str(self.app / "archive")],
                                  refs_restored=[restored_ref])
        combined = o.combine_outcomes([thaw])
        self.assertEqual(combined["checkoutsRestored"], {"count": 1, "paths": [str(self.repo)]})
        self.assertEqual(combined["archivesUsed"], {
            "count": 1, "location": str(o.VAULT), "paths": [str(self.app / "archive")]
        })
        self.assertEqual(combined["refsRestored"], [restored_ref])

    def fake_snapshot(self):
        return {"projects": [{"candidates": [{"id": str(self.cache), "owner": str(self.repo)}]}]}

    def plan(self):
        value = self.validate()
        p = {"id": "a" * 32, "created": o.now(), "items": [value], "blocked": [], "totalSize": value["size"]}
        o.save(o.STATE / "plans" / (p["id"] + ".json"), p)
        return p

    def test_ignored_dependency_is_valid(self):
        self.assertEqual(self.validate()["path"], str(self.cache))

    def test_physical_size_accepts_serialized_paths_without_following_symlinks(self):
        # Lifecycle manifests round-trip checkout roots through JSON strings.
        location = self.root / "roots.json"
        o.save(location, [str(self.repo)])
        roots = o.load(location)
        self.assertEqual(o.physical_size(roots[0]), o.physical_size(self.repo))
        link = self.root / "project-link"
        link.symlink_to(self.repo, target_is_directory=True)
        self.assertEqual(o.physical_size(str(link)), 0)
        self.assertEqual(o.physical_size(link), 0)

    def test_snapshot_process_scan_reads_only_working_directories(self):
        result = subprocess.CompletedProcess([], 0, stdout="pfixture\ncFixture\nn" + str(self.repo) + "\n", stderr="")
        with patch.object(o.subprocess, "run", return_value=result) as observed:
            rows = o.process_cwds()
        command = observed.call_args.args[0]
        self.assertIn("-a", command)
        self.assertEqual(command[command.index("-d") + 1], "cwd")
        self.assertNotIn("+D", command)
        self.assertEqual(rows, [{"pid": "fixture", "command": "Fixture", "path": str(self.repo)}])

    def test_unknown_snapshot_activity_does_not_claim_an_open_process(self):
        state = o.git_state(str(self.repo))
        project = o.project_record([state], o.observe_files(self.root), None, live=False)
        self.assertFalse(project["activityKnown"])
        self.assertFalse(project["active"])
        self.assertNotIn("open-processes", [b["code"] for b in project["blockers"]])

    def test_old_decisions_migrate_once_to_hold_and_keep_notes(self):
        old = {name: {"decision": name, "note": "Note for " + name, "at": "saved time"}
               for name in ("Keep", "Undecided", "Review", "Archive", "Retire")}
        o.save(o.STATE / "decisions.json", old)
        migrated = o.project_settings()
        for name, row in migrated.items():
            self.assertEqual(row, {"hold": name == "Keep", "note": "Note for " + name, "at": "saved time"})
        with patch.object(o, "save", side_effect=AssertionError("must migrate only once")):
            self.assertEqual(o.project_settings(), migrated)

    def test_hold_and_note_commands_preserve_each_other(self):
        o.save(o.STATE / "snapshot.json", {"root": str(self.root), "projects": [
            {"id": str(self.repo), "path": str(self.repo), "git": True, "decision": "Keep", "candidates": []}]})
        o.set_hold(str(self.repo), "true")
        o.set_note(str(self.repo), "Keep my note")
        project = o.snapshot()["projects"][0]
        self.assertTrue(project["hold"])
        self.assertEqual(project["note"], "Keep my note")
        self.assertNotIn("decision", project)
        o.set_hold(str(self.repo), "false")
        self.assertFalse(o.snapshot()["projects"][0]["hold"])
        self.assertEqual(o.project_settings()[str(self.repo)]["note"], "Keep my note")
        with self.assertRaises(o.Refusal):
            o.set_hold(str(self.repo), "yes")

    def test_snapshot_does_not_expose_deprecated_decision(self):
        o.save(o.STATE / "snapshot.json", {"root": str(self.root), "projects": [
            {"id": str(self.repo), "path": str(self.repo), "git": True, "decision": "Keep", "candidates": []}]})
        project = o.snapshot()["projects"][0]
        self.assertNotIn("decision", project)
        self.assertIn("hold", project)

    def test_deprecated_decide_command_is_not_accepted(self):
        o.save(o.STATE / "snapshot.json", {"root": str(self.root), "projects": [
            {"id": str(self.repo), "path": str(self.repo), "git": True, "candidates": []}]})
        with patch.object(sys, "argv", ["organizer", "decide", str(self.repo), "Keep", "note"]), \
             patch("sys.stderr", new_callable=io.StringIO), patch("builtins.print"):
            with self.assertRaises(SystemExit) as stopped:
                o.main()
        self.assertEqual(stopped.exception.code, 2)

    def test_backend_help_hides_deprecated_decide_alias(self):
        output = io.StringIO()
        with patch.object(sys, "argv", ["organizer", "--help"]), patch.object(sys, "stdout", output):
            with self.assertRaises(SystemExit) as stopped:
                o.main()
        self.assertEqual(stopped.exception.code, 0)
        self.assertNotRegex(output.getvalue(), r"\bdecide\b")
        self.assertRegex(output.getvalue(), r"\bhold\b")

    def test_tracked_content_refuses_even_when_ignored(self):
        self.git("add", "-f", "node_modules/package.js")
        with self.assertRaisesRegex(o.Refusal, "Tracked"):
            self.validate()

    def test_environment_file_inside_dependency_refuses(self):
        (self.cache / ".env.local").write_text("synthetic fixture\n")
        with self.assertRaisesRegex(o.Refusal, "environment"):
            self.validate()

    def test_nested_repo_inside_dependency_refuses(self):
        nested = self.cache / "handmade"
        nested.mkdir()
        subprocess.run(["/usr/bin/git", "-C", str(nested), "init", "-q"], check=True)
        with self.assertRaisesRegex(o.Refusal, "nested Git"):
            self.validate()

    def test_symlink_component_refuses(self):
        alias = self.root / "alias"
        alias.symlink_to(self.repo, target_is_directory=True)
        with self.assertRaisesRegex(o.Refusal, "Symbolic"):
            o.checked_path(str(alias / "node_modules"))

    def test_traversal_outside_and_app_paths_refuse(self):
        for path in (str(self.root), "/tmp", str(self.app / "state"), str(self.repo / ".." / "project")):
            with self.subTest(path=path), self.assertRaises(o.Refusal):
                o.checked_path(path, allow_missing=True)

    def test_active_session_refuses(self):
        with patch.object(o, "process_cwds", return_value=[{"path": str(self.repo), "command": "fixture"}]):
            with self.assertRaisesRegex(o.Refusal, "active"):
                o.validate_cache(str(self.cache), str(self.repo))

    def test_snapshot_flag_cannot_allow_unknown_path(self):
        with patch.object(o, "snapshot", return_value=self.fake_snapshot()):
            plan = o.preview_cleanup([str(self.root / "unknown")])
        self.assertFalse(plan["items"])
        self.assertEqual(len(plan["blocked"]), 1)

    def test_stale_nested_file_change_refuses(self):
        plan = self.plan()
        (self.cache / "package.js").write_text("changed after preview\n")
        with patch.object(o, "process_cwds", return_value=[]), patch.object(Path, "home", return_value=self.root):
            result = o.execute_cleanup(plan["id"])
        self.assertFalse(result["results"][0]["ok"])
        self.assertTrue(self.cache.is_dir())

    def test_expired_preview_refuses(self):
        plan = self.plan()
        plan["created"] = "2000-01-01T00:00:00+00:00"
        o.save(o.STATE / "plans" / (plan["id"] + ".json"), plan)
        with self.assertRaisesRegex(o.Refusal, "expired"):
            o.execute_cleanup(plan["id"])

    def test_trash_roundtrip_preserves_contents_and_history(self):
        plan = self.plan()
        allocated = o.physical_size(self.cache)
        with patch.object(o, "process_cwds", return_value=[]), patch.object(Path, "home", return_value=self.root):
            result = o.execute_cleanup(plan["id"])
            self.assertTrue(result["results"][0]["ok"])
            self.assertEqual(result["results"][0]["size"], allocated)
            self.assertFalse(self.cache.exists())
            history = o.load(o.STATE / "trash.json")
            self.assertEqual(history[0]["status"], "trashed")
            self.assertTrue(Path(history[0]["destination"]).is_dir())
            o.restore(history[0]["id"])
        self.assertEqual((self.cache / "package.js").read_text(), "fixture dependency\n")
        self.assertEqual(o.load(o.STATE / "trash.json")[0]["status"], "restored")

    def test_cleanup_space_report_tracks_allocated_bytes_and_restore(self):
        plan = self.plan()
        allocated = o.physical_size(self.cache)
        with patch.object(o, "process_cwds", return_value=[]), patch.object(Path, "home", return_value=self.root):
            result = o.execute_cleanup(plan["id"])
            self.assertEqual(result["outcome"]["observedSpace"], {
                "freedBytes": 0, "inTrashBytes": allocated, "restoredBytes": 0})
            row = next(row for row in o.space_report()["runs"] if row["id"] == plan["id"])
            self.assertEqual(row["observed"], {
                "freedBytes": 0, "inTrashBytes": allocated, "restoredBytes": 0})
            item = o.load(o.STATE / "trash.json")[0]
            self.assertEqual(item["runID"], plan["id"])
            o.restore(item["id"])
        row = next(row for row in o.space_report()["runs"] if row["id"] == plan["id"])
        self.assertEqual(row["observed"], {
            "freedBytes": 0, "inTrashBytes": 0, "restoredBytes": allocated})

    def test_restore_never_overwrites_replacement(self):
        plan = self.plan()
        with patch.object(o, "process_cwds", return_value=[]), patch.object(Path, "home", return_value=self.root):
            o.execute_cleanup(plan["id"])
            history = o.load(o.STATE / "trash.json")
            self.cache.mkdir()
            (self.cache / "new.txt").write_text("keep")
            with self.assertRaisesRegex(o.Refusal, "already exists"):
                o.restore(history[0]["id"])
        self.assertEqual((self.cache / "new.txt").read_text(), "keep")

    def test_push_rejects_changed_sha_before_contacting_remote(self):
        with patch.object(o, "require_project", return_value={}):
            with self.assertRaisesRegex(o.Refusal, "changed since review"):
                o.push_branch(str(self.repo), self.git("branch", "--show-current"), "0" * 40, "origin", "fixture")

    def test_private_history_branch_cannot_be_pushed(self):
        with patch.object(o, "require_project", return_value={}):
            with self.assertRaisesRegex(o.Refusal, "private"):
                o.push_branch(str(self.repo), "archive/private-history", "0" * 40, "origin", "fixture")

    def test_live_branch_coverage_and_new_commit(self):
        bare = self.root / "remote.git"
        subprocess.run(["/usr/bin/git", "init", "-q", "--bare", str(bare)], check=True)
        self.git("remote", "add", "origin", str(bare))
        branch = self.git("branch", "--show-current")
        self.git("push", "-q", "origin", branch)
        state = o.git_state(str(self.repo))
        o.verify_remote(state)
        self.assertEqual(state["branches"][0]["uncovered"], 0)
        (self.repo / "source.txt").write_text("changed\n")
        self.git("add", "source.txt")
        self.git("commit", "-qm", "local only")
        state = o.git_state(str(self.repo))
        o.verify_remote(state)
        self.assertEqual(state["branches"][0]["uncovered"], 1)

    def test_scanner_finds_worktrees_skips_fixtures_and_reports_config_filename(self):
        fixture = self.repo / ".worker" / "fixture"
        fixture.mkdir(parents=True)
        (fixture / ".git").mkdir()
        tenant = self.repo / ".orchestration" / "local"
        tenant.mkdir(parents=True)
        (tenant / "tenant.json").write_text("{}")
        worktree = self.repo / ".claude" / "worktrees" / "review"
        worktree.parent.mkdir(parents=True)
        self.git("worktree", "add", "-qb", "review", str(worktree))
        obs = o.observe_files(self.root)
        self.assertIn(str(self.repo), obs["repos"])
        self.assertIn(str(worktree), obs["repos"])
        self.assertNotIn(str(fixture), obs["repos"])
        self.assertIn(str(tenant / "tenant.json"), [f["path"] for f in obs["local"]])
        self.assertNotIn(str(self.app), obs["repos"])

    def make_bare(self, name="remote.git"):
        bare = self.root / name
        subprocess.run(["/usr/bin/git", "init", "-q", "--bare", str(bare)], check=True)
        return bare

    def bare_ref(self, bare, ref):
        result = subprocess.run(["/usr/bin/git", "-C", str(bare), "rev-parse", "--verify", ref],
                                capture_output=True, text=True)
        return result.stdout.strip() if result.returncode == 0 else None

    def test_literal_pathspec_protects_tracked_directory_with_brackets(self):
        special = self.repo / "packages" / "a[1]" / "node_modules"
        special.mkdir(parents=True)
        (special / "keep.txt").write_text("tracked")
        with open(self.repo / ".gitignore", "a") as f:
            f.write("\n**/node_modules/\n")
        self.git("add", "-f", ":(literal)packages/a[1]/node_modules/keep.txt")
        with self.assertRaisesRegex(o.Refusal, "Tracked"):
            o.validate_cache(str(special), str(self.repo), check_processes=False)

    def test_push_disables_configured_tag_following_and_sends_one_ref(self):
        bare = self.make_bare()
        self.git("remote", "add", "origin", str(bare))
        self.git("config", "push.followTags", "true")
        self.git("tag", "-a", "private-history", "-m", "synthetic private tag")
        sha = self.git("rev-parse", "HEAD")
        self.git("branch", "approved", sha)
        self.git("branch", "unreviewed", sha)
        with patch.object(o, "require_project", return_value={}), patch.object(o, "refresh_project"):
            o.push_branch(str(self.repo), "approved", sha, "origin", str(bare))
        self.assertEqual(self.bare_ref(bare, "refs/heads/approved"), sha)
        self.assertIsNone(self.bare_ref(bare, "refs/heads/unreviewed"))
        self.assertIsNone(self.bare_ref(bare, "refs/tags/private-history"))

    def test_multiple_push_destinations_refuse_before_publication(self):
        first, second = self.make_bare("first.git"), self.make_bare("second.git")
        self.git("remote", "add", "origin", str(first))
        self.git("config", "--add", "remote.origin.pushurl", str(first))
        self.git("config", "--add", "remote.origin.pushurl", str(second))
        sha = self.git("rev-parse", "HEAD")
        self.git("branch", "approved", sha)
        with patch.object(o, "require_project", return_value={}):
            with self.assertRaisesRegex(o.Refusal, "single push destination"):
                o.push_branch(str(self.repo), "approved", sha, "origin", str(first))
        self.assertIsNone(self.bare_ref(first, "refs/heads/approved"))
        self.assertIsNone(self.bare_ref(second, "refs/heads/approved"))

    def test_push_keeps_reviewed_sha_if_branch_changes_after_validation(self):
        bare = self.make_bare()
        self.git("remote", "add", "origin", str(bare))
        reviewed = self.git("rev-parse", "HEAD")
        self.git("branch", "approved", reviewed)
        tree = self.git("rev-parse", "HEAD^{tree}")
        newer = self.git("commit-tree", tree, "-p", reviewed, "-m", "concurrent fixture commit")
        original = o.run_git
        def racing(path, *args, **kwargs):
            if args and args[0] == "push":
                self.git("update-ref", "refs/heads/approved", newer)
            return original(path, *args, **kwargs)
        with patch.object(o, "require_project", return_value={}), patch.object(o, "refresh_project"), patch.object(o, "run_git", side_effect=racing):
            o.push_branch(str(self.repo), "approved", reviewed, "origin", str(bare))
        self.assertEqual(self.bare_ref(bare, "refs/heads/approved"), reviewed)
        self.assertEqual(self.git("rev-parse", "refs/heads/approved"), newer)

    def test_fetch_never_prunes_even_when_configured(self):
        bare = self.make_bare()
        self.git("remote", "add", "origin", str(bare))
        sha = self.git("rev-parse", "HEAD")
        self.git("push", "-q", "origin", self.git("branch", "--show-current"))
        self.git("update-ref", "refs/remotes/origin/stale-review", sha)
        self.git("config", "fetch.prune", "true")
        self.git("config", "remote.origin.prune", "true")
        self.git("config", "remote.origin.pruneTags", "true")
        self.git("tag", "local-review-tag")
        record = {"remotes": [{"name": "origin"}], "worktrees": [{"path": str(self.repo), "exists": True}]}
        with patch.object(o, "require_project", return_value=record), patch.object(o, "process_cwds", return_value=[]):
            o.refresh_project(str(self.repo), fetch=True)
        self.assertEqual(self.git("rev-parse", "refs/remotes/origin/stale-review"), sha)
        self.assertEqual(self.git("rev-parse", "refs/tags/local-review-tag"), sha)

    def test_detached_unique_commit_remains_a_preservation_hold(self):
        bare = self.make_bare()
        self.git("remote", "add", "origin", str(bare))
        self.git("push", "-q", "origin", self.git("branch", "--show-current"))
        self.git("checkout", "--detach", "-q")
        (self.repo / "source.txt").write_text("detached change")
        self.git("add", "source.txt")
        self.git("commit", "-qm", "detached only")
        state = o.git_state(str(self.repo))
        observation = o.observe_files(self.root)
        project = o.project_record([state], observation, [])
        self.assertEqual(project["detachedHeads"][0]["uncovered"], 1)
        self.assertTrue(any("detached" in h for h in project["holds"]))

    def test_targeted_refresh_keeps_nested_repository_ownership(self):
        nested = self.repo / "nested"
        nested.mkdir()
        for args in [("init", "-q"), ("config", "user.name", "Fixture"),
                     ("config", "user.email", "fixture@example.invalid")]:
            subprocess.run(["/usr/bin/git", "-C", str(nested), *args], check=True)
        (nested / ".gitignore").write_text("node_modules/\n.env\n")
        subprocess.run(["/usr/bin/git", "-C", str(nested), "add", ".gitignore"], check=True)
        subprocess.run(["/usr/bin/git", "-C", str(nested), "commit", "-qm", "fixture"], check=True)
        (nested / "node_modules").mkdir()
        (nested / "node_modules" / "index.js").write_text("fixture")
        (nested / ".env").write_text("synthetic fixture")
        record = {"remotes": [], "worktrees": [{"path": str(self.repo), "exists": True}]}
        o.save(o.STATE / "snapshot.json", {"projects": [{"id": str(self.repo)}]})
        with patch.object(o, "require_project", return_value=record), patch.object(o, "process_cwds", return_value=[]):
            o.refresh_project(str(self.repo))
        project = o.load(o.STATE / "snapshot.json")["projects"][0]
        self.assertNotIn(str(nested / "node_modules"), [c["path"] for c in project["candidates"]])
        self.assertNotIn(str(nested / ".env"), [c["path"] for c in project["localFiles"]])

    def test_unavailable_activity_check_stops_cleanup(self):
        with patch.object(o, "process_cwds", side_effect=o.Refusal("Activity check unavailable")):
            with self.assertRaisesRegex(o.Refusal, "Activity"):
                o.validate_cache(str(self.cache), str(self.repo))
        self.assertTrue(self.cache.exists())

if __name__ == "__main__":
    unittest.main()
