"""Task tests use disposable projects, temporary homes and mocked LaunchAgents."""
import json
import ast
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import re
import unittest
from unittest.mock import patch
from tests.test_planning import PlanningTests, o
import automation as tasks

class TaskTests(unittest.TestCase):
    git=PlanningTests.git
    make_bare=PlanningTests.make_bare
    bare_ref=PlanningTests.bare_ref
    project=PlanningTests.project
    def setUp(self):
        PlanningTests.setUp(self);tasks.configure(o)
        self.records=[self.project(ageDays=70)]
        self.definition=dict(id="weekly-cleanup",name="Weekly cleanup",root=str(self.root),vault=str(o.VAULT),
            mode="cleanup",rules=tasks.planning.defaults(),include=[],exclude=[],projectTypes=[],
            maxProjects=5,maxBytes=10*1024**3,unattended=False)
        tasks.save(self.definition)
    def tearDown(self):PlanningTests.tearDown(self)
    def sources(self):
        source=Path(__file__).resolve().parents[1]
        for name in tasks.MODULES:
            origin=source/name if name=="LICENSE" else source/"backend"/name
            shutil.copyfile(origin,self.app/name)
    def test_save_is_private_and_never_enables_a_schedule(self):
        self.assertEqual(tasks.task_path().stat().st_mode&0o777,0o600)
        self.assertFalse((self.root/"Library/LaunchAgents").exists())
        self.assertFalse(tasks.listing()["tasks"][0]["definition"]["unattended"])
    def test_default_run_only_plans_and_apply_is_explicit(self):
        run=tasks.run_task("weekly-cleanup")
        self.assertEqual(run["status"],"planned")
        self.assertTrue(self.cache.exists())
        result=tasks.apply_plan(run["planID"])
        self.assertEqual(result["status"],"completed")
        self.assertFalse(self.cache.exists())
        replay=tasks.apply_plan(run["planID"])
        self.assertEqual(replay["status"],"failed")

    def test_cli_space_line_uses_clear_estimate_and_disk_labels(self):
        with patch.dict(sys.modules):
            import task_cli
            line = task_cli.space_line({"outcome": {"bytesFreedEstimate": 12}, "observed": {
                "freedBytes": 10, "inTrashBytes": 20, "restoredBytes": 0}})
        self.assertEqual(line,
                         "Estimate: 12 bytes · Freed from disk: 10 · In Trash: 20 · Restored from Trash: 0")
    def test_review_only_task_cannot_execute(self):
        tasks.save(dict(self.definition,mode="review"))
        run=tasks.run_task("weekly-cleanup",apply=True)
        self.assertEqual(run["status"],"failed");self.assertTrue(self.cache.exists())
    def test_old_projects_are_held_for_freeze_review_without_publication(self):
        self.records=[self.project(ageDays=300)]
        with patch.object(tasks.planning,"execute") as execute:
            run=tasks.run_task("weekly-cleanup",apply=True)
        execute.assert_not_called()
        self.assertEqual(run["status"],"held")
        plan=o.load(tasks.planning.location(run["planID"]))
        self.assertEqual(plan["rows"][0]["action"],"freeze")
        self.assertTrue((self.repo/".git").exists())
    def test_byte_limit_blocks_the_whole_batch(self):
        tasks.save(dict(self.definition,maxBytes=1))
        run=tasks.run_task("weekly-cleanup",apply=True)
        self.assertEqual(run["status"],"held");self.assertTrue(self.cache.exists())
    def test_scope_exclusion_and_type_filter_apply_to_plans(self):
        for change in (dict(exclude=[str(self.repo)]),dict(projectTypes=["Website"]),dict(include=[str(self.root/"elsewhere")])):
            tasks.save(dict(self.definition,**change))
            run=tasks.run_task("weekly-cleanup",apply=True)
            self.assertEqual(run["status"],"nothing-to-do")
            self.assertTrue(self.cache.exists())
    def test_project_cap_holds_remaining_candidates(self):
        second=self.project(id=str(self.root/"second"),path=str(self.root/"second"),name="Second",size=1)
        self.records.append(second);tasks.save(dict(self.definition,maxProjects=1))
        run=tasks.run_task("weekly-cleanup")
        self.assertEqual(run["ready"],1);self.assertEqual(run["blocked"],1)
    def test_active_and_unknown_age_are_not_cleaned(self):
        for change in (dict(active=True),dict(ageDays=None),dict(activityKnown=False)):
            self.records=[self.project(ageDays=70,**change)] if "ageDays" not in change else [self.project(**change)]
            run=tasks.run_task("weekly-cleanup",apply=True)
            self.assertEqual(run["status"],"nothing-to-do");self.assertTrue(self.cache.exists())
    def test_failed_scan_never_prepares_or_executes_cleanup(self):
        with patch.object(o,"scan",return_value={"errors":["fixture unreadable"]}):
            run=tasks.run_task("weekly-cleanup",apply=True)
        self.assertEqual(run["status"],"failed");self.assertTrue(self.cache.exists())
    def test_changed_definition_invalidates_prepared_plan(self):
        run=tasks.run_task("weekly-cleanup")
        tasks.save(dict(self.definition,rules=dict(self.definition["rules"],cacheDays=40)))
        with self.assertRaisesRegex(o.Refusal,"changed"):tasks.apply_plan(run["planID"])
        self.assertTrue(self.cache.exists())
    def test_unattended_requires_grant_and_pinned_policy(self):
        for grant,expected in ((False,tasks.policy_hash(tasks.get("weekly-cleanup"))),(True,"old-policy")):
            tasks.save(dict(self.definition,unattended=grant))
            with patch.object(o,"scan") as scan:
                run=tasks.run_task("weekly-cleanup",scheduled=True,expected_hash=expected)
            scan.assert_not_called();self.assertEqual(run["status"],"failed")
        self.assertTrue(self.cache.exists())
    def test_authorized_scheduled_cleanup_uses_fresh_checks(self):
        tasks.save(dict(self.definition,unattended=True))
        run=tasks.run_task("weekly-cleanup",scheduled=True,expected_hash=tasks.policy_hash(tasks.get("weekly-cleanup")))
        self.assertEqual(run["status"],"completed");self.assertFalse(self.cache.exists())

    def test_scheduled_hold_leaves_project_and_cache_unchanged(self):
        self.records = [self.project(ageDays=70, hold=True)]
        tasks.save(dict(self.definition, unattended=True))
        before = {str(p.relative_to(self.repo)): p.read_bytes()
                  for p in self.repo.rglob("*") if p.is_file()}
        run = tasks.run_task("weekly-cleanup", scheduled=True,
                             expected_hash=tasks.policy_hash(tasks.get("weekly-cleanup")))
        review = o.load(tasks.planning.location(run["planID"]))
        self.assertEqual(run["ready"], 0)
        self.assertEqual(review["held"][0]["reason"], "On hold.")
        after = {str(p.relative_to(self.repo)): p.read_bytes()
                 for p in self.repo.rglob("*") if p.is_file()}
        self.assertEqual(before, after)

    def test_task_review_and_execution_include_outcome(self):
        review_run = tasks.run_task("weekly-cleanup")
        estimate = review_run["outcome"]
        self.assertEqual(estimate["movedToTrash"], 1)
        self.assertIsNone(estimate["bytesFreed"])
        result = tasks.apply_plan(review_run["planID"])["outcome"]
        self.assertTrue(result["observed"])
        self.assertEqual(result["movedToTrash"], estimate["movedToTrash"])
        self.assertEqual(result["bytesFreed"], 0)

    def test_task_history_includes_observed_space_and_legacy_runs_are_unmeasured(self):
        allocated = o.physical_size(self.cache)
        applied = tasks.run_task("weekly-cleanup", apply=True)
        observed = {"freedBytes": 0, "inTrashBytes": allocated, "restoredBytes": 0}
        self.assertEqual(applied["observed"], observed)
        saved = tasks.history("weekly-cleanup")["runs"]
        self.assertEqual(next(run for run in saved if run["id"] == applied["id"])["observed"], observed)

        legacy = {"id": "legacy-run", "taskID": "weekly-cleanup", "taskName": "Weekly cleanup",
                  "started": o.now(), "status": "completed", "message": "Old record"}
        o.save(o.STATE / "task-runs" / "weekly-cleanup" / "legacy-run.json", legacy)
        row = next(run for run in o.space_report()["runs"] if run["id"] == "legacy-run")
        self.assertIsNone(row["observed"])
    def test_calendar_uses_strict_local_time_and_weekday(self):
        self.assertEqual(tasks.calendar({"frequency":"weekly","time":"09:30","weekday":0}),{"Hour":9,"Minute":30,"Weekday":0})
        self.assertEqual(tasks.calendar({"frequency":"daily","time":"22:10"}),{"Hour":22,"Minute":10})
        for v in ({"frequency":"daily","time":"24:00"},{"frequency":"weekly","time":"09:00","weekday":True}):
            with self.assertRaises(o.Refusal):tasks.calendar(v)
    def test_generated_agent_has_no_shell_and_does_not_run_at_load(self):
        self.sources();tasks.save(dict(self.definition,unattended=True))
        with patch.object(tasks.subprocess,"run",return_value=subprocess.CompletedProcess([],0)):
            tasks.schedule_task("weekly-cleanup",{"frequency":"weekly","time":"09:00","weekday":0})
        path=self.root/"Library/LaunchAgents/local.igloo.task.weekly-cleanup.plist"
        data=plistlib.loads(path.read_bytes())
        self.assertFalse(data["RunAtLoad"]);self.assertEqual(data["StartCalendarInterval"]["Weekday"],0)
        self.assertIn("--policy-hash",data["ProgramArguments"])
        self.assertNotIn("/bin/sh",data["ProgramArguments"])
        tasks.save(dict(self.definition,unattended=True,rules=dict(self.definition["rules"],cacheDays=40)))
        with patch.object(tasks,"loaded",return_value=True):
            self.assertTrue(tasks.listing()["tasks"][0]["schedule"]["stale"])
    def test_cli_runtime_is_independent_and_never_replaces_foreign_executable(self):
        self.sources()
        installed=tasks.install_cli()
        self.assertIn("task-runtime",Path(installed["path"]).read_text())
        env=dict(os.environ,HOME=str(self.root/"cli-home"))
        result=subprocess.run([installed["path"],"--help"],capture_output=True,text=True,env=env)
        self.assertEqual(result.returncode,0,result.stderr)
        Path(installed["path"]).write_text("unrelated executable")
        with self.assertRaisesRegex(o.Refusal,"different"):tasks.install_cli()
    def test_cli_install_preserves_saved_tasks_and_uses_igloo_storage(self):
        self.sources()
        before = tasks.task_path().read_bytes()
        installed = tasks.install_cli()
        self.assertEqual(Path(installed["path"]).name, "igloo")
        self.assertEqual(tasks.listing()["cliPath"], installed["path"])
        self.assertEqual(tasks.task_path().read_bytes(), before)
        self.assertTrue(tasks.our_cli(Path(installed["path"])))
        result = subprocess.run([installed["path"], "--help"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("usage: igloo", result.stdout)
        self.assertEqual(tasks.label("weekly-cleanup"), "local.igloo.task.weekly-cleanup")

    def test_cli_reinstall_reuses_runtime_without_touching_other_commands(self):
        self.sources()
        installed = tasks.install_cli()
        other = Path(installed["path"]).with_name("unrelated-tool")
        other.write_text("unrelated executable")
        tasks.install_cli()
        self.assertEqual(other.read_text(), "unrelated executable")

    def test_review_command_and_hidden_plan_alias_have_the_same_result(self):
        self.sources()
        home = self.root / "users/isolated-user"
        home.mkdir(parents=True)
        config = dict(o.CONFIG, projectsRoot=str(self.root), cloneRoot=str(self.root),
                      stateRoot=str(o.STATE), archiveRoot=str(o.VAULT), pythonExecutable=sys.executable)
        o.save(home / ".config/igloo/config.json", config)
        o.save(home / ".config/igloo/tasks.json", tasks.config())
        cli = tasks.install_cli()["path"]
        replies = []
        for command in ("review", "plan"):
            result = subprocess.run([cli, command, "weekly-cleanup", "--json"],
                                    capture_output=True, text=True, env=dict(os.environ, HOME=str(home)))
            self.assertEqual(result.returncode, 0, result.stderr)
            run = json.loads(result.stdout)
            review = o.load(tasks.planning.location(run["planID"]))
            for row in review["rows"]:
                if row.get("cleanup"):
                    row["cleanup"].pop("id", None)
                    row["cleanup"].pop("created", None)
            replies.append((run["status"], run["message"], run["ready"], run["blocked"], review["rows"]))
        self.assertEqual(replies[0], replies[1])
        help_result = subprocess.run([cli, "--help"], capture_output=True, text=True,
                                     env=dict(os.environ, HOME=str(home)))
        self.assertEqual(help_result.returncode, 0, help_result.stderr)
        self.assertRegex(help_result.stdout, r"\breview\b")
        self.assertNotRegex(help_result.stdout, r"\bplan\b")
        self.assertTrue(self.cache.exists())

    def test_backend_message_literals_use_glossary_terms(self):
        backend = Path(__file__).resolve().parents[1] / "backend"
        for path in backend.glob("*.py"):
            tree = ast.parse(path.read_text())
            docstrings = {id(node.body[0].value) for node in ast.walk(tree)
                          if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                          and node.body and isinstance(node.body[0], ast.Expr)
                          and isinstance(node.body[0].value, ast.Constant)}
            for node in ast.walk(tree):
                if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                        and id(node) not in docstrings and " " in node.value):
                    with self.subTest(path=path.name, line=node.lineno):
                        self.assertIsNone(re.search(r"\b(?:vault|retire|retirement)\b|Ready to freeze",
                                                    node.value, re.I))

    def test_busy_skip_and_failure_results_are_persisted(self):
        run=tasks.record_busy("weekly-cleanup")
        self.assertEqual(run["status"],"skipped-busy")
        self.assertEqual(tasks.history("weekly-cleanup")["runs"][0]["id"],run["id"])
    def test_cli_busy_lock_skips_without_mutation_and_records_the_run(self):
        import fcntl
        self.sources()
        home=self.root/"users/isolated-user";home.mkdir(parents=True)
        config=dict(o.CONFIG,projectsRoot=str(self.root),cloneRoot=str(self.root),stateRoot=str(o.STATE),archiveRoot=str(o.VAULT),pythonExecutable=sys.executable)
        o.save(home/".config/igloo/config.json",config)
        o.save(home/".config/igloo/tasks.json",tasks.config())
        installed=tasks.install_cli()
        o.STATE.mkdir(parents=True,exist_ok=True)
        with (o.STATE/"backend.lock").open("a") as lock:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            result=subprocess.run([installed["path"],"run","weekly-cleanup","--apply","--json"],
                                  capture_output=True,text=True,env=dict(os.environ,HOME=str(home)))
        self.assertEqual(result.returncode,75,result.stderr)
        self.assertEqual(json.loads(result.stdout)["status"],"skipped-busy")
        self.assertTrue(self.cache.exists())
        self.assertEqual(tasks.history("weekly-cleanup")["runs"][0]["status"],"skipped-busy")

    def test_documentation_assets_contain_only_fictional_user_paths(self):
        assets=Path(__file__).resolve().parents[1]/"assets"
        def values(value):
            if isinstance(value,dict):
                for child in value.values():yield from values(child)
            elif isinstance(value,list):
                for child in value:yield from values(child)
            elif isinstance(value,str):yield value
        for name in ("demo-catalog.json","demo-tasks.json"):
            data=json.loads((assets/name).read_text())
            for text in values(data):
                if text.startswith("/Users/"):self.assertTrue(text.startswith("/Users/demo/"),text)
                if text.startswith("https://github.com/"):self.assertTrue(text.startswith("https://github.com/example/"))
        catalog=json.loads((assets/"demo-catalog.json").read_text())
        self.assertEqual(catalog["totalSize"],sum(p["size"] for p in catalog["projects"]))

    def test_task_list_still_works_after_changing_workspace(self):
        with patch.object(o,"ROOT",self.root/"other"):
            self.assertFalse(tasks.listing()["tasks"][0]["compatible"])
            with self.assertRaises(o.Refusal):tasks.get("weekly-cleanup")

if __name__=="__main__":unittest.main()
