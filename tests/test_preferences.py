import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
import tempfile
import unittest
import unittest.mock
import preferences

class PreferencesTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.home=Path(self.temp.name)
        self.path=self.home/".config/igloo/config.json"
        self.value=preferences.defaults(self.home)
    def tearDown(self): self.temp.cleanup()
    def test_defaults_use_home_configuration_and_independent_workspace(self):
        self.assertEqual(preferences.config_path(self.home),self.path)
        self.assertEqual(self.value["projectsRoot"],str(self.home/"Projects"))
        self.assertEqual(self.value["cloneRoot"],self.value["projectsRoot"])
        self.assertEqual(self.value["githubOwner"],"")
        self.assertEqual(self.value["stateRoot"], str(self.home/".igloo"))
        self.assertEqual(self.value["archiveRoot"], str(self.home/".igloo/vault"))
        self.assertFalse(self.value["quietReminderEnabled"])
        self.assertEqual(self.value["quietReminderWeekday"], 1)
        self.assertEqual(self.value["quietReminderTime"], "09:00")
    def test_partial_workspace_configuration_derives_clone_default(self):
        self.path.parent.mkdir(parents=True)
        workspace=self.home/"Work"
        self.path.write_text(json.dumps({"version":1,"projectsRoot":str(workspace)}))
        result=preferences.load(self.path,self.home)
        self.assertEqual(result["cloneRoot"],str(workspace))
        self.assertEqual(result["archiveRoot"],str(self.home/".igloo/vault"))
    def test_roundtrip_private_permissions(self):
        preferences.write(self.value,self.path)
        self.assertEqual(preferences.load(self.path,self.home),self.value)
        self.assertEqual(self.path.stat().st_mode&0o777,0o600)
        self.assertEqual(self.path.parent.stat().st_mode&0o777,0o700)
    def test_backup_defaults_and_existing_schema_remain_compatible(self):
        self.assertEqual(self.value["freezerBackupRoot"], "")
        self.assertEqual(self.value["freezerBackupCloudAcceptedRoot"], "")
        legacy = dict(self.value)
        legacy.pop("freezerBackupRoot")
        legacy.pop("freezerBackupCloudAcceptedRoot")
        self.path.parent.mkdir(parents=True)
        self.path.write_text(json.dumps(legacy))
        self.assertEqual(preferences.load(self.path, self.home), self.value)

    def test_backup_configuration_roundtrip_and_validation(self):
        value = dict(self.value, freezerBackupRoot=str(self.home / "Backup"),
                     freezerBackupCloudAcceptedRoot=str((self.home / "Backup").resolve()).casefold())
        preferences.write(value, self.path)
        self.assertEqual(preferences.load(self.path, self.home), value)
        for root in (None, False, "relative", " ", "bad\x00path"):
            with self.subTest(root=root), self.assertRaises(ValueError):
                preferences.validate(dict(self.value, freezerBackupRoot=root))
        for accepted in (None, True, False, "true", 1, " ", "bad\x00path", "/backup/../other"):
            with self.subTest(accepted=accepted), self.assertRaises(ValueError):
                preferences.validate(dict(self.value, freezerBackupCloudAcceptedRoot=accepted))

    def test_legacy_cloud_acceptance_does_not_authorize_any_folder(self):
        legacy = dict(self.value, freezerBackupCloudAccepted=True)
        legacy.pop("freezerBackupCloudAcceptedRoot")
        self.path.parent.mkdir(parents=True)
        self.path.write_text(json.dumps(legacy))
        result = preferences.load(self.path, self.home)
        self.assertEqual(result["freezerBackupCloudAcceptedRoot"], "")
        self.assertNotIn("freezerBackupCloudAccepted", result)

    def test_cloud_acceptance_root_resolves_alias_and_casefolds(self):
        destination = self.home / "MixedCaseBackup"
        destination.mkdir()
        alias = self.home / "alias"
        alias.symlink_to(destination, target_is_directory=True)
        value = dict(self.value, freezerBackupCloudAcceptedRoot=str(alias))
        self.assertEqual(preferences.validate(value)["freezerBackupCloudAcceptedRoot"],
                         str(destination.resolve()).casefold())
    def test_whole_filesystem_and_home_are_not_workspaces(self):
        for root in ("/",str(Path.home())):
            with self.subTest(root=root),self.assertRaises(ValueError):
                preferences.validate(dict(self.value,projectsRoot=root))
    def test_relative_executable_is_refused(self):
        with self.assertRaises(ValueError):
            preferences.validate(dict(self.value,pythonExecutable="python3"))
    def test_storage_cannot_replace_workspace_or_its_parent(self):
        for key in ("archiveRoot","stateRoot"):
            for value in (self.value["projectsRoot"],str(self.home)):
                with self.subTest(key=key,value=value),self.assertRaises(ValueError):
                    preferences.validate(dict(self.value,**{key:value}))
    def test_quiet_threshold_rejects_boolean_and_out_of_range_values(self):
        for days in (True,0,4000):
            with self.subTest(days=days),self.assertRaises(ValueError):
                preferences.validate(dict(self.value,quietDays=days))

    def test_quiet_reminder_settings_validate_weekday_and_local_time(self):
        value = dict(self.value, quietReminderEnabled=True, quietReminderWeekday=5,
                     quietReminderTime="17:45")
        self.assertEqual(preferences.validate(value), value)
        invalid = (
            {"quietReminderEnabled": 1},
            {"quietReminderWeekday": True},
            {"quietReminderWeekday": 7},
            {"quietReminderTime": "24:00"},
            {"quietReminderTime": "9:00"},
        )
        for changes in invalid:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                preferences.validate(dict(self.value, **changes))
    def test_bundled_backend_works_without_source_checkout(self):
        bundle=self.home/"Installed.app/Contents/Resources/backend"
        bundle.mkdir(parents=True)
        for name in ("organizer.py","lifecycle.py","preferences.py","planning.py","classification.py","automation.py","task_cli.py"):
            shutil.copyfile(Path(__file__).resolve().parents[1]/"backend"/name,bundle/name)
        workspace=self.home/"Projects";workspace.mkdir()
        preferences.write(self.value,self.path)
        result=subprocess.run([sys.executable,"-B",str(bundle/"organizer.py"),"snapshot"],
                              env=dict(os.environ,HOME=str(self.home)),capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stdout)
        self.assertEqual(json.loads(result.stdout)["root"],str(workspace.resolve()))
        wizard=subprocess.run([sys.executable,"-B",str(bundle/"organizer.py"),"wizard-state"],
                              env=dict(os.environ,HOME=str(self.home)),capture_output=True,text=True)
        self.assertEqual(wizard.returncode,0,wizard.stdout)
        self.assertEqual(json.loads(wizard.stdout)["rules"]["ageBasis"],"git")
        self.assertFalse((bundle/"__pycache__").exists())
        self.assertEqual((self.home/".igloo").stat().st_mode&0o777,0o700)

    def test_source_layout_keeps_self_protection_and_cli_installation(self):
        root = Path(__file__).resolve().parents[1]
        project = self.home / "Projects/Igloo"
        source = project / "backend"
        source.mkdir(parents=True)
        for path in (root / "backend").glob("*.py"):
            shutil.copyfile(path, source / path.name)
        shutil.copyfile(root / "LICENSE", project / "LICENSE")
        (project / "build.command").touch()
        preferences.write(self.value, self.path)
        script = """
import organizer as o
from pathlib import Path
assert o.APP == Path.cwd()
for target in [o.APP, o.APP / "node_modules"]:
    try:
        o.checked_path(str(target))
    except o.Refusal:
        pass
    else:
        raise AssertionError("Igloo source must stay protected")
"""
        result = subprocess.run([sys.executable, "-B", "-c", script], cwd=project,
                                env=dict(os.environ, HOME=str(self.home), PYTHONPATH=str(source)),
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        installed = subprocess.run([sys.executable, "-B", str(source / "task_cli.py"), "install-cli", "--json"],
                                   env=dict(os.environ, HOME=str(self.home)), capture_output=True, text=True)
        self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
        launcher = json.loads(installed.stdout)["path"]
        tasks = subprocess.run([launcher, "tasks", "--json"], env=dict(os.environ, HOME=str(self.home)),
                               capture_output=True, text=True)
        self.assertEqual(tasks.returncode, 0, tasks.stdout + tasks.stderr)
        self.assertEqual(json.loads(tasks.stdout)["tasks"], [])
        notices = list((self.home / ".igloo/task-runtime").glob("*/LICENSE"))
        self.assertEqual(len(notices), 1)
        self.assertEqual(notices[0].read_bytes(), (root / "LICENSE").read_bytes())

    def test_dev_identity_uses_its_own_root_and_leaves_the_release_unchanged(self):
        preferences.write(self.value, self.path)
        before = self.path.read_bytes()
        with unittest.mock.patch.dict(os.environ, {"IGLOO_APP_ID": "local.igloo.dev"}):
            dev = preferences.config_path(self.home)
            self.assertEqual(dev, self.home / ".config/igloo.dev/config.json")
            preferences.write(dict(self.value, quietDays=12), dev)
            self.assertEqual(preferences.load(dev, self.home)["quietDays"], 12)
        self.assertEqual(self.path.read_bytes(), before)
        with unittest.mock.patch.dict(os.environ, {"IGLOO_APP_ID": "local.other"}), self.assertRaises(ValueError):
            preferences.config_path(self.home)

    def test_release_ignores_a_foreign_bundle_python_and_keeps_the_file(self):
        bundle = self.home / "Installed.app/Contents/Resources/backend"
        bundle.mkdir(parents=True)
        shutil.copyfile(Path(__file__).resolve().parents[1] / "backend/preferences.py", bundle / "preferences.py")
        script = "import json, preferences; print(json.dumps(preferences.load()['pythonExecutable']))"
        env = {k: v for k, v in os.environ.items() if k != "IGLOO_APP_ID"}
        env.update(HOME=str(self.home), PYTHONPATH=str(bundle))
        for python, kept in (("/Volumes/Work/Igloo Dev.app/Contents/Resources/python/arm64/bin/python3", False),
                             (str(self.home / "Installed.app/Contents/Resources/python/arm64/bin/python3"), True),
                             ("/opt/custom/bin/python3", True)):
            with self.subTest(python=python):
                preferences.write(dict(self.value, pythonExecutable=python), self.path)
                before = self.path.read_bytes()
                result = subprocess.run([sys.executable, "-B", "-c", script], env=env, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                loaded = json.loads(result.stdout)
                self.assertEqual(loaded == python, kept, loaded)
                if not kept:
                    self.assertNotIn(".app/", loaded)
                self.assertEqual(self.path.read_bytes(), before)

    def test_invalid_json_is_not_silently_overwritten(self):
        self.path.parent.mkdir(parents=True);self.path.write_text("{broken")
        with self.assertRaises(ValueError): preferences.load(self.path,self.home)
        self.assertEqual(self.path.read_text(),"{broken")

NATIVE_CHECKS = r"""
let scenario = CommandLine.arguments[1]
let home = FileManager.default.homeDirectoryForCurrentUser
switch scenario {
case "identity":
    precondition(ConfigRoot.appIdentifier("local.igloo") == "local.igloo")
    precondition(ConfigRoot.appIdentifier("local.igloo.dev") == "local.igloo.dev")
    precondition(ConfigRoot.appIdentifier(nil) == "local.igloo.dev")
    precondition(ConfigRoot.folder(for: "local.igloo", home: home).path == home.path + "/.config/igloo")
    precondition(ConfigRoot.folder(for: "local.igloo.dev", home: home).path == home.path + "/.config/igloo.dev")
case "save":
    precondition(UserPreferences.bundledPython != nil, "the fixture runtime must be found")
    precondition(UserPreferences.file == ConfigRoot.folder(for: "local.igloo.dev", home: home).appendingPathComponent("config.json"))
    let valid = try! UserPreferences.read().validated()
    precondition(valid.pythonExecutable != UserPreferences.bundledPython)
    try! valid.write()
case "foreign":
    let own = home.path + "/Own.app"
    let read = UserPreferences.read(ownBundle: own)
    precondition(!read.pythonExecutable.contains(".app/"), read.pythonExecutable)
    precondition(!ConfigRoot.isForeignBundlePath(own + "/Contents/Resources/python/arm64/bin/python3", ownBundle: own))
    precondition(!ConfigRoot.isForeignBundlePath("/opt/custom/bin/python3", ownBundle: own))
default:
    fatalError("unknown scenario")
}
print("PASS " + scenario)
"""


class NativePreferencesTests(unittest.TestCase):
    """Compiles the production settings model with a check program. No app is built or launched."""
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix="igloo-preferences-tests-")
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.root = Path(cls.temporary.name)
        source = (Path(__file__).resolve().parents[1] / "app/PreferencesViews.swift").read_text()
        model = source[source.index("struct UserPreferences: Codable"):source.index("struct BackupLocation")]
        (cls.root / "main.swift").write_text("import Foundation\n" + model + NATIVE_CHECKS)
        cls.executable = cls.root / "check"
        build = subprocess.run(["/usr/bin/swiftc", cls.root / "main.swift", "-o", cls.executable],
                               capture_output=True, text=True, timeout=300)
        if build.returncode:
            raise AssertionError(build.stderr)
        # An unbundled executable finds resources next to itself, like python/<arch> in the app.
        runtime = cls.root / "python" / os.uname().machine / "bin/python3"
        runtime.parent.mkdir(parents=True)
        runtime.write_text("#!/bin/sh\n")
        runtime.chmod(0o700)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=self.root)
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        (self.home / "Projects").mkdir()
        self.release = self.home / ".config/igloo"
        self.dev = self.home / ".config/igloo.dev"

    def run_check(self, scenario):
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        env.update(HOME=str(self.home), CFFIXED_USER_HOME=str(self.home))
        result = subprocess.run([self.executable, scenario], env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "PASS " + scenario)

    def write_release(self, **changes):
        value = dict(preferences.defaults(self.home), pythonExecutable="/opt/custom/bin/python3", quietDays=21)
        value.update(changes)
        preferences.write(value, self.release / "config.json")
        (self.release / "wizard.json").write_text('{"rules": {"cacheDays": 9}}\n')
        return {name: (self.release / name).read_bytes() for name in ("config.json", "wizard.json")}

    def test_identity_maps_each_bundle_to_its_folder(self):
        self.run_check("identity")

    def test_first_dev_save_copies_release_once_and_never_writes_the_release(self):
        before = self.write_release()
        self.run_check("save")
        self.assertEqual({name: (self.release / name).read_bytes() for name in before}, before)
        saved = json.loads((self.dev / "config.json").read_text())
        self.assertEqual(saved["quietDays"], 21)
        self.assertNotEqual(saved["pythonExecutable"], "/opt/custom/bin/python3")
        self.assertNotIn(str(self.root), saved["pythonExecutable"])
        self.assertEqual((self.dev / "wizard.json").read_bytes(), before["wizard.json"])
        self.assertEqual((self.dev / "config.json").stat().st_mode & 0o777, 0o600)
        # A later save keeps a user-entered Python and does not copy the release again.
        saved.update(pythonExecutable="/opt/other/bin/python3", quietDays=33)
        (self.dev / "config.json").write_text(json.dumps(saved))
        self.write_release(quietDays=44)
        self.run_check("save")
        saved = json.loads((self.dev / "config.json").read_text())
        self.assertEqual((saved["pythonExecutable"], saved["quietDays"]), ("/opt/other/bin/python3", 33))

    def test_read_ignores_a_foreign_bundle_python_without_writing(self):
        foreign = str(self.home / "Igloo Dev.app/Contents/Resources/python/arm64/bin/python3")
        self.dev.mkdir(parents=True)
        preferences.write(dict(preferences.defaults(self.home), pythonExecutable=foreign), self.dev / "config.json")
        before = (self.dev / "config.json").read_bytes()
        self.run_check("foreign")
        self.assertEqual((self.dev / "config.json").read_bytes(), before)
        self.assertFalse(self.release.exists())

if __name__=="__main__": unittest.main()
