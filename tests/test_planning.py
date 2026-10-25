"""Wizard tests operate only on disposable checkouts and local bare remotes."""
import copy
import os
from pathlib import Path
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
    def test_age_bands_and_freeze_precedence_at_boundaries(self):
        policy=wizard.defaults()
        for age,action in [(0,"keep"),(29,"keep"),(30,"cleanup"),(179,"cleanup"),(180,"freeze"),(500,"freeze")]:
            with self.subTest(age=age):
                self.assertEqual(wizard.suggest({"projects":[self.project(ageDays=age)]},policy)[0]["action"],action)
    def test_unknown_age_activity_and_active_projects_default_to_keep(self):
        for changed in (dict(ageDays=None),dict(active=True),dict(activityKnown=False)):
            with self.subTest(changed=changed):
                self.assertEqual(wizard.suggest({"projects":[self.project(**changed)]},wizard.defaults())[0]["action"],"keep")
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
        review=self.prepare("archive");row=wizard.execute(review["id"])["rows"][0]
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
