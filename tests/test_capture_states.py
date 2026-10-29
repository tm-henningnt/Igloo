"""Capture launcher checks use disposable output and a fake process boundary."""
import contextlib
import importlib.util
import io
import json
import plistlib
import re
import stat
import signal
import struct
import zlib
import subprocess
import sys
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, call, patch
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("capture_states", ROOT / "scripts/capture_states.py")
capture = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(capture)


def sample_png(uniform=False, near_uniform=False, placeholder=False, yellow=(255, 204, 0), red=(255, 59, 48)):
    width, height = 64, 64
    def chunk(kind, payload):
        return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload) & 0xffffffff)
    rows = []
    for y in range(height):
        pixels = bytearray()
        for x in range(width):
            black = uniform or (near_uniform and (x, y) != (0, 0)) or (not near_uniform and (x + y) % 2)
            color = (0, 0, 0, 255) if black else (240, 240, 240, 255)
            if placeholder and not black:
                color = (*yellow, 255) if y < 32 else (*red, 255)
            pixels.extend(color)
        rows.append(b"\0" + pixels)
    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(b"".join(rows))) + chunk(b"IEND", b"")


def sparse_sheet_png(noise=False):
    """White sheet with either two small text-like rows or scattered dark noise."""
    width, height = 128, 128
    def chunk(kind, payload):
        return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload) & 0xffffffff)
    rows = []
    for y in range(height):
        pixels = bytearray()
        for x in range(width):
            if noise:
                dark = y < 80 and x == (y * 17) % width
            else:
                dark = (20 <= y < 25 or 50 <= y < 55) and any(start <= x < start + 2 for start in (20, 30, 40, 50))
            pixels.extend((0, 0, 0, 255) if dark else (255, 255, 255, 255))
        rows.append(b"\0" + pixels)
    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(b"".join(rows))) + chunk(b"IEND", b"")


def write_window_metadata(folder, pid, sheet=False):
    frame = {"x": 0, "y": 0, "width": 900, "height": 700}
    windows = [{"kind": "window", "windowNumber": 123, "frame": frame, "isOnScreen": True}]
    if sheet:
        windows.append({"kind": "sheet", "windowNumber": 124, "frame": frame, "isOnScreen": True})
    (folder / "windows.json").write_text(json.dumps({"pid": pid, "windows": windows}))


def write_renderer_png(folder, kind, png):
    path = capture.renderer_path(folder, kind)
    path.parent.mkdir(mode=0o700, exist_ok=True)
    path.write_bytes(png)


def orca_state(tree_text):
    return {"result": {"snapshot": {"treeText": tree_text}}}


ORCA_WIZARD_START = """193 sheet
\t194 text Cleanup wizard
\t195 text Choose a path
\t196 text Choose what you want to do with your projects.
\t197 button        (Make space)
\t198 button        (Thaw projects)
\t199 button        (Close)
"""

ORCA_THAW_SELECTION = """202 sheet
\t203 text Select projects
\t204 text Select Frozen projects to restore from their remotes.
\t205 heading
\t206 scroll area, 5 items
\t\t207 list
\t\t\t208 outline row
\t\t\t\t209 cell
\t\t\t\t\t210 checkbox, Value: 0
\t\t\t\t211 text Harbor CLI
\t\t\t\t212 text Frozen 2026-10-28
\t\t\t\t213 text /demo/Harbor CLI
\t\t\t214 outline row
\t\t\t\t215 cell
\t\t\t\t\t216 checkbox, Value: 0
\t222 button        (Back)
\t223 button        (Previous Thaw results)
\t224 text 0 selected
\t225 button (disabled)   (Create Thaw plan)
\t226 button        (Close)
"""

ORCA_THAW_REVIEW = """300 sheet
\t301 text Thaw plan
\t302 button (Change selection)
\t303 outline row (selected) Harbor CLI Review details
\t304 checkbox, Value: 0
\t305 outline row Signal Lab Review details
\t306 checkbox, Value: 0
\t307 button (Close)
\t308 button (disabled) (Thaw 1 ready project)
\t309 text Expected outcome for Harbor CLI
\t310 text Original paths
"""

ORCA_THAW_BLOCKED = """300 sheet
\t301 text Thaw plan
\t302 button (Change selection)
\t303 outline row Harbor CLI Review details
\t304 checkbox, Value: 0
\t305 outline row (selected) Signal Lab Review details
\t306 checkbox, Value: 0
\t307 button (Close)
\t308 button (disabled) (Thaw 1 ready project)
\t309 text Path occupied
"""


