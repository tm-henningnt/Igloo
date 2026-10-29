"""Wizard tests operate only on disposable checkouts and local bare remotes."""
import copy
import json
import os
from pathlib import Path
import shutil
import time
import unittest
from unittest.mock import patch

from tests.test_organizer import GuardTests, o
import lifecycle as life
import planning as wizard

class PlanningTests(unittest.TestCase):
    git = GuardTests.git
    make_bare = GuardTests.make_bare
    bare_ref = GuardTests.bare_ref
    def setUp(self):
        GuardTests.setUp(self)
        life.configure(o);wizard.configure(o)
        self.bare=self.make_bare()
        self.git("remote","add","origin",str(self.bare))
        (self.repo/".gitignore").write_text("node_modules/\n.cache/\n.env\n")
        self.git("add",".gitignore");self.git("commit","-qm","local rules")
        self.records=[self.project()]
        self.extra=[patch.object(o,"snapshot",side_effect=lambda:{"projects":self.records}),
                    patch.object(o,"process_cwds",return_value=[]),
                    patch.object(life,"open_files",return_value=None),
                    patch.object(o,"lifecycle_module",return_value=life),
                    patch.object(o,"scan",side_effect=lambda:{"projects":self.records,"generated":o.now(),"errors":[]}),
                    patch("pathlib.Path.home",return_value=self.root)]
        for p in self.extra:p.start()
    def tearDown(self):
        for p in reversed(self.extra):p.stop()
        GuardTests.tearDown(self)
    def project(self,**changes):
        p=dict(id=str(self.repo),path=str(self.repo),name="Fixture",size=1000,git=True,
               ageDays=200,fileAgeDays=10,active=False,activityKnown=True,
               candidates=[dict(id=str(self.cache),path=str(self.cache),owner=str(self.repo),
                                size=100,kind="node_modules",eligible=True,reason="fixture")],
               changes=[],localFiles=[],stashes=[],uncovered=0,unknown=0,
               remotes=[dict(name="origin",url=str(self.bare),pushURL=str(self.bare))],
               worktrees=[dict(path=str(self.repo),exists=True)])
        return dict(p,**changes)
    def prepare(self,action,policy=None):
        return wizard.prepare({"rules":policy or wizard.defaults(),
                               "choices":[{"path":str(self.repo),"action":action,"remote":"origin"}]})
    def freeze_for_thaw(self, count=1):
        paths = [self.repo]
        for index in range(1, count):
            second = self.root / f"second-{index}"
            rc, _ = o.run_git(self.repo, "clone", "--", str(self.repo), str(second))
            self.assertEqual(rc, 0)
            o.run_git(second, "remote", "set-url", "origin", str(self.bare))
            self.records.append(self.project(id=str(second), path=str(second), name=f"Second {index}",
                                             candidates=[], worktrees=[dict(path=str(second), exists=True)]))
            paths.append(second)
        identifiers = []
        for index, path in enumerate(paths[:count]):
            (path / ".env").write_text(f"local fixture {index}")
            review = life.preview_freeze(str(path), "origin", "freeze")
            result = life.execute_freeze(review["id"], "freeze")
            self.assertEqual(result["state"], "frozen", result["message"])
            identifiers.append(review["id"])
        return identifiers
    def test_age_bands_and_freeze_precedence_at_boundaries(self):
        policy=wizard.defaults()
        for age,action in [(0,"keep"),(29,"keep"),(30,"cleanup"),(179,"cleanup"),(180,"freeze"),(500,"freeze")]:
            with self.subTest(age=age):
                self.assertEqual(wizard.suggest({"projects":[self.project(ageDays=age)]},policy)[0]["action"],action)
    def test_unknown_age_activity_and_active_projects_default_to_keep(self):
        for changed in (dict(ageDays=None),dict(active=True),dict(activityKnown=False)):
            with self.subTest(changed=changed):
                self.assertEqual(wizard.suggest({"projects":[self.project(**changed)]},wizard.defaults())[0]["action"],"keep")

    def test_hold_overrides_wizard_suggestions_and_preparation(self):
        self.records = [self.project(hold=True)]
        row = wizard.suggest({"projects": self.records}, wizard.defaults())[0]
        self.assertEqual((row["action"], row["reason"]), ("keep", "On hold."))
        review = self.prepare("freeze")
        self.assertEqual(review["rows"][0]["status"], "blocked")
        self.assertEqual(review["rows"][0]["message"], "On hold.")
        result = wizard.execute(review["id"])
        self.assertEqual(result["rows"][0]["status"], "blocked")
        self.assertTrue((self.repo / ".git").is_dir())

    def test_hold_added_after_review_stops_wizard_cleanup(self):
        review = self.prepare("cleanup")
        self.records = [self.project(hold=True)]
        result = wizard.execute(review["id"])
        self.assertEqual(result["rows"][0]["status"], "blocked")
        self.assertEqual(result["rows"][0]["message"], "On hold.")
        self.assertTrue(self.cache.exists())

    def test_manual_reviews_on_hold_still_allow_cleanup_preserve_and_freeze(self):
        self.records = [self.project(hold=True)]
        cleanup = o.preview_cleanup([str(self.cache)])
        self.assertTrue(cleanup["held"])
        result = o.execute_cleanup(cleanup["id"])
        self.assertTrue(result["results"][0]["ok"])
        preserve = life.preview_freeze(str(self.repo), "origin", "preserve")
        self.assertTrue(preserve["held"])
        self.assertEqual(life.execute_freeze(preserve["id"])["state"], "ready")
        freeze = life.preview_freeze(str(self.repo), "origin", "freeze")
        self.assertTrue(freeze["held"])
        self.assertEqual(life.execute_freeze(freeze["id"])["state"], "frozen")

    def test_cleanup_outcome_reports_trash_and_observed_zero_space_freed(self):
        review = o.preview_cleanup([str(self.cache)])
        outcome = review["outcome"]
        self.assertEqual(outcome["checkoutsRemoved"], {"count": 0, "paths": []})
        self.assertEqual(outcome["archivesWritten"]["count"], 0)
        self.assertEqual(outcome["refsPushed"], [])
        self.assertEqual(outcome["movedToTrash"], 1)
        self.assertEqual(outcome["bytesFreedEstimate"], 0)
        self.assertIsNone(outcome["bytesFreed"])
        result = o.execute_cleanup(review["id"])["outcome"]
        self.assertTrue(result["observed"])
        self.assertEqual(result["movedToTrash"], 1)
        self.assertEqual(result["bytesFreed"], outcome["bytesFreedEstimate"])
        self.assertFalse(self.cache.exists())

    def test_wizard_run_records_cleanup_space(self):
        allocated = o.physical_size(self.cache)
        review = self.prepare("cleanup")
        result = wizard.execute(review["id"])
        self.assertEqual(result["outcome"]["observedSpace"], {
            "freedBytes": 0, "inTrashBytes": allocated, "restoredBytes": 0})
        run = o.load(o.STATE / "space-runs" / (review["id"] + ".json"))
        self.assertEqual(run["kind"], "wizard")
        self.assertEqual(run["observed"], {
            "freedBytes": 0, "inTrashBytes": allocated, "restoredBytes": 0})

    def test_mixed_wizard_outcome_sums_ready_rows_and_records_blocked_projects(self):
        second = self.root / "second"
        rc, _ = o.run_git(self.repo, "clone", "--", str(self.repo), str(second))
        self.assertEqual(rc, 0)
        o.run_git(second, "remote", "set-url", "origin", str(self.bare))
        third = self.root / "held"
        self.records += [self.project(id=str(second), path=str(second), name="Second", candidates=[],
                                      worktrees=[dict(path=str(second), exists=True)]),
                         self.project(id=str(third), path=str(third), name="Held", hold=True, candidates=[],
                                      worktrees=[dict(path=str(third), exists=False)])]
        review = wizard.prepare({"rules": wizard.defaults(), "choices": [
            {"path": str(self.repo), "action": "cleanup"},
            {"path": str(second), "action": "freeze", "remote": "origin"},
            {"path": str(third), "action": "freeze", "remote": "origin"}]})
        estimate = review["outcome"]
        self.assertEqual(estimate["checkoutsRemoved"], {"count": 1, "paths": [str(second)]})
        self.assertEqual(estimate["archivesWritten"]["count"], 1)
        self.assertIsNone(estimate["archivesWritten"]["paths"])
        self.assertEqual(estimate["movedToTrash"], 1)
        self.assertEqual(estimate["held"], [{"path": str(third), "reason": "On hold."}])
        executed = wizard.execute(review["id"])
        self.assertEqual(executed["message"],
                         "Wizard actions completed. Changed or blocked projects were held; completed actions remain recorded.")
        result = executed["outcome"]
        for field in ("checkoutsRemoved", "refsPushed", "movedToTrash", "held"):
            self.assertEqual(result[field], estimate[field])
        for field in ("count", "location"):
            self.assertEqual(result["archivesWritten"][field], estimate["archivesWritten"][field])
        self.assertEqual(result["archivesWritten"]["paths"],
                         [str(o.VAULT / executed["rows"][1]["archiveID"])])
        self.assertTrue((Path(result["archivesWritten"]["paths"][0]) / "manifest.json").is_file())
        self.assertTrue(result["observed"])
        self.assertEqual(result["bytesFreedEstimate"], estimate["bytesFreedEstimate"])
        self.assertIsInstance(result["bytesFreed"], int)
    def test_file_age_is_choosable_and_has_no_git_fallback(self):
        policy=dict(wizard.defaults(),ageBasis="files")
        for age,action in [(10,"keep"),(70,"cleanup"),(300,"freeze"),(None,"keep")]:
            self.assertEqual(wizard.suggest({"projects":[self.project(fileAgeDays=age)]},policy)[0]["action"],action)
    def test_bad_rules_and_duplicate_selection_refused(self):
        for policy in (dict(wizard.defaults(),freezeDays=30),dict(wizard.defaults(),cacheDays=True),
                       dict(wizard.defaults(),ageBasis="guess"),dict(wizard.defaults(),cacheKinds=[".git"])):
            with self.assertRaises(o.Refusal):wizard.rules(policy)
        choice={"path":str(self.repo),"action":"cleanup","remote":"origin"}
        with self.assertRaises(o.Refusal):wizard.prepare({"rules":wizard.defaults(),"choices":[choice,choice]})
    def test_recent_project_override_runs_cleanup_once(self):
        self.records=[self.project(ageDays=1)]
        review=self.prepare("cleanup")
        self.assertEqual(review["rows"][0]["status"],"ready")
        self.assertTrue(self.cache.exists())
        reply=wizard.execute(review["id"])
        self.assertEqual(reply["rows"][0]["status"],"done")
        self.assertFalse(self.cache.exists())
        self.assertTrue(o.load(o.STATE/"trash.json"))
        with self.assertRaisesRegex(o.Refusal,"already accepted"):wizard.execute(review["id"])
    def test_active_processes_cannot_be_forced_by_age_override(self):
        with patch.object(o,"process_cwds",return_value=[{"path":str(self.repo),"pid":"fixture","command":"fixture"}]):
            plan=self.prepare("freeze")
        self.assertEqual(plan["rows"][0]["status"],"blocked")
        self.assertTrue(self.repo.exists())
    def test_stale_cache_is_held_and_source_preserved(self):
        review=self.prepare("cleanup")
        (self.cache/"new.txt").write_text("new local edits")
        result=wizard.execute(review["id"])
        self.assertEqual(result["rows"][0]["status"],"failed")
        self.assertTrue((self.cache/"new.txt").exists())
    def test_one_acceptance_fully_freezes_and_offline_rehydrates(self):
        (self.repo/".env").write_text("synthetic local fixture")
        plan=self.prepare("freeze")
        self.assertEqual(plan["rows"][0]["status"],"ready")
        self.assertTrue((self.repo/".git").exists())
        result=wizard.execute(plan["id"]);row=result["rows"][0]
        self.assertEqual(row["status"],"done",row["message"])
        self.assertFalse((self.repo/".git").exists())
        self.assertTrue((self.repo/life.MARKER).exists())
        life.rehydrate(row["archiveID"],offline=True)
        self.assertEqual((self.repo/".env").read_text(),"synthetic local fixture")
        self.assertEqual((self.repo/"source.txt").read_text(),"source\n")
    def test_archive_only_keeps_checkout(self):
        review=self.prepare("archive")
        self.assertEqual(review["outcome"]["bytesFreedEstimate"], 0)
        self.assertGreater(review["outcome"]["bytesUsedEstimate"], 0)
        result=wizard.execute(review["id"]);row=result["rows"][0]
        self.assertEqual(result["outcome"]["bytesUsedEstimate"], review["outcome"]["bytesUsedEstimate"])
        self.assertEqual(row["status"],"done",row["message"])
        self.assertTrue((self.repo/".git").exists())
        self.assertEqual(life.archive_record(row["archiveID"])[1]["state"],"ready")
    def test_changed_source_blocks_accepted_freeze(self):
        plan=self.prepare("freeze")
        (self.repo/"source.txt").write_text("new local source")
        row=wizard.execute(plan["id"])["rows"][0]
        self.assertEqual(row["status"],"failed")
        self.assertTrue((self.repo/".git").is_dir())
    def test_workspace_change_and_expired_review_stop_before_actions(self):
        review=self.prepare("cleanup")
        plan=o.load(wizard.location(review["id"]));plan["created"]="2000-01-01T00:00:00+00:00";wizard.persist(plan)
        with self.assertRaisesRegex(o.Refusal,"expired"):wizard.execute(plan["id"])
        self.assertTrue(self.cache.exists())
        plan["created"]=o.now();plan["root"]="/different";wizard.persist(plan)
        with self.assertRaisesRegex(o.Refusal,"Workspace"):wizard.execute(plan["id"])
    def test_row_failure_does_not_prevent_remaining_work_and_results_persist(self):
        plan=self.prepare("cleanup")
        bad=copy.deepcopy(plan["rows"][0]);bad.update(id="blocked-row",name="Missing",path="/missing",status="blocked",message="fixture hold")
        plan["rows"].insert(0,bad);wizard.persist(plan)
        result=wizard.execute(plan["id"])
        self.assertEqual([r["status"] for r in result["rows"]],["blocked","done"])
        self.assertEqual(o.load(o.STATE/"wizard-last.json")["status"],"finished")
    def test_failed_first_action_does_not_prevent_second_project_cleanup(self):
        second=self.root/"second"
        rc,_=o.run_git(self.repo,"clone","--",str(self.repo),str(second))
        self.assertEqual(rc,0)
        cache=second/"node_modules";cache.mkdir();(cache/"fixture").write_text("dependency")
        other=self.project(id=str(second),path=str(second),name="Second",
                           candidates=[dict(id=str(cache),path=str(cache),owner=str(second),size=100,
                                            kind="node_modules",eligible=True,reason="fixture")])
        self.records.append(other)
        plan=wizard.prepare({"rules":wizard.defaults(),"choices":[
            {"path":str(self.repo),"action":"cleanup","remote":"origin"},
            {"path":str(second),"action":"cleanup","remote":"origin"}]})
        (self.cache/"new-work").write_text("keep this new edit")
        result=wizard.execute(plan["id"])
        self.assertEqual([r["status"] for r in result["rows"]],["failed","done"])
        self.assertTrue((self.cache/"new-work").exists())
        self.assertFalse(cache.exists())
    def test_wizard_rules_save_in_home_config_and_scan_is_read_only(self):
        reply=wizard.scan(dict(wizard.defaults(),cacheDays=45))
        self.assertEqual(wizard.state()["rules"]["cacheDays"],45)
        self.assertEqual(wizard.rules_path(),self.root/".config/igloo/wizard.json")
        self.assertEqual(wizard.rules_path().stat().st_mode&0o777,0o600)
        self.assertTrue(self.cache.is_dir())
        self.assertTrue((self.repo/".git").is_dir())
        self.assertEqual(len(reply["rows"]),1)

    def test_overlapping_cleanup_and_freeze_scopes_are_not_both_approved(self):
        other=self.project(id=str(self.root/"second"),path=str(self.root/"second"),name="Second")
        self.records.append(other)
        fake=dict(id="a"*32,checkouts=[str(self.repo)])
        with patch.object(life,"preview_freeze",return_value=fake):
            review=wizard.prepare({"rules":wizard.defaults(),"choices":[
                {"path":other["path"],"action":"freeze","remote":"origin"},
                {"path":str(self.repo),"action":"cleanup","remote":"origin"}]})
        self.assertEqual([r["status"] for r in review["rows"]],["ready","blocked"])
        self.assertIn("Overlaps",review["rows"][1]["message"])
    def test_interrupted_batch_cannot_replay_completed_or_uncertain_actions(self):
        plan=self.prepare("cleanup");plan["status"]="running";wizard.persist(plan)
        with self.assertRaisesRegex(o.Refusal,"already accepted"):wizard.execute(plan["id"])
        self.assertTrue(self.cache.exists())
    def test_including_active_projects_only_changes_suggestions_not_process_guards(self):
        policy=dict(wizard.defaults(),ignoreActive=False)
        row=wizard.suggest({"projects":[self.project(active=True)]},policy)[0]
        self.assertEqual(row["action"],"freeze")
        self.assertIn("processes must close",row["reason"])

    def test_batch_thaw_restores_two_projects_and_reports_each_result(self):
        identifiers = self.freeze_for_thaw(2)
        with patch.object(o, "on_hold", return_value=True):
            review = wizard.prepare_thaw(identifiers)
        self.assertEqual([row["status"] for row in review["rows"]], ["ready", "ready"])
        self.assertTrue(all(row["destination"] == str(self.bare) for row in review["rows"]))
        self.assertTrue(all(row["refs"] and row["localFiles"] for row in review["rows"]))
        self.assertEqual(review["outcome"]["archivesUsed"]["paths"],
                         [row["archiveLocation"] for row in review["rows"]])

        with patch.object(o, "on_hold", return_value=True):
            result = wizard.execute_thaw(review["id"])

        self.assertEqual([row["status"] for row in result["rows"]], ["done", "done"])
        self.assertEqual(result["message"],
                         "Thaw completed. Inspect each project result; completed projects remain restored.")
        self.assertTrue(all("Checkout and remote refs were restored." in row["message"] for row in result["rows"]))
        self.assertTrue(all("Dependencies remain uninstalled." in row["message"] for row in result["rows"]))
        self.assertTrue(all((Path(row["path"]) / ".git").is_dir() for row in result["rows"]))
        self.assertEqual(result["outcome"]["checkoutsRestored"]["count"], 2)
        self.assertEqual(result["outcome"]["archivesUsed"]["count"], 2)
        restored_refs = result["outcome"]["refsRestored"]
        self.assertEqual([ref["path"] for ref in restored_refs], [row["path"] for row in result["rows"]])
        self.assertTrue(all(ref["ref"] == "refs/heads/main" and len(ref["sha"]) >= 7 for ref in restored_refs))
        self.assertIsNone(result["outcome"]["bytesFreed"])
        with self.assertRaisesRegex(o.Refusal, "already accepted"):
            wizard.execute_thaw(review["id"])

    def test_thaw_prepare_returns_rows_for_exactly_the_selected_archives(self):
        identifiers = self.freeze_for_thaw(3)
        selected = [identifiers[2], identifiers[0]]

        review = wizard.dispatch("wizard-thaw-prepare", [json.dumps(selected)])

        self.assertEqual([row["id"] for row in review["rows"]], selected)
        self.assertEqual([row["archiveID"] for row in review["rows"]], selected)
        paths = {row["path"] for row in review["rows"]}
        self.assertEqual(len(paths), 2)
        self.assertEqual(set(review["outcome"]["checkoutsRestored"]["paths"]), paths)
        self.assertEqual(review["outcome"]["archivesUsed"]["paths"],
                         [row["archiveLocation"] for row in review["rows"]])
        self.assertNotIn(str(self.root / "second-1"), paths)

    def test_batch_thaw_blocks_an_occupied_destination(self):
        identifier = self.freeze_for_thaw()[0]
        occupied = self.repo / "work added after Freeze"
        occupied.write_text("keep")

        review = wizard.prepare_thaw([identifier])

        self.assertEqual(review["rows"][0]["status"], "blocked")
        self.assertEqual(review["rows"][0]["message"],
                         "The original path is occupied. Inspect its contents in Finder before Thaw.")
        self.assertEqual(wizard.execute_thaw(review["id"])["rows"][0]["status"], "blocked")
        self.assertEqual(occupied.read_text(), "keep")

    def test_batch_thaw_blocks_a_corrupted_archive(self):
        identifier = self.freeze_for_thaw()[0]
        location, _ = life.archive_record(identifier)
        (location / "local-files.tar.gz").write_bytes(b"corrupt")

        review = wizard.prepare_thaw([identifier])

        self.assertEqual(review["rows"][0]["status"], "blocked")
        self.assertEqual(review["rows"][0]["message"],
                         "The archive failed its checksum check. Keep the Freezer copy and use Open Freezer to inspect it.")

    def test_missing_thaw_parent_suggests_the_available_finder_action(self):
        path = self.root / "missing-parent" / "checkout"
        manifest = {"path": str(path), "roots": [str(path)]}

        self.assertEqual(wizard.thaw_blocker(manifest, "a" * 32),
                         "The original parent folder is missing. Use Show path in Finder to inspect its location.")

    def test_unreachable_remote_blocker_offers_a_check_again_action(self):
        identifier = self.freeze_for_thaw()[0]

        with patch.object(wizard, "thaw_remote_ready", return_value=False):
            review = wizard.prepare_thaw([identifier])

        self.assertEqual(review["rows"][0]["message"],
                         "The remote is unreachable. Restore remote access, then use Check remote again.")

    def test_batch_thaw_blocks_an_entry_that_is_no_longer_frozen(self):
        identifier = self.freeze_for_thaw()[0]
        location, manifest = life.archive_record(identifier)
        manifest["state"] = "rehydrated"
        o.save(location / "manifest.json", manifest)

        review = wizard.prepare_thaw([identifier])

        self.assertEqual(review["rows"][0]["status"], "blocked")
        self.assertEqual(review["rows"][0]["message"],
                         "This project is no longer Frozen. Its Freezer entry changed after you selected it. Use Open Freezer to inspect it.")
        self.assertEqual(review["rows"][0]["destination"], str(self.bare))

    def test_rehydrating_archive_points_to_freezer_recovery(self):
        identifier = self.freeze_for_thaw()[0]
        location, manifest = life.archive_record(identifier)
        manifest.update(state="rehydrating", restoredRoots=[])
        o.save(location / "manifest.json", manifest)

        review = wizard.prepare_thaw([identifier])

        row = review["rows"][0]
        self.assertEqual(row["status"], "blocked")
        self.assertEqual(row["restoredRoots"], [])
        self.assertIn("Open Freezer", row["message"])

    def test_expired_thaw_review_is_refused_without_restoring_paths(self):
        identifier = self.freeze_for_thaw()[0]
        review = wizard.prepare_thaw([identifier])

        with patch.object(o, "age_seconds", return_value=601):
            with self.assertRaisesRegex(o.Refusal, "Thaw plan expired"):
                wizard.execute_thaw(review["id"])

        self.assertTrue(life.is_stub(Path(review["rows"][0]["path"]), identifier))

    def test_overlapping_thaw_destinations_block_the_later_row(self):
        first, second = "a" * 32, "b" * 32
        paths = {first: str(self.repo), second: str(self.repo / "nested")}

        def ready_row(identifier):
            path = paths[identifier]
            return {"id": identifier, "archiveID": identifier, "name": identifier,
                    "path": path, "status": "ready", "message": "Ready.", "destination": str(self.bare),
                    "refs": [], "checkouts": [path], "localFiles": [],
                    "restoredRoots": [], "archiveAttempted": False,
                    "archiveLocation": str(self.root / "Freezer" / identifier), "checksums": {},
                    "manifestFingerprint": "", "destinationStates": []}

        with patch.object(wizard, "thaw_row", side_effect=ready_row):
            review = wizard.prepare_thaw([first, second])

        self.assertEqual([row["status"] for row in review["rows"]], ["ready", "blocked"])
        self.assertEqual(review["rows"][1]["message"],
                         "Overlaps another selected Thaw destination. Use Change selection to keep one project.")

    def test_manifest_change_after_thaw_review_refuses_the_whole_review(self):
        identifier = self.freeze_for_thaw()[0]
        review = wizard.prepare_thaw([identifier])
        location, manifest = life.archive_record(identifier)
        manifest["destination"] = str(self.root / "changed-remote.git")
        o.save(location / "manifest.json", manifest)

        with self.assertRaisesRegex(o.Refusal, "stale"):
            wizard.execute_thaw(review["id"])

        stale = wizard.thaw_location(review["id"])
        self.assertIn("Thaw plan", o.load(stale)["message"])
        self.assertTrue(life.is_stub(Path(review["rows"][0]["path"]), identifier))

    def test_partial_root_restore_names_restored_roots_and_freezer_recovery(self):
        linked = self.root / "linked-worktree"
        self.git("worktree", "add", "-b", "linked", str(linked))
        identifier = self.freeze_for_thaw()[0]
        location, manifest = life.archive_record(identifier)
        roots = manifest["roots"]
        self.assertEqual(len(roots), 2)
        review = wizard.prepare_thaw([identifier])
        original_rename = life.rename_exclusive
        fail_root = roots[1]

        def fail_later_root(source, destination):
            if str(destination) == fail_root:
                raise OSError("injected later root publication failure")
            return original_rename(source, destination)

        with patch.object(life, "rename_exclusive", side_effect=fail_later_root):
            result = wizard.execute_thaw(review["id"])

        row = result["rows"][0]
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["restoredRoots"], [roots[0]])
        self.assertIn(roots[0], row["message"])
        self.assertIn("Use Open Freezer", row["message"])
        self.assertEqual(result["outcome"]["checkoutsRestored"]["count"], 1)
        self.assertEqual([ref["path"] for ref in result["outcome"]["refsRestored"]], [roots[0]])

    def test_unreachable_remote_does_not_stop_other_project_from_thawing(self):
        first, second = self.freeze_for_thaw(2)
        first_remote = self.make_bare("late-remote.git")
        location, manifest = life.archive_record(first)
        manifest["destination"] = str(first_remote)
        o.save(location / "manifest.json", manifest)

        review = wizard.prepare_thaw([first, second])

        self.assertEqual([row["status"] for row in review["rows"]], ["ready", "ready"])
        shutil.rmtree(first_remote)
        result = wizard.execute_thaw(review["id"])

        self.assertEqual([row["status"] for row in result["rows"]], ["failed", "done"])
        self.assertEqual(result["rows"][0]["message"],
                         "The remote is unreachable. The archive is intact; restore remote access, then use Check remote again.")
        self.assertTrue((Path(result["rows"][1]["path"]) / ".git").is_dir())
        self.assertTrue(life.is_stub(Path(result["rows"][0]["path"]), first))

    def test_failure_of_second_project_does_not_stop_the_third(self):
        first, second, third = self.freeze_for_thaw(3)
        review = wizard.prepare_thaw([first, second, third])
        original_rehydrate = life.rehydrate

        def fail_second(identifier, offline=False):
            if identifier == second:
                raise o.Refusal("Injected second-project failure.")
            return original_rehydrate(identifier, offline=offline)

        with patch.object(life, "rehydrate", side_effect=fail_second):
            result = wizard.execute_thaw(review["id"])

        self.assertEqual([row["status"] for row in result["rows"]], ["done", "failed", "done"])
        self.assertTrue((Path(result["rows"][0]["path"]) / ".git").is_dir())
        self.assertTrue(life.is_stub(Path(result["rows"][1]["path"]), second))
        self.assertTrue((Path(result["rows"][2]["path"]) / ".git").is_dir())

    def test_destination_change_after_thaw_review_refuses_the_whole_review(self):
        first, second = self.freeze_for_thaw(2)
        review = wizard.prepare_thaw([first, second])
        (Path(review["rows"][0]["path"]) / "new file").write_text("keep")

        with self.assertRaisesRegex(o.Refusal, "stale"):
            wizard.execute_thaw(review["id"])

        self.assertTrue((Path(review["rows"][0]["path"]) / "new file").is_file())
        self.assertTrue(life.is_stub(Path(review["rows"][1]["path"]), second))

    def test_interrupted_thaw_is_recorded_and_cannot_be_replayed(self):
        identifier = self.freeze_for_thaw()[0]
        review = wizard.prepare_thaw([identifier])
        with patch.object(life, "rehydrate", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                wizard.execute_thaw(review["id"])

        saved = o.load(wizard.thaw_location(review["id"]))
        self.assertEqual((saved["status"], saved["rows"][0]["status"]), ("running", "running"))
        with self.assertRaisesRegex(o.Refusal, "already accepted"):
            wizard.execute_thaw(review["id"])

    def test_file_age_ignores_dependencies_git_metadata_and_build_output(self):
        old=time.time()-90*86400
        for p in self.repo.iterdir():
            if p.is_file():os.utime(p,(old,old))
        (self.repo/"build").mkdir();(self.repo/"build/output").write_text("fresh generated")
        obs=o.observe_files(self.root)
        self.assertEqual(o.file_age([obs["modified"][str(self.repo)]]),90)
        (self.repo/"source.txt").write_text("fresh source")
        obs=o.observe_files(self.root)
        self.assertEqual(o.file_age([obs["modified"][str(self.repo)]]),0)

if __name__=="__main__":unittest.main()
