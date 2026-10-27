import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
import tempfile
import unittest
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

    def test_invalid_json_is_not_silently_overwritten(self):
        self.path.parent.mkdir(parents=True);self.path.write_text("{broken")
        with self.assertRaises(ValueError): preferences.load(self.path,self.home)
        self.assertEqual(self.path.read_text(),"{broken")

if __name__=="__main__": unittest.main()
