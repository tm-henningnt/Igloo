"""Task tests use disposable projects, temporary homes and mocked LaunchAgents."""
import json
import ast
import datetime as dt
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import tempfile
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

    def test_remind_check_counts_freeze_candidates_and_writes_only_history(self):
        eligible = self.project(id=str(self.root / "old"), path=str(self.root / "old"),
                                name="Old project", ageDays=220, fileAgeDays=10, size=4096)
        held = self.project(id=str(self.root / "held"), path=str(self.root / "held"),
                            name="Held project", ageDays=300, hold=True, size=8192)
        frozen = self.project(id=str(self.root / "frozen"), path=str(self.root / "frozen"),
                              name="Frozen project", ageDays=400, size=16384)
        data = {"projects": [eligible, held, frozen],
                "archives": [{"path": frozen["path"], "state": "frozen"}]}
        before = {str(path.relative_to(o.STATE)): path.read_bytes()
                  for path in o.STATE.rglob("*") if path.is_file()}
        checked = dt.datetime.now(dt.timezone.utc)
        commit = str(int((checked - dt.timedelta(days=221)).timestamp()))
        with patch.object(o, "snapshot", return_value=data), \
             patch.object(o, "now", return_value=checked.isoformat()), \
             patch.object(o, "run_git", return_value=(0, commit)):
            result = tasks.remind_check()
        after = {str(path.relative_to(o.STATE)): path.read_bytes()
                 for path in o.STATE.rglob("*") if path.is_file()}
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["size"], 4096)
        self.assertEqual(result["ids"], [eligible["id"]])
        self.assertEqual(set(after) - set(before), {"reminder-history.json"})
        history = json.loads((o.STATE / "reminder-history.json").read_text())
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["ids"], [eligible["id"]])

    def test_remind_check_refreshes_git_age_without_fetching_and_uses_strict_freeze_age(self):
        checked = dt.datetime(2026, 10, 28, 12, tzinfo=dt.timezone.utc)
        generated = (checked - dt.timedelta(days=90)).isoformat()
        exact = self.project(id=str(self.root / "exact"), path=str(self.root / "exact"),
                             name="Exact threshold", ageDays=1, size=10)
        stale = self.project(id=str(self.root / "stale"), path=str(self.root / "stale"),
                             name="Stale age", ageDays=1, size=20)
        exact_commit = str(int((checked - dt.timedelta(days=180)).timestamp()))
        stale_commit = str(int((checked - dt.timedelta(days=181)).timestamp()))
        with patch.object(o, "snapshot", return_value={"generated": generated,
                                                       "projects": [exact, stale], "archives": []}), \
             patch.object(o, "now", return_value=checked.isoformat()), \
             patch.object(tasks.planning, "state", return_value={"rules": tasks.planning.defaults()}), \
             patch.object(o, "run_git", side_effect=[(0, exact_commit), (0, stale_commit)]) as git:
            result = tasks.remind_check()
        self.assertEqual(result["ids"], [stale["id"]])
        self.assertEqual(result["message"], "1 project has been quiet for more than 180 days (20 bytes)")
        self.assertTrue(all(call.args[1:4] == ("log", "--all", "-1") for call in git.call_args_list))
        self.assertTrue(all("fetch" not in call.args for call in git.call_args_list))

    def test_remind_check_adds_days_since_scan_for_file_age(self):
        checked = dt.datetime(2026, 10, 28, 12, tzinfo=dt.timezone.utc)
        scanned = checked - dt.timedelta(days=2)
        project = self.project(ageDays=400, fileAgeDays=179, verifiedAt=scanned.isoformat())
        with patch.object(o, "snapshot", return_value={"generated": scanned.isoformat(),
                                                       "projects": [project], "archives": []}), \
             patch.object(o, "now", return_value=checked.isoformat()), \
             patch.object(tasks.planning, "state", return_value={
                 "rules": dict(tasks.planning.defaults(), ageBasis="files")}), \
             patch.object(o, "run_git") as git:
            result = tasks.remind_check()
        git.assert_not_called()
        self.assertEqual(result["ids"], [project["id"]])

    def test_reminder_notification_text_matches_issue_copy_and_singular_grammar(self):
        size = round(12.4 * 1024**3)
        self.assertEqual(tasks.reminder_notification_text(3, 180, size),
                         "3 projects have been quiet for more than 180 days (12.4 GB)")
        self.assertEqual(tasks.reminder_notification_text(1, 180, 0),
                         "1 project has been quiet for more than 180 days (0 bytes)")

    def test_remind_history_keeps_only_the_latest_52_entries(self):
        history_path = o.STATE / "reminder-history.json"
        o.save(history_path, [{"sequence": n} for n in range(52)])
        with patch.object(o, "snapshot", return_value={"projects": [], "archives": []}):
            result = tasks.remind_check()
        history = json.loads(history_path.read_text())
        self.assertEqual(result["count"], 0)
        self.assertEqual(len(history), 52)
        self.assertEqual(history[-1]["count"], 0)
        self.assertEqual(history[0]["sequence"], 1)

    def test_reminder_check_queues_behind_catalog_work(self):
        self.assertTrue(o.command_waits_for_backend_lock("remind-check"))
        self.assertFalse(o.command_waits_for_backend_lock("snapshot"))

    def test_reminder_agent_uses_app_identity_schedule_and_removes_cleanly(self):
        self.assertEqual(tasks.reminder_label("local.igloo"), "local.igloo.remind")
        self.assertEqual(tasks.reminder_label("local.igloo.dev"), "local.igloo.dev.remind")
        def fake_app(path, bundle_id):
            info = path / "Contents/Info.plist"
            info.parent.mkdir(parents=True, exist_ok=True)
            info.write_bytes(plistlib.dumps({"CFBundleIdentifier": bundle_id}))
            executable = path / "Contents/MacOS/Igloo"
            executable.parent.mkdir(parents=True, exist_ok=True)
            executable.write_bytes(b"")

        first_app = self.root / "Apps/Igloo Dev.app"
        second_app = self.root / "Other/Igloo Dev.app"
        fake_app(first_app, "local.igloo.dev")
        fake_app(second_app, "local.igloo.dev")
        with patch.dict(os.environ, {"HOME": str(self.root)}), \
             patch.object(tasks.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as launchctl:
            tasks.configure_reminder({"enabled": False, "weekday": 2, "time": "09:35",
                                      "appPath": str(first_app)})
            launchctl.assert_not_called()
            tasks.configure_reminder({"enabled": True, "weekday": 2, "time": "09:35",
                                      "appPath": str(first_app)})
            agent = self.root / "Library/LaunchAgents/local.igloo.dev.remind.plist"
            first = plistlib.loads(agent.read_bytes())
            self.assertEqual(first["Label"], "local.igloo.dev.remind")
            # launchd runs the app executable, so Background activity names Igloo Dev, not "open".
            self.assertEqual(first["ProgramArguments"], [str(first_app / "Contents/MacOS/Igloo"), "--remind"])
            self.assertEqual(first["StartCalendarInterval"], {"Weekday": 2, "Hour": 9, "Minute": 35})
            self.assertEqual(first["AssociatedBundleIdentifiers"], ["local.igloo.dev"])
            tasks.configure_reminder({"enabled": True, "weekday": 4, "time": "17:10",
                                      "appPath": str(second_app)})
            changed = plistlib.loads(agent.read_bytes())
            self.assertEqual(changed["ProgramArguments"], [str(second_app / "Contents/MacOS/Igloo"), "--remind"])
            self.assertEqual(changed["StartCalendarInterval"], {"Weekday": 4, "Hour": 17, "Minute": 10})
            tasks.configure_reminder({"enabled": False, "weekday": 4, "time": "17:10",
                                      "appPath": str(second_app)})
            self.assertFalse(agent.exists())
        self.assertGreaterEqual(launchctl.call_count, 3)

    def test_release_reminder_agent_runs_the_release_app_executable(self):
        app = self.root / "Applications/Igloo.app"
        info = app / "Contents/Info.plist"
        info.parent.mkdir(parents=True)
        info.write_bytes(plistlib.dumps({"CFBundleIdentifier": "local.igloo"}))
        with patch.dict(os.environ, {"HOME": str(self.root)}), \
             patch.object(tasks.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)):
            request = {"enabled": True, "weekday": 1, "time": "09:00", "appPath": str(app)}
            with self.assertRaisesRegex(o.Refusal, "executable"):
                tasks.configure_reminder(request)
            agent = self.root / "Library/LaunchAgents/local.igloo.remind.plist"
            self.assertFalse(agent.exists())
            (app / "Contents/MacOS").mkdir()
            (app / "Contents/MacOS/Igloo").write_bytes(b"")
            tasks.configure_reminder(request)
            definition = plistlib.loads(agent.read_bytes())
        self.assertEqual(definition["Label"], "local.igloo.remind")
        self.assertEqual(definition["ProgramArguments"], [str(app / "Contents/MacOS/Igloo"), "--remind"])
        self.assertEqual(definition["AssociatedBundleIdentifiers"], ["local.igloo"])

    def test_reminder_launch_hands_off_to_a_running_copy_of_the_same_bundle(self):
        source = (Path(__file__).resolve().parents[1] / "app/Igloo.swift").read_text()
        route = "enum ReminderRoute {" + source.split("enum ReminderRoute {", 1)[1].split("enum ReminderHandoff {", 1)[0]
        checks = """
typealias Copy = ReminderRoute.RunningCopy
let other = Copy(processIdentifier: 20, bundleIdentifier: "local.igloo.dev", bundlePath: "/Other/Igloo Dev.app")
let same = Copy(processIdentifier: 30, bundleIdentifier: "local.igloo.dev", bundlePath: "/Apps/Igloo Dev.app")
let release = Copy(processIdentifier: 40, bundleIdentifier: "local.igloo", bundlePath: "/Apps/Igloo.app")
let own = Copy(processIdentifier: 10, bundleIdentifier: "local.igloo.dev", bundlePath: "/Apps/Igloo Dev.app")
func target(_ copies: [Copy]) -> Copy? {
    ReminderRoute.handoffTarget(among: copies, ownProcess: 10, bundleIdentifier: "local.igloo.dev",
                                bundlePath: "/Apps/./Igloo Dev.app")
}
precondition(target([own]) == nil)
precondition(target([own, release]) == nil)
precondition(target([own, other, same]) == same)
precondition(target([own, other]) == other)
precondition(ReminderRoute.handoffTarget(among: [same], ownProcess: 10, bundleIdentifier: nil,
                                         bundlePath: "/Apps/Igloo Dev.app") == nil)
"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "main.swift").write_text("import Foundation\n" + route + checks)
            build = subprocess.run(["/usr/bin/swiftc", str(root / "main.swift"), "-o", str(root / "check")],
                                   capture_output=True, text=True, timeout=120)
            self.assertEqual(build.returncode, 0, build.stderr)
            run = subprocess.run([str(root / "check")], capture_output=True, text=True, timeout=30)
            self.assertEqual(run.returncode, 0, run.stderr)
        # The hand-off runs before NSApplication starts, so a launchd start opens no window.
        main = source.split("@main enum IglooMain", 1)[1]
        self.assertLess(main.index("ReminderHandoff.toRunningCopy()"), main.index("ReminderRunnerApp.main()"))
        self.assertIn('URL(string: "igloo://remind")', source)

    def test_reminder_commands_are_exposed_by_the_organizer_cli(self):
        script = Path(__file__).resolve().parents[1] / "backend/organizer.py"
        result = subprocess.run([sys.executable, "-B", str(script), "--help"],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("remind-check", result.stdout)
        self.assertIn("remind-agent", result.stdout)
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

    def test_space_report_counts_each_run_once_and_totals_equal_the_rows(self):
        allocated = o.physical_size(self.cache)
        applied = tasks.run_task("weekly-cleanup", apply=True)
        def check(expected):
            report = o.space_report()
            measured = [run for run in report["runs"] if isinstance(run.get("observed"), dict)]
            # The task run owns the wizard review and the cleanup review that it ran.
            self.assertEqual([run["id"] for run in measured], [applied["id"]])
            for name in ("last30Days", "allTime"):
                totals = report["totals"][name]
                self.assertEqual(totals["measuredRuns"], len(measured))
                self.assertEqual(totals["estimateBytes"], sum(run.get("estimateBytes", 0) for run in report["runs"]))
                for field in o.SPACE_FIELDS:
                    self.assertEqual(totals["observed"][field], sum(run["observed"][field] for run in measured))
                self.assertEqual(totals["observed"], expected)
        # In Trash is never added to Freed from disk.
        check({"freedBytes": 0, "inTrashBytes": allocated, "restoredBytes": 0})
        item = o.load(o.STATE / "trash.json")[0]
        self.assertEqual(item["runID"], applied["id"])
        with patch.object(Path, "home", return_value=self.root):
            o.restore(item["id"])
        check({"freedBytes": 0, "inTrashBytes": 0, "restoredBytes": allocated})
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

    def test_dev_cli_labels_and_schedules_stay_apart_from_the_release(self):
        self.sources()
        release_tasks = tasks.task_path()
        release_bytes = release_tasks.read_bytes()
        registry = o.STATE / "task-schedules.json"
        o.save(registry, {"weekly-cleanup": {"label": "local.igloo.task.weekly-cleanup"}})
        registry_bytes = registry.read_bytes()
        with patch.dict(os.environ, {"IGLOO_APP_ID": "local.igloo.dev"}):
            self.assertEqual(tasks.task_path(), self.root / ".config/igloo.dev/tasks.json")
            self.assertEqual(tasks.label("weekly-cleanup"), "local.igloo.dev.task.weekly-cleanup")
            tasks.save(dict(self.definition, unattended=True))
            installed = tasks.install_cli()
            self.assertEqual(Path(installed["path"]), self.root / ".local/bin/igloo.dev")
            self.assertEqual(tasks.listing()["cliPath"], installed["path"])
            self.assertIn("export IGLOO_APP_ID=local.igloo.dev\n", Path(installed["path"]).read_text())
            with patch.object(tasks.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as run:
                tasks.schedule_task("weekly-cleanup", {"frequency": "weekly", "time": "09:00", "weekday": 0})
                tasks.unschedule("weekly-cleanup")
            labels = {arg for call in run.call_args_list for arg in call.args[0] if "local.igloo" in arg}
            self.assertTrue(labels)
            self.assertTrue(all("local.igloo.dev.task.weekly-cleanup" in arg for arg in labels), labels)
            self.assertTrue((o.STATE / "task-logs.dev/weekly-cleanup.out.log").exists())
            self.assertEqual(o.load(o.STATE / "task-schedules.dev.json"), {})
        self.assertFalse((self.root / ".local/bin/igloo").exists())
        self.assertFalse((self.root / "Library/LaunchAgents/local.igloo.task.weekly-cleanup.plist").exists())
        self.assertFalse((o.STATE / "task-logs").exists())
        self.assertEqual(registry.read_bytes(), registry_bytes)
        self.assertEqual(release_tasks.read_bytes(), release_bytes)
        self.assertEqual(tasks.label("weekly-cleanup"), "local.igloo.task.weekly-cleanup")

    def test_dev_cli_reads_the_dev_configuration_root(self):
        self.sources()
        home = self.root / "users/dev-user"
        home.mkdir(parents=True)
        config = dict(o.CONFIG, projectsRoot=str(self.root), cloneRoot=str(self.root),
                      stateRoot=str(o.STATE), archiveRoot=str(o.VAULT), pythonExecutable=sys.executable)
        with patch.dict(os.environ, {"IGLOO_APP_ID": "local.igloo.dev"}):
            folder = o.preferences.config_path(home).parent
            cli = tasks.install_cli()["path"]
        o.save(folder / "config.json", config)
        o.save(folder / "tasks.json", tasks.config())
        env = {k: v for k, v in os.environ.items() if k != "IGLOO_APP_ID"}
        result = subprocess.run([cli, "tasks", "--json"], capture_output=True, text=True, env=dict(env, HOME=str(home)))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([t["definition"]["id"] for t in json.loads(result.stdout)["tasks"]], ["weekly-cleanup"])
        self.assertFalse((home / ".config/igloo").exists())

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