class CaptureStatesTests(unittest.TestCase):
    def test_shown_scope_minimum_fits_inspector_and_detail_tabs(self):
        scenario = {"size": "1100x700", "scope": "shown"}
        self.assertEqual(capture.expected_size(scenario), (1300, 793))

    def test_full_rail_scope_hidden_uses_its_minimum_width(self):
        scenario = {"size": "850x700", "scope": "hidden", "rail": "full"}
        self.assertEqual(capture.expected_size(scenario), (1100, 793))

    def test_demo_remote_urls_are_valid(self):
        catalog = json.loads((ROOT / "assets/demo-catalog.json").read_text())
        urls = [url for project in catalog["projects"] for remote in project.get("remotes", [])
                for url in (remote.get("url", ""), remote.get("pushURL", "")) if url]
        self.assertTrue(urls)
        for url in urls:
            with self.subTest(url=url):
                parsed = urlsplit(url)
                self.assertEqual(parsed.scheme, "https")
                self.assertTrue(parsed.netloc)
                self.assertNotRegex(url, r"\s")

    def test_demo_urls_in_catalog_and_task_fixtures_have_no_spaces(self):
        def values(value):
            if isinstance(value, dict):
                for child in value.values():
                    yield from values(child)
            elif isinstance(value, list):
                for child in value:
                    yield from values(child)
            elif isinstance(value, str) and value.startswith("https://"):
                yield value

        urls = []
        for fixture in ("demo-catalog.json", "demo-tasks.json"):
            data = json.loads((ROOT / "assets" / fixture).read_text())
            urls.extend(values(data))
        self.assertTrue(urls)
        for url in urls:
            with self.subTest(url=url):
                self.assertNotRegex(url, r"\s")

    def test_demo_wizard_freeze_outcome_matches_its_project(self):
        tasks = json.loads((ROOT / "assets/demo-tasks.json").read_text())
        catalog = json.loads((ROOT / "assets/demo-catalog.json").read_text())
        review = tasks["demoWizardReview"]
        freeze_rows = [row for row in review["rows"] if row["action"] == "freeze"]
        self.assertEqual(review["outcome"]["checkoutsRemoved"]["paths"], [row["path"] for row in freeze_rows])
        harbor_cli = next(project for project in catalog["projects"] if project["name"] == "Harbor CLI")
        self.assertLessEqual(catalog["reviewOutcomes"]["freeze"]["bytesFreedEstimate"], harbor_cli["size"])
        self.assertLessEqual(catalog["reviewOutcomes"]["wizard"]["bytesFreedEstimate"], harbor_cli["size"])
        for key in ("demoWizardReview", "demoTaskReview"):
            item = tasks[key]
            row_paths = {row["path"] for row in item["rows"]}
            self.assertTrue(all(held["path"] in row_paths for held in item["outcome"]["held"]))
        for pushed in review["outcome"]["refsPushed"]:
            self.assertRegex(pushed["sha"], r"^[0-9a-f]{7,12}$")
            project = next(project for project in catalog["projects"] if project["path"] == freeze_rows[0]["path"])
            self.assertIn(pushed["destination"], [remote["pushURL"] for remote in project["remotes"]])

    def test_demo_thaw_catalog_covers_blockers_and_uses_demo_paths(self):
        tasks = json.loads((ROOT / "assets/demo-tasks.json").read_text())
        thaw = tasks["demoWizardThaw"]
        rows = thaw["review"]["rows"]
        messages = "\n".join(row["message"] for row in rows)
        for blocker in ("occupied", "unreachable", "checksum", "no longer Frozen", "Open Freezer"):
            self.assertIn(blocker, messages)
        result = thaw["result"]
        self.assertEqual({row["id"] for row in result["rows"]}, {row["id"] for row in rows})
        blocked_paths = {row["path"] for row in result["rows"] if row["status"] == "blocked"}
        self.assertEqual(blocked_paths, {item["path"] for item in result["outcome"]["held"]})
        restored = result["outcome"]["refsRestored"]
        self.assertEqual([item["path"] for item in restored], ["/Users/demo/Projects/Ice Garden"])
        self.assertTrue(all(len(item["sha"]) == 7 for item in restored))
        catalog = json.loads((ROOT / "assets/demo-catalog.json").read_text())
        paths = [archive["path"] for archive in catalog["archives"]]
        paths += [row["archiveLocation"] for row in rows]
        paths += [path for row in rows for path in row["checkouts"] + row["localFiles"]]
        self.assertTrue(all(path.startswith("/Users/demo/") for path in paths))
        self.assertTrue(all(row["destination"].startswith("https://example.com/") for row in rows))
        self.assertEqual(thaw["result"]["outcome"]["bytesFreed"], None)

    def test_demo_uses_one_frozen_set_for_the_wizard_freezer_and_project_list(self):
        catalog = json.loads((ROOT / "assets/demo-catalog.json").read_text())
        tasks = json.loads((ROOT / "assets/demo-tasks.json").read_text())
        source = (ROOT / "app/DemoCatalog.swift").read_text()
        thaw = tasks["demoWizardThaw"]
        self.assertNotIn("archives", thaw, "the Thaw wizard must read the Freezer archives of the catalog")
        self.assertIn('(snapshot().archives ?? []).filter { $0.state == "frozen" }', source)
        frozen = {archive["id"]: archive for archive in catalog["archives"] if archive["state"] == "frozen"}
        project_paths = {project["path"] for project in catalog["projects"]}
        frozen_paths = {path for archive in frozen.values() for path in [archive["path"]] + archive["paths"]}
        self.assertEqual(frozen_paths & project_paths, set(), "a Frozen project has no checkout in the project list")
        for plan in (thaw["review"], thaw["result"]):
            for row in plan["rows"]:
                if row.get("state") == "frozen":
                    self.assertIn(row["id"], frozen)
                    self.assertEqual(row["path"], frozen[row["id"]]["path"])
        backups = {row["id"] for row in catalog["backupStatus"]["archives"]}
        self.assertTrue(set(frozen).issubset(backups))

    def test_open_sheets_show_refusals_and_progress_themselves(self):
        main = (ROOT / "app/Igloo.swift").read_text()
        self.assertIn("store.error != nil && store.sheetErrorHosts.isEmpty", main)
        self.assertIn("store.error != nil && store.sheetErrorHosts.last == host", main)
        sheets = {"LifecycleViews.swift": "struct RemoteSetupView", "PreferencesViews.swift": "struct RemoteRepositoriesView",
                  "WizardViews.swift": "struct CleanupWizardView"}
        for name, start in sheets.items():
            with self.subTest(sheet=start):
                body = (ROOT / "app" / name).read_text().split(start, 1)[1].split("\nstruct ", 1)[0]
                self.assertIn(".sheetErrorAlert(store)", body)
                self.assertIn("SheetProgressRow(", body)
        remote = (ROOT / "app/LifecycleViews.swift").read_text().split("struct RemoteSetupView", 1)[1]
        self.assertIn("if !store.isDemo { if let onClose { onClose() } else { store.remoteSetup = nil } }", remote)
        self.assertIn("repositories-loading", capture.SHEET_VIEWS)

    def test_demo_result_fixtures_match_the_selected_action_and_space_labels(self):
        catalog = json.loads((ROOT / "assets/demo-catalog.json").read_text())
        tasks = json.loads((ROOT / "assets/demo-tasks.json").read_text())
        cleanup = catalog["demoResults"]["cleanup"]
        self.assertEqual(cleanup["observedSpace"]["restoredBytes"], 0)
        selected_freeze_paths = [row["path"] for row in tasks["demoWizardReview"]["rows"]
                                 if row["action"] == "freeze"]
        self.assertEqual(catalog["demoResults"]["wizard"]["checkoutsRemoved"]["paths"], selected_freeze_paths)
        hashes = []
        def collect_hashes(value):
            if isinstance(value, dict):
                for key, item in value.items():
                    if key == "sha" and isinstance(item, str):
                        hashes.append(item)
                    else:
                        collect_hashes(item)
            elif isinstance(value, list):
                for item in value:
                    collect_hashes(item)
        collect_hashes(catalog)
        self.assertTrue(all(len(value) <= 12 for value in hashes))

    def test_demo_wizard_trash_and_restore_totals_match_the_reviewed_actions(self):
        catalog = json.loads((ROOT / "assets/demo-catalog.json").read_text())
        tasks = json.loads((ROOT / "assets/demo-tasks.json").read_text())
        source = (ROOT / "app/DemoCatalog.swift").read_text()
        cleanup_items = [item for row in tasks["demoWizardReview"]["rows"]
                         if row["status"] == "ready" and row["action"] == "cleanup"
                         for item in row["cleanup"]["items"]]
        result = catalog["demoResults"]["wizard"]
        self.assertEqual(result["movedToTrash"], len(cleanup_items))
        self.assertEqual(result["observedSpace"]["inTrashBytes"], sum(item["size"] for item in cleanup_items))
        self.assertEqual(result["observedSpace"]["restoredBytes"], 0)
        wizard_run = next(run for run in catalog["spaceReport"]["runs"] if run["kind"] == "wizard")
        self.assertEqual(wizard_run["observed"]["inTrashBytes"], result["observedSpace"]["inTrashBytes"])
        self.assertEqual(wizard_run["observed"]["restoredBytes"], 0)
        cleanup_case = source.split('case "cleanup-result":', 1)[1].split('case "wizard-result":', 1)[0]
        self.assertEqual(cleanup_case.count("ActionResult(path:"), catalog["demoResults"]["cleanup"]["movedToTrash"])

    def test_demo_space_report_totals_equal_the_rows_and_the_trash_history(self):
        catalog = json.loads((ROOT / "assets/demo-catalog.json").read_text())
        report = catalog["spaceReport"]
        fields = ("freedBytes", "inTrashBytes", "restoredBytes")
        for name in ("last30Days", "allTime"):
            with self.subTest(totals=name):
                totals = report["totals"][name]
                self.assertEqual(totals["estimateBytes"], sum(run["estimateBytes"] for run in report["runs"]))
                for field in fields:
                    self.assertEqual(totals["observed"][field], sum(run["observed"][field] for run in report["runs"]))
        for run in report["runs"]:
            with self.subTest(run=run["id"]):
                items = [item for item in catalog["history"] if item["runID"] == run["id"]]
                self.assertEqual(run["observed"]["inTrashBytes"],
                                 sum(item["size"] for item in items if item["status"] == "trashed"))
                self.assertEqual(run["observed"]["restoredBytes"],
                                 sum(item["size"] for item in items if item["status"] == "restored"))
        frozen = {archive["name"] for archive in catalog["archives"] if archive["state"] == "frozen"}
        for run in report["runs"]:
            if run["kind"] == "freeze":
                self.assertIn(run["label"], frozen)

    def test_demo_remote_status_and_branch_commits_agree_with_the_project(self):
        catalog = json.loads((ROOT / "assets/demo-catalog.json").read_text())
        for project in catalog["projects"]:
            with self.subTest(project=project["name"]):
                if not project["remotes"]:
                    self.assertEqual(project["remoteStatus"], "No remote")
                    self.assertTrue(any(blocker["code"] == "no-remote" for blocker in project["blockers"]))
                    self.assertTrue(all(branch["upstream"] == "" for branch in project["branches"]))
                for branch in project["branches"]:
                    self.assertRegex(branch["sha"], r"^[0-9a-f]{7,12}$")

    def test_updater_scenarios_are_captured_as_sheets(self):
        for scenario in capture.scenarios():
            if scenario["view"].startswith("update-"):
                with self.subTest(view=scenario["view"]):
                    self.assertIn(scenario["view"], capture.SHEET_VIEWS)

    def test_ready_update_view_is_in_the_swift_capture_allow_list(self):
        source = (ROOT / "app/DemoCatalog.swift").read_text()
        self.assertIn('static let updateViews = AppUpdater.demoFailureNames.map { "update-" + $0 } + ["update-markdown", "update-ready", "update-install-failed"]', source)
        self.assertIn('updateViews).union(extraSheetViews)).contains(view)', source)
        shadows_shared_views = 'let updateViews = AppUpdater.demoFailureNames.map { "update-" + $0 } + ["update-markdown"]' in source
        self.assertFalse(shadows_shared_views, "capture options must use the shared update view set")

    def test_thaw_review_copy_uses_project_noun_consistently(self):
        backend = (ROOT / "backend/planning.py").read_text()
        fixture = json.loads((ROOT / "assets/demo-tasks.json").read_text())
        self.assertTrue('"message": "Only ready projects will run. Blocked projects stay untouched."' in backend,
                        "backend thaw review copy must use project wording")
        self.assertEqual(fixture["demoWizardThaw"]["review"]["message"],
                         "Only ready projects will run. Blocked projects stay untouched.")

    def test_matrix_covers_required_states(self):
        scenarios = capture.scenarios()
        self.assertEqual({s["appearance"] for s in scenarios}, {"light", "dark"})
        self.assertTrue({"850x700", "1100x700", "1300x700", "1440x900"}.issubset({s["size"] for s in scenarios}))
        self.assertTrue({"overview", "git", "worktrees", "local-files", "cleanup", "plan", "freeze", "shelf"}.issubset({s["view"] for s in scenarios}))
        self.assertEqual({s["scope"] for s in scenarios}, {"shown", "hidden"})
        self.assertEqual({s["rail"] for s in scenarios}, {"compact", "full"})
        self.assertTrue({"normal", "long", "empty"}.issubset({s["data"] for s in scenarios}))
        self.assertEqual(len({s["name"] for s in scenarios}), len(scenarios))
        minimum = [row for row in scenarios if row["size"] == "850x700"]
        self.assertTrue(minimum)
        self.assertTrue(all((row["scope"], row["rail"]) == ("hidden", "compact") for row in minimum))
        self.assertEqual({capture.expected_size(row) for row in scenarios},
                         {(850, 793), (1100, 793), (1300, 793), (1440, 900)})

    def test_changed_preferences_view_selects_all_shared_user_preference_scenarios(self):
        selected = capture.scenarios_for_changed_paths(["app/PreferencesViews.swift"])
        self.assertTrue(selected)
        self.assertEqual({row["name"] for row in selected}, {row["name"] for row in capture.scenarios()})

    def test_shared_type_definition_maps_cover_every_user_view(self):
        shared_types = ("OverviewView", "CleanupView", "PlanView", "OutcomeSummaryView", "ReviewOutcome",
                        "HistoryView", "WizardPrimaryButtonStyle", "TasksView", "SpaceData", "IglooPalette",
                        "UserPreferences")
        sources = {path.as_posix(): path.read_text()
                   for path in (ROOT / "app").glob("*.swift")}
        matrix = capture.scenarios()
        declaration = re.compile(r"\b(?:struct|class|enum|protocol|typealias)\s+({})\b")
        for name in shared_types:
            with self.subTest(type=name):
                definitions = [path for path, source in sources.items()
                               if re.search(declaration.pattern.format(re.escape(name)), source)]
                self.assertEqual(len(definitions), 1, f"expected one declaration for {name}")
                definition = definitions[0]
                definition_scenarios = {row["name"] for row in
                                        capture.scenarios_for_changed_paths([definition], matrix)}
                users = [path for path, source in sources.items()
                         if path != definition and re.search(rf"\b{re.escape(name)}\b", source)]
                self.assertTrue(users, f"expected at least one user of {name}")
                for user in users:
                    user_scenarios = {row["name"] for row in capture.scenarios_for_changed_paths([user], matrix)}
                    self.assertTrue(user_scenarios <= definition_scenarios,
                                    f"{name} in {definition} affects views not covered for {user}")

    def test_unmapped_swift_file_selects_the_full_matrix(self):
        selected = capture.scenarios_for_changed_paths(["app/NewUnmappedView.swift"])
        self.assertEqual({row["name"] for row in selected}, {row["name"] for row in capture.scenarios()})

    def test_since_selection_unions_changed_views_with_explicit_scenarios(self):
        matrix = capture.scenarios()
        explicit = next(row["name"] for row in matrix if row["view"] == "overview")
        resolved = subprocess.CompletedProcess(["git", "rev-parse"], 0, stdout="deadbeef\n", stderr="")
        empty = subprocess.CompletedProcess(["git"], 0, stdout="", stderr="")
        diff = subprocess.CompletedProcess(["git", "diff"], 0, stdout="app/Updates.swift\n", stderr="")
        with patch.object(capture.subprocess, "run", side_effect=[resolved, diff, empty]) as run:
            selected = capture.select_scenarios(matrix, explicit=[explicit], since="main")
        self.assertEqual({row["view"] for row in selected}, {"overview"} | {row["view"] for row in matrix
                                                                            if row["view"].startswith("update-")})
        self.assertEqual(run.call_count, 3)
        self.assertEqual(run.call_args.kwargs["check"], True)
        self.assertEqual(run.call_args_list[0].args[0],
                         ["git", "rev-parse", "--verify", "--end-of-options", "main^{commit}"])
        self.assertEqual(run.call_args_list[1].args[0],
                         ["git", "diff", "--name-only", "deadbeef", "--", "app"])

    def test_since_selection_returns_no_scenarios_for_non_view_changes(self):
        resolved = subprocess.CompletedProcess(["git", "rev-parse"], 0, stdout="deadbeef\n", stderr="")
        empty = subprocess.CompletedProcess(["git"], 0, stdout="", stderr="")
        with patch.object(capture.subprocess, "run", side_effect=[resolved, empty, empty]):
            selected = capture.select_scenarios(capture.scenarios(), since="main")
        self.assertEqual(selected, [])

    def test_since_rejects_an_unverified_revision_before_diff(self):
        error = subprocess.CalledProcessError(128, ["git", "rev-parse"], stderr="bad revision")
        with patch.object(capture.subprocess, "run", side_effect=error) as run:
            with self.assertRaises(subprocess.CalledProcessError):
                capture.changed_app_paths("--output")
        run.assert_called_once_with(["git", "rev-parse", "--verify", "--end-of-options",
                                     "--output^{commit}"], cwd=capture.ROOT, check=True,
                                    capture_output=True, text=True)

    def test_thaw_interactive_scenario_is_in_both_appearances(self):
        flow_rows = [row for row in capture.scenarios() if row.get("flow") == "wizard-thaw"]
        self.assertEqual({row["appearance"] for row in flow_rows}, {"light", "dark"})
        self.assertTrue(all(row["view"] == "wizard-start" for row in flow_rows))

    def test_interactive_thaw_steps_are_declarative_and_parseable(self):
        steps = capture.parse_interactive_steps(capture.INTERACTIVE_FLOWS["wizard-thaw"])
        self.assertEqual([step.get("key", step.get("click", {}).get("role")) for step in steps], [
            "button", "checkbox", "button", "checkbox", "button",
        ])
        self.assertTrue(all(step["click"].get("anchor") for step in steps if "click" in step))
        self.assertEqual(steps[3]["capture"], "05-confirmed")
        self.assertEqual(steps[1]["click"]["parent"], {"role": "outline row", "ordinal": 1})
        self.assertEqual(steps[-1]["wait_for"], {"anchor": "Results"})
        with self.assertRaisesRegex(ValueError, "anchor"):
            capture.parse_interactive_steps([{"click": {"role": "button", "ordinal": 1}, "capture": "bad"}])
        with self.assertRaisesRegex(ValueError, "ordinal"):
            capture.parse_interactive_steps([{"click": {"role": "button", "ordinal": 0, "anchor": "Choose a path"},
                                             "capture": "bad"}])
        with self.assertRaisesRegex(ValueError, "label"):
            capture.parse_interactive_steps([{"click": {"role": "outline row", "ordinal": 1,
                                                          "anchor": "Thaw plan", "label": " "},
                                             "capture": "bad"}])
        with self.assertRaisesRegex(ValueError, "selection"):
            capture.parse_interactive_steps([{"key": "Down", "capture": "bad"}])
        with self.assertRaisesRegex(ValueError, "key"):
            capture.parse_interactive_steps([{"key": "Space", "capture": "bad",
                                               "wait_for_selection": {"role": "outline row", "ordinal": 1,
                                                                      "anchor": "Thaw plan", "label": "Signal Lab"}}])

    def test_process_stop_requires_the_target_bundle_executable(self):
        with tempfile.TemporaryDirectory() as temp:
            app = Path(temp) / "Igloo.app"
            executable = app / "Contents/MacOS/Igloo"
            executable.parent.mkdir(parents=True)
            executable.touch()
            other_executable = Path(temp) / "Other.app/Contents/MacOS/Igloo"
            other_executable.parent.mkdir(parents=True)
            other_executable.touch()
            with patch.object(capture, "process_executable_path", return_value=other_executable), \
                 patch.object(capture.os, "kill") as kill:
                with self.assertRaisesRegex(ValueError, "executable path"):
                    capture.stop_verified_process(123, app)
                kill.assert_called_once_with(123, 0)

    def test_launch_orca_stops_the_verified_pid_after_success(self):
        with tempfile.TemporaryDirectory() as temp:
            app = Path(temp) / "Igloo.app"
            executable = app / "Contents/MacOS/Igloo"
            executable.parent.mkdir(parents=True)
            executable.touch()
            process = Mock()
            scenario = capture.scenarios()[0]
            results = [{"kind": "window", "status": "PARTIAL"}]
            with patch.object(capture.sys, "platform", "darwin"), \
                 patch.object(capture.subprocess, "Popen", return_value=process), \
                 patch.object(capture, "_wait_for_launched_capture", return_value=123), \
                 patch.object(capture, "process_executable_path", return_value=executable), \
                 patch.object(capture.os, "kill") as kill, \
                 patch.object(capture, "_capture_orca_states", return_value=results):
                self.assertIs(capture.launch_orca(app, Path(temp) / "scenario", scenario), results)
            self.assertEqual(kill.call_args_list, [call(123, 0), call(123, signal.SIGTERM)])
            process.wait.assert_called_once_with(timeout=20)

    def test_launch_orca_stops_the_verified_pid_after_capture_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            app = Path(temp) / "Igloo.app"
            executable = app / "Contents/MacOS/Igloo"
            executable.parent.mkdir(parents=True)
            executable.touch()
            process = Mock()
            scenario = capture.scenarios()[0]
            with patch.object(capture.sys, "platform", "darwin"), \
                 patch.object(capture.subprocess, "Popen", return_value=process), \
                 patch.object(capture, "_wait_for_launched_capture", return_value=123), \
                 patch.object(capture, "process_executable_path", return_value=executable), \
                 patch.object(capture.os, "kill") as kill, \
                 patch.object(capture, "_capture_orca_states", side_effect=capture.OrcaError("capture failed")):
                with self.assertRaisesRegex(capture.OrcaError, "capture failed"):
                    capture.launch_orca(app, Path(temp) / "scenario", scenario)
            self.assertEqual(kill.call_args_list, [call(123, 0), call(123, signal.SIGTERM)])
            process.wait.assert_called_once_with(timeout=20)

    def test_launch_orca_stops_the_started_pid_after_metadata_timeout(self):
        with tempfile.TemporaryDirectory() as temp:
            app = Path(temp) / "Igloo.app"
            executable = app / "Contents/MacOS/Igloo"
            executable.parent.mkdir(parents=True)
            executable.touch()
            folder = Path(temp) / "scenario"
            folder.mkdir()
            (folder / "started.json").write_text(json.dumps({"pid": 123}))
            process = Mock()
            scenario = capture.scenarios()[0]
            with patch.object(capture.sys, "platform", "darwin"), \
                 patch.object(capture.subprocess, "Popen", return_value=process), \
                 patch.object(capture, "_wait_for_launched_capture",
                              side_effect=capture.OrcaError("Timed out waiting for Orca capture metadata")), \
                 patch.object(capture, "process_executable_path", return_value=executable), \
                 patch.object(capture.os, "kill") as kill:
                with self.assertRaisesRegex(capture.OrcaError, "Timed out"):
                    capture.launch_orca(app, folder, scenario)
            self.assertEqual(kill.call_args_list, [call(123, 0), call(123, signal.SIGTERM)])
            process.wait.assert_called_once_with(timeout=20)

    def test_capture_orca_states_runs_and_verifies_every_interactive_step(self):
        with tempfile.TemporaryDirectory() as temp:
            app, folder = Path(temp) / "Igloo.app", Path(temp) / "scenario"
            scenario = {"flow": "wizard-thaw"}
            with patch.object(capture, "verify_target_process"), \
                 patch.object(capture, "_orca_state_for_sheet", return_value=({}, [])), \
                 patch.object(capture, "capture_orca_screenshot", side_effect=[{"step": i} for i in range(6)]) as shot, \
                 patch.object(capture, "_click_orca_locator") as click, \
                 patch.object(capture, "_press_orca_key") as press_key, \
                 patch.object(capture, "_wait_for_anchor", return_value={}) as wait, \
                 patch.object(capture, "_verify_step_state") as verify:
                results = capture._capture_orca_states(123, app, folder, scenario)
            self.assertEqual(len(results), 6)
            self.assertEqual(click.call_count, 5)
            press_key.assert_not_called()
            self.assertEqual(wait.call_count, 3)
            self.assertEqual(verify.call_count, 5)
            self.assertEqual(shot.call_count, 6)

    def test_verify_step_state_checks_value_control_and_visible_labels(self):
        approval = {"role": "checkbox", "ordinal": 1, "anchor": "Thaw plan"}
        execute = {"role": "button", "ordinal": 3, "anchor": "Thaw plan"}
        step = {"click": approval, "expect": {"value": True},
                "expect_control": {"locator": execute, "enabled": True},
                "expect_labels": ["Thaw plan"]}
        state = orca_state(ORCA_THAW_REVIEW.replace("Value: 0", "Value: 1")
                           .replace("button (disabled) (Thaw 1 ready project)", "button (Thaw 1 ready project)"))
        self.assertIsNone(capture._verify_step_state(state, step))
        bad_states = (
            orca_state(ORCA_THAW_REVIEW),
            orca_state(ORCA_THAW_REVIEW.replace("button (disabled) (Thaw 1 ready project)",
                                                "button (Thaw 1 ready project)")),
            orca_state(ORCA_THAW_REVIEW.replace("text Thaw plan", "text Different screen")),
        )
        for bad_state in bad_states:
            with self.subTest(state=bad_state), self.assertRaises(capture.OrcaError):
                capture._verify_step_state(bad_state, step)

    def test_orca_locator_uses_sheet_anchor_role_and_ordinal(self):
        locator = {"role": "button", "ordinal": 2, "anchor": "Choose a path"}
        node = capture.find_orca_locator(orca_state(ORCA_WIZARD_START), locator)
        self.assertEqual(capture._element_index(node), 198)
        self.assertIsNone(capture.find_orca_locator(orca_state(ORCA_WIZARD_START),
                                                    {"role": "button", "ordinal": 4, "anchor": "Choose a path"}))
        self.assertIsNone(capture.find_orca_locator(orca_state(ORCA_WIZARD_START),
                                                    {"role": "button", "ordinal": 2, "anchor": "Select projects"}))

    def test_orca_locator_resolves_a_role_inside_a_nested_outline_row(self):
        locator = {"role": "checkbox", "ordinal": 1, "parent": {"role": "outline row", "ordinal": 1},
                   "anchor": "Select projects"}
        node = capture.find_orca_locator(orca_state(ORCA_THAW_SELECTION), locator)
        self.assertEqual(capture._element_index(node), 210)
        self.assertFalse(node["value"])

    def test_orca_locator_anchors_row_by_label_and_ordinal(self):
        locator = {"role": "outline row", "ordinal": 1, "anchor": "Thaw plan", "label": "Harbor CLI"}
        node = capture.find_orca_locator(orca_state(ORCA_THAW_REVIEW), locator)
        self.assertEqual(capture._element_index(node), 303)
        self.assertIsNone(capture.find_orca_locator(orca_state(ORCA_THAW_REVIEW),
                                                    {**locator, "label": "Other project"}))

    def test_orca_tree_marks_the_selected_row(self):
        locator = {"role": "outline row", "ordinal": 1, "anchor": "Thaw plan", "label": "Harbor CLI"}
        node = capture.find_orca_locator(orca_state(ORCA_THAW_REVIEW), locator)
        self.assertTrue(node["selected"])

    def test_press_key_targets_the_app_without_coordinate_or_window_clicks(self):
        with patch.object(capture, "verify_target_process") as verify, \
             patch.object(capture, "orca_json") as orca:
            capture._press_orca_key(123, Path("/example/Igloo.app"), "Down")
        verify.assert_called_once_with(123, Path("/example/Igloo.app"))
        orca.assert_called_once_with(["press-key", "--app", "pid:123", "--key", "Down", "--no-screenshot"])

    def test_wait_for_selection_reports_open_when_key_does_not_change_selection(self):
        locator = {"role": "outline row", "ordinal": 1, "anchor": "Thaw plan", "label": "Signal Lab"}
        with patch.object(capture, "verify_target_process"), \
             patch.object(capture, "_orca_state_for_sheet", return_value=(
                 orca_state(ORCA_THAW_REVIEW), ["--window-id", "66605"])), \
             patch.object(capture.time, "monotonic", return_value=10):
            with self.assertRaisesRegex(capture.OrcaError,
                                        "Orca key Down did not change selection to Signal Lab"):
                capture._wait_for_selection(123, Path("/example/Igloo.app"), "Down", locator, timeout=0)

    def test_wait_for_selection_captures_the_requested_selected_row(self):
        locator = {"role": "outline row", "ordinal": 1, "anchor": "Thaw plan", "label": "Signal Lab"}
        expected = orca_state(ORCA_THAW_BLOCKED)
        with patch.object(capture, "verify_target_process") as verify, \
             patch.object(capture, "_orca_state_for_sheet", return_value=(expected, ["--window-id", "66605"])), \
             patch.object(capture, "orca_app_state", return_value=expected) as screenshot:
            state = capture._wait_for_selection(123, Path("/example/Igloo.app"), "Down", locator, timeout=0)
        self.assertIs(state, expected)
        self.assertEqual(verify.call_count, 2)
        screenshot.assert_called_once_with(123, window=["--window-id", "66605"])

    def test_orca_click_re_resolves_the_element_index_from_each_snapshot(self):
        shifted = ORCA_WIZARD_START
        for before, after in ((193, 300), (194, 301), (195, 302), (196, 303), (197, 317), (198, 318), (199, 319)):
            shifted = shifted.replace(str(before), str(after), 1)
        locator = {"role": "button", "ordinal": 2, "anchor": "Choose a path"}
        windows = {"result": {"windows": [
            {"id": 66602, "title": "Igloo", "isMain": True},
            {"id": 66605, "title": "Cleanup wizard", "role": "sheet", "isMain": False},
        ]}}
        state = {"result": {"snapshot": {"treeText": shifted, "window": {"windowId": 66605}}}}
        with patch.object(capture, "verify_target_process") as verify, \
             patch.object(capture, "orca_json", side_effect=[windows, state, {}]) as orca:
            capture._click_orca_locator(123, Path("/example/Igloo.app"), locator)
        self.assertEqual(verify.call_count, 2)
        self.assertEqual(orca.call_args_list[0], call(["list-windows", "--app", "pid:123"]))
        self.assertEqual(orca.call_args_list[1], call(["get-app-state", "--app", "pid:123",
                                                       "--window-id", "66605", "--no-screenshot"]))
        self.assertEqual(orca.call_args_list[2], call(["click", "--app", "pid:123", "--window-id", "66605",
                                                       "--element-index", "318"]))

    def test_orca_command_follows_restore_window_suggestion_once_and_keeps_real_error(self):
        failed = {"ok": False, "error": {"code": "window_not_focused",
                  "message": "coordinate click aborted because the target window is not topmost",
                  "data": {"nextSteps": ["Retry with --restore-window"]}}}
        success = {"ok": True, "result": {"clicked": True}}
        with patch.object(capture.subprocess, "run", side_effect=[
                subprocess.CompletedProcess(["orca"], 1, stdout=json.dumps(failed), stderr=""),
                subprocess.CompletedProcess(["orca"], 0, stdout=json.dumps(success), stderr="")]) as run:
            self.assertEqual(capture.orca_json(["click", "--app", "pid:123", "--window-id", "66605"]), success)
        self.assertEqual(run.call_count, 2)
        self.assertIn("--restore-window", run.call_args_list[1].args[0])
        self.assertEqual(run.call_args_list[1].args[0].count("--restore-window"), 1)

    def test_orca_command_reports_code_and_message_after_restore_retry(self):
        failed = {"ok": False, "error": {"code": "window_not_focused",
                  "message": "target window is no longer topmost",
                  "data": {"nextSteps": ["Retry with --restore-window"]}}}
        with patch.object(capture.subprocess, "run", side_effect=[
                subprocess.CompletedProcess(["orca"], 1, stdout=json.dumps(failed), stderr=""),
                subprocess.CompletedProcess(["orca"], 1, stdout=json.dumps(failed), stderr="")]):
            with self.assertRaisesRegex(capture.OrcaError, "window_not_focused: target window is no longer topmost"):
                capture.orca_json(["click", "--app", "pid:123", "--window-id", "66605"])

    def test_wait_for_anchor_polls_without_screenshots_then_refreshes(self):
        with patch.object(capture, "verify_target_process"), \
             patch.object(capture, "_orca_state_for_sheet", side_effect=[
                 capture.OrcaError("Orca could not find the Select projects sheet in a listed window"),
                 (orca_state(ORCA_THAW_SELECTION), ["--window-id", "66605"]),
                 ({"result": {"screenshot": {"path": "captured.png"}}}, ["--window-id", "66605"])]) as state, \
             patch.object(capture.time, "sleep") as sleep:
            result = capture._wait_for_anchor(123, Path("/example/Igloo.app"), "Select projects")
        self.assertEqual(result["result"]["screenshot"]["path"], "captured.png")
        self.assertEqual(state.call_args_list, [call(123, "Select projects", include_screenshot=False),
                                                call(123, "Select projects", include_screenshot=False),
                                                call(123, "Select projects")])
        sleep.assert_called_once_with(0.5)

    def test_orca_probe_extracts_only_the_sheet_subtree(self):
        self.assertEqual(capture.orca_sheet_subtree(ORCA_WIZARD_START), ORCA_WIZARD_START.rstrip())

    def test_probe_option_is_documented_by_the_capture_cli(self):
        output = io.StringIO()
        with patch.object(capture.sys, "argv", ["capture_states.py", "--help"]), \
             contextlib.redirect_stdout(output), self.assertRaises(SystemExit) as raised:
            capture.main()
        self.assertEqual(raised.exception.code, 0)
        self.assertIn("--probe", output.getvalue())

    def test_orca_command_failure_is_not_suppressed(self):
        with patch.object(capture.subprocess, "run", return_value=subprocess.CompletedProcess(
                ["orca"], 1, stdout="", stderr="permission denied")):
            with self.assertRaisesRegex(capture.OrcaError, r"Orca command failed \(exit 1\)"):
                capture.orca_app_state(123)

    def test_orca_screenshot_is_saved_as_private_real_pixel_evidence(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            source = folder / "orca-source.png"
            source_bytes = b"\x89PNG\r\n\x1a\n" + b"x" * 2048
            source.write_bytes(source_bytes)
            state = {"result": {"screenshot": {"path": str(source)}}}
            with patch.object(capture, "png_nonbackground_share", return_value=0.25):
                result = capture.capture_orca_screenshot(state, folder, "window-orca.png")
            output = folder / "window-orca.png"
            self.assertEqual(output.read_bytes(), source_bytes)
            self.assertFalse(source.exists(), "the Orca temporary screenshot is removed after copying")
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)
            self.assertEqual(result["status"], "PARTIAL")
            self.assertEqual(result["path"], "window-orca.png")

    def test_orca_missing_screenshot_is_open(self):
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaisesRegex(capture.OrcaError, "Orca returned no screenshot"):
                capture.capture_orca_screenshot({"result": {"snapshot": {}}}, Path(temp), "window-orca.png")

    def test_orca_failure_is_reported_open(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            app = root / "Igloo.app"
            executable = app / "Contents/MacOS/Igloo"
            executable.parent.mkdir(parents=True)
            executable.touch()
            output = root / "output"
            scenario = capture.scenarios()[0]
            args = ["capture_states", "--app", str(app), "--output", str(output), "--scenario", scenario["name"],
                    "--backend", "orca"]
            text = io.StringIO()
            with patch.object(sys, "argv", args), patch.object(capture.sys, "platform", "darwin"), \
                 patch.object(capture, "launch_orca", side_effect=capture.OrcaError("window unavailable")), \
                 contextlib.redirect_stdout(text):
                self.assertEqual(capture.main(), 1)
            self.assertIn("OPEN " + scenario["name"], text.getvalue())
            self.assertNotIn("PASS " + scenario["name"], text.getvalue())

    def test_review_and_settings_matrix_includes_minimum_and_default_in_both_appearances(self):
        views = {"freeze-review", "preserve-review", "freeze-result", "cleanup-review", "cleanup-result",
                 "trash-history", "wizard-review", "wizard-result", "task-review", "task-history",
                 "settings", "hold", "freeze", "shelf"}
        scenarios = capture.scenarios()
        for view in views:
            for appearance in ("light", "dark"):
                for size in ("850x700", "1440x900"):
                    with self.subTest(view=view, appearance=appearance, size=size):
                        self.assertTrue(any(s["view"] == view and s["appearance"] == appearance and s["size"] == size
                                            for s in scenarios))

    def test_review_sheet_requires_sheet_artifact_and_metadata(self):
        scenario = next(s for s in capture.scenarios() if s["view"] == "cleanup-review" and s["size"] == "1440x900")
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            write_renderer_png(folder, "window", sample_png())
            (folder / "started.json").write_text(json.dumps({"pid": 123}))
            write_window_metadata(folder, 123)
            (folder / "accessibility.json").write_text(json.dumps({"pid": 123, "tree": {"role": "AXWindow"},
                                                                    "actualWidth": 1440, "actualHeight": 900}))
            with self.assertRaisesRegex(ValueError, "sheet"):
                capture.validate_result(folder, scenario)

    def test_sheet_evidence_is_reported_independently_of_black_window(self):
        scenario = next(s for s in capture.scenarios() if s["view"] == "freeze")
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            write_renderer_png(folder, "window", sample_png(uniform=True))
            write_renderer_png(folder, "sheet", sample_png())
            (folder / "started.json").write_text(json.dumps({"pid": 123}))
            write_window_metadata(folder, 123, sheet=True)
            (folder / "accessibility.json").write_text(json.dumps({"pid": 123, "tree": {"role": "AXSheet"},
                                                                    "actualWidth": 1440, "actualHeight": 900}))
            result = capture.validate_sheet_result(folder, scenario)
            self.assertGreater(result["sheetNonBackgroundShare"], 0.01)
            with self.assertRaisesRegex(ValueError, "uniform"):
                capture.validate_result(folder, scenario)

    def test_demo_projects_have_checklist_and_hold_data(self):
        catalog = json.loads((ROOT / "assets/demo-catalog.json").read_text())
        for project in catalog["projects"]:
            with self.subTest(project=project["name"]):
                self.assertIsInstance(project.get("blockers"), list)
                self.assertIsInstance(project.get("hold"), bool)

    def test_update_failure_scenarios_cover_both_appearances(self):
        expected = {"quota", "quota-no-reset", "forbidden", "offline", "timeout", "server", "malformed", "cancelled-download", "digest-mismatch"}
        captures = [row for row in capture.scenarios()
                    if row["view"].startswith("update-") and row["view"] not in {"update-markdown", "update-ready", "update-install-failed"}]
        self.assertEqual({row["view"].removeprefix("update-") for row in captures}, expected)
        for failure in expected:
            states = [row for row in captures if row["view"] == "update-" + failure]
            self.assertEqual({row["appearance"] for row in states}, {"light", "dark"})
            self.assertEqual({row["size"] for row in states}, {"850x700", "1440x900"})

    def test_markdown_update_sheet_covers_minimum_and_default_sizes(self):
        captures = [row for row in capture.scenarios() if row["view"] == "update-markdown"]
        self.assertEqual({(row["appearance"], row["size"]) for row in captures},
                         {(appearance, size) for appearance in ("light", "dark")
                          for size in ("850x700", "1440x900")})

    def test_install_failed_update_sheet_covers_minimum_and_default_sizes(self):
        captures = [row for row in capture.scenarios() if row["view"] == "update-install-failed"]
        self.assertEqual({(row["appearance"], row["size"]) for row in captures},
                         {(appearance, size) for appearance in ("light", "dark")
                          for size in ("850x700", "1440x900")})

    def test_ready_update_sheet_covers_light_and_dark(self):
        captures = [row for row in capture.scenarios() if row["view"] == "update-ready"]
        self.assertEqual({(row["appearance"], row["size"]) for row in captures},
                         {(appearance, "1440x900") for appearance in ("light", "dark")})

    def test_preservation_detail_covers_scope_minimum_and_default_sizes(self):
        captures = [row for row in capture.scenarios() if row["view"] == "plan" and row["scope"] == "shown"]
        self.assertTrue({(appearance, size) for appearance in ("light", "dark")
                         for size in ("1300x700", "1440x900")}.issubset(
                             {(row["appearance"], row["size"]) for row in captures}))

    def test_overview_covers_scope_visible_minimum_and_default_sizes(self):
        captures = [row for row in capture.scenarios() if row["view"] == "overview" and row["scope"] == "shown"]
        self.assertTrue({(appearance, size) for appearance in ("light", "dark")
                         for size in ("1300x700", "1440x900")}.issubset(
                             {(row["appearance"], row["size"]) for row in captures}))


    def test_new_review_accessibility_reports_missing_controls(self):
        expected = {"cleanup-review": "Review cleanup", "wizard-review": "Expected outcome",
                    "wizard-start": "Make space", "wizard-thaw-selection": "Create Thaw plan",
                    "wizard-thaw-review": "Thaw plan", "wizard-thaw-result": "Checkouts restored",
                    "wizard-thaw-rehydrating": "Thawing Ice Garden",
                    "task-review": "Review task actions", "settings": "Freezer backup folder",
                    "freeze-review": "Expected outcome", "freeze-hold": "This project is on Hold",
                    "cleanup-result": "In Trash (can be restored)", "trash-history": "Space report",
                    "wizard-result": "In Trash (can be restored)", "task-history": "Task run history"}
        for view, label in expected.items():
            with self.subTest(view=view):
                self.assertIn(label, capture.accessibility_missing({"role": "AXSheet"}, {"view": view}))

    def test_thaw_blocker_capture_contracts_include_short_reasons_and_action_buttons(self):
        expected = {
            "wizard-thaw-occupied": ["Thaw plan", "Path occupied", "Show path in Finder"],
            "wizard-thaw-remote": ["Thaw plan", "Remote unavailable", "Check remote again"],
            "wizard-thaw-corrupt": ["Thaw plan", "Archive needs attention", "Open Freezer"],
            "wizard-thaw-not-frozen": ["Thaw plan", "No longer Frozen", "Open Freezer"],
        }
        empty_tree = {"role": "AXSheet"}
        for view, labels in expected.items():
            with self.subTest(view=view):
                missing = capture.accessibility_missing(empty_tree, {"view": view})
                self.assertEqual(missing, labels)

    def test_thaw_wizard_steps_and_blockers_have_minimum_and_default_captures(self):
        views = {"wizard-start", "wizard-thaw-selection", "wizard-thaw-review", "wizard-thaw-ready", "wizard-thaw-result",
                 "wizard-thaw-occupied", "wizard-thaw-remote", "wizard-thaw-corrupt", "wizard-thaw-not-frozen",
                 "wizard-thaw-rehydrating"}
        captures = [row for row in capture.scenarios() if row["view"] in views]
        self.assertEqual({row["view"] for row in captures}, views)
        self.assertTrue({(view, appearance, size) for view in views for appearance in ("light", "dark")
                         for size in ("850x700", "1440x900")}.issubset(
                             {(row["view"], row["appearance"], row["size"]) for row in captures}))

    def test_thaw_ready_capture_covers_unchecked_confirmation_in_both_appearances(self):
        captures = [row for row in capture.scenarios() if row["view"] == "wizard-thaw-ready"]
        self.assertEqual({(row["appearance"], row["size"]) for row in captures},
                         {(appearance, size) for appearance in ("light", "dark")
                          for size in ("850x700", "1440x900")})
        source = (ROOT / "app/DemoCatalog.swift").read_text()
        wizard = (ROOT / "app/WizardViews.swift").read_text()
        self.assertIn('"wizard-thaw-ready"', source)
        self.assertIn('"wizard-thaw-ready": "demo-icegarden"', wizard)

    def test_thaw_ready_capture_contract_checks_checkbox_and_disabled_button(self):
        def control(identifier, role, *, enabled, value=None):
            node = {"identifier": identifier, "role": role, "label": identifier, "enabled": enabled,
                    "frame": {"x": 1, "y": 2, "width": 40, "height": 20}}
            if value is not None:
                node["value"] = value
            return node

        tree = {"role": "AXCapture", "children": [
            {"label": "Thaw projects"}, {"label": "Only ready projects will run"},
            {"label": "Ice Garden"}, {"label": "Thaw 1 ready project"},
            control("wizard.thaw.acceptance", "AXCheckBox", enabled=True, value=False),
            control("wizard.thaw.execute", "AXButton", enabled=False),
        ]}
        tree["children"][-2]["label"] = "I reviewed the remote refs, original paths and local-only files shown for Ice Garden."
        self.assertEqual(capture.accessibility_missing(tree, {"view": "wizard-thaw-ready"}), [])
        self.assertEqual(capture.accessibility_control_issues(tree, {"view": "wizard-thaw-ready"}), [])
        tree["children"][-1]["enabled"] = True
        self.assertIn("wizard.thaw.execute", capture.accessibility_control_issues(tree, {"view": "wizard-thaw-ready"}))
        tree["children"][-1]["enabled"] = False
        tree["children"][-2]["value"] = True
        self.assertIn("wizard.thaw.acceptance", capture.accessibility_control_issues(tree, {"view": "wizard-thaw-ready"}))

    def test_demo_capture_activates_and_keys_its_window_before_capture(self):
        source = (ROOT / "app/DemoCatalog.swift").read_text()
        capture_start = source.split("static func start(store:", 1)[1].split("private static func windowMetadata", 1)[0]
        self.assertIn("NSApp.activate(ignoringOtherApps: true)", capture_start)
        self.assertIn("window.makeKeyAndOrderFront(nil)", capture_start)
        self.assertLess(capture_start.index("window.makeKeyAndOrderFront(nil)"), capture_start.index("view.layoutSubtreeIfNeeded()"))
        self.assertIn("NSApp.isActive && (target.isKeyWindow || window.isKeyWindow || target.attachedSheet?.isKeyWindow == true)", capture_start)
        self.assertLess(capture_start.index("NSApp.isActive && (target.isKeyWindow || window.isKeyWindow || target.attachedSheet?.isKeyWindow == true)"),
                        capture_start.index("view.layoutSubtreeIfNeeded()"))

    def test_update_sheet_focuses_the_default_action_and_skips_the_release_link(self):
        source = (ROOT / "app/Updates.swift").read_text()
        view = source.split("struct UpdatesView: View {", 1)[1].split("private struct ReleaseNotesView", 1)[0]
        self.assertIn("@FocusState private var defaultActionFocused: Bool", view)
        self.assertIn(".focusable(false)", view)
        self.assertEqual(view.count(".defaultFocus($defaultActionFocused, true)"), 2)

    def test_capture_review_documents_matrix_geometry_and_measured_contrast(self):
        guide = (ROOT / "docs/development.md").read_text()
        checklist = (ROOT / "docs/capture-review.md").read_text()
        self.assertIn("`850x700` request captures at 850 × 793", guide)
        self.assertIn("`1100x700` request captures at 1100 × 793", guide)
        self.assertIn("`1300x700` request captures at 1300 × 793", guide)
        self.assertIn("`1440x900` request captures at 1440 × 900", guide)
        self.assertIn("2.29:1", checklist)
        self.assertIn("3.02:1", checklist)
        self.assertIn("5.22:1", checklist)
        self.assertIn("7.67:1", checklist)
        self.assertIn("1.77:1", checklist)
        self.assertIn("1.00:1", checklist)
        self.assertIn("10.22:1", checklist)
        self.assertIn("4.13:1", checklist)
        self.assertIn("3.65:1", checklist)
        self.assertIn("Inactive parent window", checklist)
        self.assertIn("inactive user-interface components", checklist)
        self.assertIn("WCAG relative-luminance formula", checklist)

    def test_issue_50_review_panes_freezer_tables_and_disabled_actions_have_explanations(self):
        lifecycle = (ROOT / "app/LifecycleViews.swift").read_text()
        review = lifecycle.split("} else if let review = plan ?? captureReview {", 1)[1].split(
            '} else {\n                Picker("Action"', 1)[0]
        self.assertIn("alignment: .topLeading", lifecycle)
        self.assertIn("reviewRows(review.localFiles", review)
        self.assertIn("reviewRows(review.excluded", review)
        self.assertIn("reviewRows(review.checkouts.map", review)
        freezer = lifecycle.split("struct FrozenProjectsView", 1)[1]
        table = freezer.split("@ViewBuilder private func archiveTable", 1)[1].split(
            "@ViewBuilder private func backupCell", 1)[0]
        self.assertIn("if archives.isEmpty", table)
        self.assertNotIn(".overlay", table)
        self.assertIn('TableColumn("Backup") { archive in', table)
        self.assertIn(".frame(height: archiveTableHeight(archives))", table)
        self.assertIn("private func archiveTableHeight(_ archives: [FrozenArchive])", freezer)
        self.assertIn("archiveRowHeight(for: archive)", freezer)
        self.assertIn("}.width(min: 150)", table)
        backup_cell = freezer.split("@ViewBuilder private func backupCell", 1)[1].split(
            "private func emptyArchiveState", 1)[0]
        self.assertIn(".lineLimit(2)", backup_cell)
        self.assertIn(".help(([description, reason]", backup_cell)
        self.assertIn(".frame(height: orphanRowsHeight(backup.orphans))", freezer)
        self.assertIn("private func orphanRowsHeight(_ orphans: [FreezerBackupRow])", freezer)
        self.assertIn(".frame(height: reviewHistoryPaneHeight(review), alignment: .top)", review)
        self.assertIn(".frame(height: reviewOutcomeHeight(review.outcome), alignment: .top)", review)
        self.assertIn("private func reviewHistoryPaneHeight(_ review: PreservationReview)", lifecycle)
        self.assertIn("private func reviewPaneHeight(_ review: PreservationReview)", lifecycle)
        self.assertIn("private func reviewOutcomeHeight(_ outcome: ReviewOutcome)", lifecycle)
        for control in ("shelf.removeBackupCopy", "shelf.backUpNow", "shelf.verifyBackup", "shelf.showArchive",
                        "shelf.freeze", "shelf.offlineRecovery", "shelf.thawRemote"):
            with self.subTest(control=control):
                position = freezer.index(f'"{control}"')
                self.assertIn(".help(", freezer[max(0, position - 500):position])

    def test_overview_summary_and_list_badge_read_one_blocker_source(self):
        model = (ROOT / "app/Igloo.swift").read_text()
        detail = (ROOT / "app/DetailViews.swift").read_text()
        summary = model[model.index("var overviewSummary"):model.index("struct Project: Decodable")]
        self.assertIn("let open = blockers ?? []", summary)
        self.assertIn('"Blocked: " + first.shortLabel', summary)
        self.assertLess(summary.index("open.first"), summary.index('"Ready for a closer review"'))
        self.assertIn("ProjectBlockerBadges(blockers: project.blockers ?? []", model)
        self.assertIn("project.overviewSummary.title", detail)
        self.assertNotIn("no immediate preservation flags", detail)

    def test_issue_50_warning_and_selected_row_colors_use_contrasting_source_styles(self):
        navigation = (ROOT / "app/NavigationViews.swift").read_text()
        wizard = (ROOT / "app/WizardViews.swift").read_text()
        self.assertIn("Color(red: 0.55, green: 0.23, blue: 0.00)", navigation)
        self.assertIn(".foregroundStyle(blockerColor)", navigation)
        self.assertIn("Color(red: 0.55, green: 0.23, blue: 0.00)", wizard)
        self.assertIn("@Environment(\\.backgroundProminence) private var backgroundProminence", navigation)
        self.assertIn("selected && backgroundProminence == .increased", navigation)
        self.assertIn("@Environment(\\.controlActiveState) private var controlActiveState", wizard)
        self.assertIn("row.id == selectedThawID && controlActiveState == .key", wizard)
        self.assertIn("Color(nsColor: .selectedMenuItemTextColor)", navigation)
        self.assertIn("selectedThawID && controlActiveState == .key ? selectedRowTextColor", wizard)

    def test_thaw_wizard_capture_contract_uses_shared_height_and_scrollable_list(self):
        source = (ROOT / "app/WizardViews.swift").read_text()
        height = source.split("private var wizardSheetHeight: CGFloat {", 1)[1].split("private var stepTitle", 1)[0]
        self.assertIn('if mode == "thaw"', height)
        self.assertNotIn("thawPhase ==", height)
        self.assertIn(".scrollIndicators(.visible)", source)
        review = source.split("var thawReviewStep: some View {", 1)[1].split("var thawRowsAndDetails", 1)[0]
        self.assertNotIn('Text("Thaw plan")', review)
        self.assertEqual(review.count("Blocked projects and their Freezer archives remain untouched."), 0)

    def test_thaw_review_keeps_refs_and_freezer_location_in_one_detail_pane(self):
        source = (ROOT / "app/WizardViews.swift").read_text()
        review = source.split("var thawReviewStep: some View {", 1)[1].split("var thawRowsAndDetails", 1)[0]
        details = source.split("@ViewBuilder func thawDetails(", 1)[1].split("func shortThawReason", 1)[0]
        self.assertNotIn("ForEach(row.refs)", review)
        self.assertNotIn("row.archiveLocation", review)
        self.assertEqual(details.count("ForEach(row.refs)"), 1)
        self.assertIn('Text("Freezer archive")', details)
        self.assertIn('Text(thawPlan?.message ?? "")', review)
        self.assertNotIn("Blocked projects and their Freezer archives remain untouched.", review)

    def test_disabled_wizard_buttons_and_thaw_progress_keep_visible_contrast(self):
        source = (ROOT / "app/WizardViews.swift").read_text()
        button = source.split("struct WizardPrimaryButtonStyle", 1)[1].split("struct CleanupWizardView", 1)[0]
        self.assertIn("isEnabled ? Color.white : Color.primary.opacity(0.28)", button)
        self.assertIn("isEnabled ? Color.accentColor : Color.primary.opacity(0.07)", button)
        progress = source.split("if isThawExecuting {", 1)[1].split("} else if thawPlan?.status", 1)[0]
        self.assertIn("Text(thawProgressMessage)", progress)
        self.assertIn("ProgressView().progressViewStyle(.linear)", progress)
        self.assertIn("Color(red: 0.08, green: 0.34, blue: 0.78)", progress)

    def test_result_sheets_use_sized_rows_and_reduced_result_frames(self):
        cleanup = (ROOT / "app/ReviewViews.swift").read_text()
        wizard = (ROOT / "app/WizardViews.swift").read_text()
        lifecycle = (ROOT / "app/LifecycleViews.swift").read_text()
        self.assertIn('item.size.map { " · " + bytes($0) }', cleanup)
        self.assertIn('item.size.map { " · " + bytes($0) }', wizard)
        self.assertIn("height: resultHeight", cleanup)
        self.assertIn("if isShowingResult { return 390 }", lifecycle)
        self.assertIn("reviewSheetChromeHeight + reviewPaneHeight(review) + reviewOutcomeHeight(review.outcome)", lifecycle)
        self.assertIn("Demo only: these results are simulated.", lifecycle)

    def test_outcome_summary_avoids_duplicate_measurements_and_labels_each_ref(self):
        source = (ROOT / "app/ReviewViews.swift").read_text()
        self.assertIn("return bytes(freed) == bytes(outcome.bytesFreedEstimate)", source)
        self.assertIn('"Ref pushed: "', source)
        self.assertIn("spaceExplanation", source)
        self.assertIn('"Total outcome for both actions"', (ROOT / "app/WizardViews.swift").read_text())

    def test_named_controls_require_identifiers_enabled_state_and_frame(self):
        requirements = {
            "plan": ["detail.connectRemote", "detail.commitSelectedFiles", "detail.preserve",
                      "detail.freeze", "detail.hold", "detail.preservationNotes",
                      "detail.exportProjectReport", "detail.savePreservation"],
            "freeze": ["freeze.cancel", "freeze.buildReview"],
            "shelf": ["shelf.backUpNow", "shelf.verifyBackup", "shelf.done", "shelf.showArchive",
                      "shelf.freeze", "shelf.offlineRecovery", "shelf.thawRemote"],
            "wizard-start": ["wizard.start.makeSpace", "wizard.start.thaw"],
            "wizard-thaw-selection": ["wizard.thaw.back", "wizard.thaw.prepare", "wizard.thaw.archive.demo-icegarden"],
            "wizard-thaw-review": ["wizard.thaw.acceptance", "wizard.thaw.execute"],
            "wizard-thaw-occupied": ["wizard.thaw.showPath"],
            "wizard-thaw-remote": ["wizard.thaw.checkRemote"],
            "wizard-thaw-corrupt": ["wizard.thaw.openFreezer"],
            "wizard-thaw-not-frozen": ["wizard.thaw.openFreezer"],
            "wizard-thaw-result": ["wizard.thaw.newPlan"],
        }
        for view, identifiers in requirements.items():
            with self.subTest(view=view):
                tree = {"role": "AXCapture", "children": [{
                    "role": "AXWindow", "windowKind": "sheet" if view != "plan" else "window",
                    "children": [{"role": {"detail.hold": "AXCheckBox",
                                             "wizard.thaw.archive.demo-icegarden": "AXCheckBox",
                                             "detail.preservationNotes": "AXTextArea"}.get(identifier, "AXButton"),
                                  "identifier": identifier, "label": identifier,
                                  "enabled": True,
                                  "frame": {"x": 1, "y": 2, "width": 40, "height": 20}}
                                 for identifier in identifiers]
                }]}
                self.assertEqual(capture.accessibility_control_issues(tree, {"view": view}), [])
                node = tree["children"][0]["children"][-1]
                node["identifier"] = "wrong.identifier"
                self.assertIn(identifiers[-1], capture.accessibility_control_issues(tree, {"view": view}))
                node["identifier"] = identifiers[-1]
                node["role"] = "AXTextField"
                self.assertIn(identifiers[-1], capture.accessibility_control_issues(tree, {"view": view}))
                node["role"] = "AXButton"
                node["enabled"] = None
                self.assertIn(identifiers[-1], capture.accessibility_control_issues(tree, {"view": view}))
                node["enabled"] = True
                del node["frame"]
                self.assertIn(identifiers[-1], capture.accessibility_control_issues(tree, {"view": view}))
        self.assertIsNone(capture.accessibility_control_issues({"children": []}, {"view": "overview"}))

    def test_accessibility_dump_counts_each_captured_window_tree(self):
        tree = {"role": "AXCapture", "children": [
            {"role": "AXWindow", "windowKind": "window", "children": [{"role": "AXButton"}]},
            {"role": "AXWindow", "windowKind": "sheet", "children": [{"role": "AXButton"}, {"role": "AXButton"}]},
        ]}
        self.assertEqual(capture.accessibility_node_counts(tree),
                         {"total": 5, "windows": {"window": 2, "sheet": 3}})

    def test_empty_repository_checklist_capture_exists_in_both_appearances_and_sizes(self):
        cases = [row for row in capture.scenarios() if row["view"] == "freeze-no-files"]
        self.assertEqual({(row["appearance"], row["size"]) for row in cases},
                         {(appearance, size) for appearance in ("light", "dark")
                          for size in ("850x700", "1440x900")})

    def test_capture_requires_real_png_and_accessibility_output(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            with self.assertRaises(ValueError):
                capture.validate_result(folder, capture.scenarios()[0])
            write_renderer_png(folder, "window", b"not a PNG")
            (folder / "accessibility.json").write_text(json.dumps({"pid": 1, "tree": {"role": "AXWindow"}}))
            with self.assertRaises(ValueError):
                capture.validate_result(folder, capture.scenarios()[0])

    def test_capture_reads_renderer_pngs_from_render_subfolder(self):
        scenario = next(s for s in capture.scenarios() if s["view"] == "overview" and s["size"] == "1440x900"
                        and s["data"] == "normal")
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            folder.chmod(0o700)
            render = folder / "render"
            render.mkdir(mode=0o700)
            write_renderer_png(folder, "window", sample_png())
            (folder / "started.json").write_text(json.dumps({"pid": 123}))
            write_window_metadata(folder, 123)
            (folder / "accessibility.json").write_text(json.dumps({"pid": 123, "tree": {"role": "AXWindow"},
                                                                    "actualWidth": 1440, "actualHeight": 900}))
            self.assertEqual(capture.validate_result(folder, scenario)["pid"], 123)

    def test_real_pixel_backend_keeps_placeholder_render_as_diagnostic(self):
        scenario = next(s for s in capture.scenarios() if s["view"] == "overview" and s["size"] == "1440x900"
                        and s["data"] == "normal")
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            write_renderer_png(folder, "window", sample_png(placeholder=True))
            (folder / "started.json").write_text(json.dumps({"pid": 123}))
            write_window_metadata(folder, 123)
            (folder / "accessibility.json").write_text(json.dumps({"pid": 123, "tree": {"role": "AXWindow"},
                                                                    "actualWidth": 1440, "actualHeight": 900}))
            result = capture.validate_result(folder, scenario, reject_renderer_placeholders=False)
            self.assertGreater(result["nonBackgroundShare"], 0.01)

    def test_real_pixel_backend_keeps_uniform_render_as_diagnostic(self):
        scenario = next(s for s in capture.scenarios() if s["view"] == "update-ready" and s["size"] == "1440x900")
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            write_renderer_png(folder, "window", sample_png(uniform=True))
            write_renderer_png(folder, "sheet", sample_png(uniform=True))
            (folder / "started.json").write_text(json.dumps({"pid": 123}))
            write_window_metadata(folder, 123, sheet=True)
            (folder / "accessibility.json").write_text(json.dumps({"pid": 123, "tree": {"role": "AXWindow"},
                                                                    "actualWidth": 1440, "actualHeight": 900}))
            result = capture.validate_result(folder, scenario, reject_renderer_placeholders=False)
            self.assertIsNone(result["nonBackgroundShare"])
            self.assertIn("uniform", result["rendererDiagnostic"])
            self.assertIsNone(result["sheetNonBackgroundShare"])
            with self.assertRaisesRegex(ValueError, "uniform"):
                capture.validate_result(folder, scenario)

    def test_app_renderer_files_use_the_render_subfolder(self):
        source = (ROOT / "app/DemoCatalog.swift").read_text()
        self.assertIn('folder.appendingPathComponent("render", isDirectory: true)', source)
        self.assertIn('renderFolder.appendingPathComponent("window-render.png")', source)
        self.assertIn('renderFolder.appendingPathComponent("sheet-render.png")', source)
        self.assertNotIn('folder.appendingPathComponent("window.png")', source)
        self.assertNotIn('folder.appendingPathComponent("sheet.png")', source)

    def test_rejects_black_and_near_uniform_png(self):
        with self.assertRaisesRegex(ValueError, "uniform"):
            capture.png_nonbackground_share(sample_png(uniform=True))
        with self.assertRaisesRegex(ValueError, "uniform"):
            capture.png_nonbackground_share(sample_png(near_uniform=True))
        self.assertGreater(capture.png_nonbackground_share(sample_png()), 0.01)

    def test_sparse_sheet_text_is_partial_not_uniform(self):
        png = sparse_sheet_png()
        with self.assertRaisesRegex(ValueError, "uniform"):
            capture.png_nonbackground_share(png)
        share = capture.png_nonbackground_share(png, allow_sparse_sheet=True)
        self.assertGreater(share, 0)
        self.assertLessEqual(share, 0.01)

    def test_sparse_sheet_does_not_accept_scattered_noise(self):
        with self.assertRaisesRegex(ValueError, "uniform"):
            capture.png_nonbackground_share(sparse_sheet_png(noise=True), allow_sparse_sheet=True)
        with self.assertRaisesRegex(ValueError, "uniform"):
            capture.png_nonbackground_share(sample_png(uniform=True), allow_sparse_sheet=True)

    def test_sparse_sheet_result_validates_with_required_metadata(self):
        scenario = next(s for s in capture.scenarios() if s["view"] == "freeze-result" and s["size"] == "1440x900")
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            write_renderer_png(folder, "sheet", sparse_sheet_png())
            (folder / "started.json").write_text(json.dumps({"pid": 123}))
            write_window_metadata(folder, 123, sheet=True)
            (folder / "accessibility.json").write_text(json.dumps({"pid": 123, "tree": {"role": "AXSheet"},
                                                                    "actualWidth": 1440, "actualHeight": 900}))
            result = capture.validate_sheet_result(folder, scenario)
            self.assertGreater(result["sheetNonBackgroundShare"], 0)
            self.assertLessEqual(result["sheetNonBackgroundShare"], 0.01)

    def test_rejects_appkit_placeholder_colors(self):
        with self.assertRaisesRegex(ValueError, "AppKit-backed views need a window-server capture"):
            capture.png_nonbackground_share(sample_png(placeholder=True))

    def test_rejects_dark_and_pure_yellow_appkit_placeholders(self):
        neutral = (240, 240, 240)
        for yellow, red in (((255, 214, 10), neutral), ((255, 255, 0), neutral), (neutral, (255, 69, 58))):
            with self.subTest(yellow=yellow, red=red):
                with self.assertRaisesRegex(ValueError, "AppKit-backed views need a window-server capture"):
                    capture.png_nonbackground_share(sample_png(placeholder=True, yellow=yellow, red=red))

    def test_rejects_insecure_app_created_scenario_directory(self):
        scenario = capture.scenarios()[0]
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp) / "scenario"
            folder.mkdir(mode=0o755)
            folder.chmod(0o755)
            with self.assertRaisesRegex(ValueError, "permissions"):
                capture.validate_result(folder, scenario)

    def test_capture_rejects_wrong_content_size(self):
        scenario = next(s for s in capture.scenarios() if s["size"] == "850x700" and not capture.has_sheet_image(s["view"]))
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            write_renderer_png(folder, "window", sample_png())
            (folder / "started.json").write_text(json.dumps({"pid": 123}))
            write_window_metadata(folder, 123)
            (folder / "accessibility.json").write_text(json.dumps({"pid": 123, "tree": {"role": "AXWindow"},
                                                                      "actualWidth": 1100, "actualHeight": 793}))
            with self.assertRaisesRegex(ValueError, "size"):
                capture.validate_result(folder, scenario)

    def test_capture_accepts_the_reported_clamped_size(self):
        scenario = next(s for s in capture.scenarios() if s["size"] == "850x700" and not capture.has_sheet_image(s["view"]))
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            write_renderer_png(folder, "window", sample_png())
            (folder / "started.json").write_text(json.dumps({"pid": 123}))
            write_window_metadata(folder, 123)
            (folder / "accessibility.json").write_text(json.dumps({"pid": 123, "tree": {"role": "AXWindow", "children": [
                                                                          {"role": "AXRadioButton", "label": "Preservation"},
                                                                          {"role": "AXButton", "title": "Connect remote…"},
                                                                          {"role": "AXButton", "title": "Freeze…"},
                                                                          {"role": "AXButton", "title": "Hold"}]},
                                                                      "actualWidth": 850, "actualHeight": 793}))
            self.assertEqual(capture.validate_result(folder, scenario)["actualWidth"], 850)

    def test_capture_marks_sheet_tree_missing_required_controls_as_partial(self):
        scenario = next(s for s in capture.scenarios() if s["view"] == "freeze")
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            write_renderer_png(folder, "window", sample_png())
            write_renderer_png(folder, "sheet", sample_png())
            (folder / "started.json").write_text(json.dumps({"pid": 123}))
            write_window_metadata(folder, 123, sheet=True)
            (folder / "accessibility.json").write_text(json.dumps({"pid": 123, "tree": {"role": "AXSheet"},
                                                                      "actualWidth": 1440, "actualHeight": 900}))
            result = capture.validate_result(folder, scenario)
            self.assertEqual(result["accessibilityMissing"], ["Requirements", "Cancel", "Build review"])

    def test_launch_rejects_relative_capture_folder(self):
        with patch.object(capture.subprocess, "run") as run:
            with self.assertRaisesRegex(ValueError, "absolute"):
                capture.launch(Path("/example/Igloo.app"), Path("captures/relative"), capture.scenarios()[0])
            run.assert_not_called()

    def test_main_refuses_existing_output(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            app = root / "Igloo.app"
            executable = app / "Contents/MacOS/Igloo"
            executable.parent.mkdir(parents=True)
            executable.touch()
            output = root / "existing"
            output.mkdir()
            args = ["capture_states", "--app", str(app), "--output", str(output)]
            with patch.object(sys, "argv", args), patch.object(capture.sys, "platform", "darwin"), \
                 patch.object(capture, "launch") as launch:
                with self.assertRaises((SystemExit, FileExistsError)):
                    capture.main()
                launch.assert_not_called()

    def test_main_refuses_a_bundle_with_the_installed_local_identity(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            app = root / "DifferentName.app"
            contents = app / "Contents"
            executable = contents / "MacOS/Igloo"
            executable.parent.mkdir(parents=True)
            executable.touch()
            with (contents / "Info.plist").open("wb") as info:
                plistlib.dump({"CFBundleIdentifier": "local.igloo"}, info)
            output = root / "output"
            args = ["capture_states", "--app", str(app), "--output", str(output)]
            with patch.object(sys, "argv", args), patch.object(capture.sys, "platform", "darwin"), \
                 patch.object(capture, "launch") as launch:
                with self.assertRaises(SystemExit):
                    capture.main()
                launch.assert_not_called()
            self.assertFalse(output.exists())

    def test_installed_bundle_path_is_refused_even_without_reading_its_contents(self):
        self.assertTrue(capture.is_protected_app_bundle(Path("/Applications/Igloo.app")))

    def test_main_refuses_dangling_output_symlink(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            app = root / "Igloo.app"
            executable = app / "Contents/MacOS/Igloo"
            executable.parent.mkdir(parents=True)
            executable.touch()
            target = root / "must-not-be-created"
            output = root / "link"
            output.symlink_to(target)
            args = ["capture_states", "--app", str(app), "--output", str(output)]
            with patch.object(sys, "argv", args), patch.object(capture.sys, "platform", "darwin"), \
                 patch.object(capture, "launch") as launch:
                with self.assertRaises(SystemExit):
                    capture.main()
                launch.assert_not_called()
            self.assertTrue(output.is_symlink())
            self.assertFalse(target.exists())

    def test_main_creates_private_output_and_passes_new_path_to_app(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            app = root / "Igloo.app"
            executable = app / "Contents/MacOS/Igloo"
            executable.parent.mkdir(parents=True)
            executable.touch()
            output = root / "output"
            scenario = capture.scenarios()[0]
            args = ["capture_states", "--app", str(app), "--output", str(output), "--scenario", scenario["name"]]
            def fake_launch(app_path, folder, selected, backend=None):
                self.assertFalse(folder.exists(), "the app must create its own new scenario directory")
                folder.mkdir(mode=0o700)
            result = {"pid": 123, "actualWidth": 1440, "actualHeight": 900}
            text = io.StringIO()
            with patch.object(sys, "argv", args), patch.object(capture.sys, "platform", "darwin"), \
                 patch.object(capture, "launch", side_effect=fake_launch), \
                 patch.object(capture, "validate_result", return_value=result), contextlib.redirect_stdout(text):
                self.assertEqual(capture.main(), 0)
            self.assertIn("PARTIAL " + scenario["name"], text.getvalue())
            self.assertNotIn("PASS " + scenario["name"], text.getvalue())
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o700)

    def test_timeout_reports_started_pid_without_stopping_it(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            app = root / "Igloo.app"
            executable = app / "Contents/MacOS/Igloo"
            executable.parent.mkdir(parents=True)
            executable.touch()
            output = root / "output"
            scenario = next(s for s in capture.scenarios() if s["view"] == "freeze")
            args = ["capture_states", "--app", str(app), "--output", str(output), "--scenario", scenario["name"]]
            def timeout(app_path, folder, selected, backend=None):
                folder.mkdir(mode=0o700)
                (folder / "started.json").write_text(json.dumps({"pid": 4321}))
                raise subprocess.TimeoutExpired(["open"], 60)
            text = io.StringIO()
            with patch.object(sys, "argv", args), patch.object(capture.sys, "platform", "darwin"), \
                 patch.object(capture, "launch", side_effect=timeout), contextlib.redirect_stdout(text):
                self.assertEqual(capture.main(), 1)
            self.assertIn("OPEN " + scenario["name"], text.getvalue())
            self.assertIn("PID 4321", text.getvalue())

    def test_window_server_argument_is_forwarded_only_when_requested(self):
        scenario = capture.scenarios()[0]
        normal = capture.build_launch_command(Path("/example/Igloo.app"), Path("/tmp/capture"), scenario, False)
        server = capture.build_launch_command(Path("/example/Igloo.app"), Path("/tmp/capture"), scenario, True)
        self.assertNotIn("--demo-window-server", normal)
        self.assertIn("--demo-window-server", server)

    def test_window_metadata_requires_number_frame_and_screen_state(self):
        window = {"kind": "window", "windowNumber": 123, "frame": {"x": 1, "y": 2, "width": 900, "height": 700}, "isOnScreen": True}
        self.assertEqual(capture.validate_windows({"pid": 42, "windows": [window]}), [window])
        for missing in ("windowNumber", "frame", "isOnScreen"):
            broken = dict(window)
            broken.pop(missing)
            with self.assertRaises(ValueError):
                capture.validate_windows({"pid": 42, "windows": [broken]})

    def test_window_server_captures_only_onscreen_windows(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            frame = {"x": 1, "y": 2, "width": 900, "height": 700}
            metadata = {"pid": 42, "windows": [{"kind": "window", "windowNumber": 123, "frame": frame, "isOnScreen": True},
                                               {"kind": "sheet", "windowNumber": 124, "frame": frame, "isOnScreen": False}]}
            (folder / "windows.json").write_text(json.dumps(metadata))
            (folder / "started.json").write_text(json.dumps({"pid": 42}))
            def fake_run(args, **kwargs):
                Path(args[-1]).write_bytes(sample_png())
            with patch.object(capture.subprocess, "run", side_effect=fake_run) as run:
                result = capture.capture_window_server(folder)
            self.assertEqual(run.call_count, 1)
            self.assertEqual(run.call_args.args[0][:5], ["/usr/sbin/screencapture", "-o", "-x", "-l", "123"])
            self.assertEqual(result[0]["status"], "PARTIAL")
            self.assertEqual(result[1]["status"], "OPEN")
            self.assertIn("display asleep or locked", result[1]["reason"])

    def test_launch_passes_demo_only_capture_arguments(self):
        scenario = capture.scenarios()[0]
        args = capture.build_launch_command(Path("/example/Igloo.app"), Path("/tmp/capture"), scenario, backend="orca")
        self.assertEqual(args[:4], ["/usr/bin/open", "-n", "-W", "/example/Igloo.app"])
        for argument in ("--demo", "--demo-size", "--demo-view", "--demo-capture", "--demo-scope", "--demo-rail", "--demo-data", "--demo-window-server", "--demo-orca"):
            self.assertIn(argument, args)

    def test_orca_demo_flag_selects_its_longer_app_deadline(self):
        source = (ROOT / "app" / "DemoCatalog.swift").read_text()
        self.assertIn('let orcaCapture = args.contains("--demo-orca")', source)
        self.assertIn("if orcaCapture && (!windowServer || folder == nil)", source)
        self.assertIn("options.orcaCapture ? 900 : (options.windowServer ? 60 : 15)", source)
