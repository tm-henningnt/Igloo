"""Install transaction tests use only fictional app bundles in temporary folders."""

import builtins
import hashlib
import importlib.util
import io
import os
from pathlib import Path
import plistlib
import signal
import stat
import sys
import tempfile
import unittest
import zipfile
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("igloo_update_installer", ROOT / "scripts/update_installer.py")
installer = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = installer
SPEC.loader.exec_module(installer)


def write_bundle(path: Path, version: str, build: int, bundle_id: str = "local.igloo",
                 runtime: str = "bundled") -> None:
    contents = path / "Contents"
    executable = contents / "MacOS" / "Igloo"
    executable.parent.mkdir(parents=True)
    (contents / "Info.plist").write_bytes(plistlib.dumps({
        "CFBundleIdentifier": bundle_id,
        "CFBundlePackageType": "APPL",
        "CFBundleExecutable": "Igloo",
        "CFBundleShortVersionString": version,
        "CFBundleVersion": str(build),
        "IglooRuntime": runtime,
    }))
    executable.write_bytes((version + " fixture executable").encode())
    executable.chmod(0o755)


def zip_entry(archive: zipfile.ZipFile, name: str, payload: bytes, mode: int) -> None:
    entry = zipfile.ZipInfo(name)
    entry.create_system = 3
    entry.external_attr = mode << 16
    archive.writestr(entry, payload)


def write_archive(path: Path, version: str, build: int,
                  bundle_id: str = "local.igloo", runtime: str = "bundled") -> str:
    info = plistlib.dumps({
        "CFBundleIdentifier": bundle_id,
        "CFBundlePackageType": "APPL",
        "CFBundleExecutable": "Igloo",
        "CFBundleShortVersionString": version,
        "CFBundleVersion": str(build),
        "IglooRuntime": runtime,
    })
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        zip_entry(archive, "Igloo.app/Contents/Info.plist", info, stat.S_IFREG | 0o644)
        zip_entry(archive, "Igloo.app/Contents/MacOS/Igloo", b"new fixture executable",
                  stat.S_IFREG | 0o755)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_entries(path: Path, entries: list[tuple[str, bytes, int]]) -> None:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, payload, mode in entries:
            zip_entry(archive, name, payload, mode)


@unittest.skipUnless(sys.platform == "darwin", "the app bundle installer uses macOS code signing")
class UpdateInstallerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="igloo-update-install-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.install_folder = self.root / "install"
        self.install_folder.mkdir()
        self.target = self.install_folder / "Igloo.app"
        write_bundle(self.target, "0.2.1", 15)
        self.archive = self.root / "download.zip"
        self.digest = write_archive(self.archive, "0.2.2", 16)

    def install(self, *, archive=None, digest=None, version="0.2.2", build=16,
                target=None, launcher=None, liveness=None, signature_verifier=None,
                process_exit=None):
        return installer.install_update(
            archive or self.archive,
            digest or self.digest,
            version,
            build,
            "bundled",
            target or self.target,
            launcher=launcher or (lambda _path: None),
            liveness=liveness or (lambda _path: True),
            signature_verifier=signature_verifier or (lambda _path: None),
            process_exit=process_exit or installer._wait_for_app_exit,
        )

    def installed_version(self, target=None):
        info_path = (target or self.target) / "Contents" / "Info.plist"
        return plistlib.loads(info_path.read_bytes())["CFBundleShortVersionString"]

    def test_verified_newer_bundle_replaces_and_relaunches(self):
        launched = []
        self.install(launcher=launched.append)
        self.assertEqual(self.installed_version(), "0.2.2")
        self.assertEqual([p.resolve() for p in launched], [self.target.resolve()])
        self.assertEqual(list(self.install_folder.iterdir()), [self.target])
        self.assertTrue(self.target.is_relative_to(self.root))

    def test_signature_is_checked_before_the_swap(self):
        checked = []
        self.install(signature_verifier=lambda candidate: checked.append(candidate))
        self.assertEqual(len(checked), 1)
        self.assertEqual(checked[0].name, "Igloo.app")

    def test_bad_code_signature_keeps_old_bundle(self):
        def reject(_candidate):
            raise installer.InstallError("The downloaded Igloo app failed its code signature check.")
        with self.assertRaisesRegex(installer.InstallError, "code signature"):
            self.install(signature_verifier=reject)
        self.assertEqual(self.installed_version(), "0.2.1")
        self.assertEqual(list(self.install_folder.iterdir()), [self.target])

    def test_code_signature_uses_deep_strict_verification(self):
        with patch.object(installer.subprocess, "run") as run:
            installer._verify_code_signature(self.target)
        run.assert_called_once_with(["/usr/bin/codesign", "--verify", "--deep", "--strict", str(self.target)],
                                    check=True, stdout=installer.subprocess.DEVNULL,
                                    stderr=installer.subprocess.PIPE, timeout=60)

    def test_bad_checksum_keeps_old_bundle(self):
        with self.assertRaisesRegex(installer.InstallError, "SHA-256"):
            self.install(digest="0" * 64)
        self.assertEqual(self.installed_version(), "0.2.1")
        self.assertEqual(list(self.install_folder.iterdir()), [self.target])

    def test_lower_version_is_refused(self):
        digest = write_archive(self.root / "lower.zip", "0.2.0", 30)
        with self.assertRaisesRegex(installer.InstallError, "not newer"):
            self.install(archive=self.root / "lower.zip", digest=digest,
                         version="0.2.0", build=30)
        self.assertEqual(self.installed_version(), "0.2.1")

    def test_wrong_bundle_identifier_is_refused(self):
        archive = self.root / "wrong-bundle.zip"
        digest = write_archive(archive, "0.2.2", 16, "local.igloo.dev")
        with self.assertRaisesRegex(installer.InstallError, "wrong bundle identifier"):
            self.install(archive=archive, digest=digest)
        self.assertEqual(self.installed_version(), "0.2.1")

    def test_failed_move_restores_and_reopens_old_bundle(self):
        real_rename = os.rename
        failed = False

        def rename_once(source, destination):
            nonlocal failed
            if Path(destination).resolve() == self.target.resolve() and Path(source).resolve() != self.target.resolve() and not failed:
                failed = True
                raise OSError("fixture move failure")
            return real_rename(source, destination)

        launched = []
        with patch.object(installer.os, "rename", side_effect=rename_once):
            with self.assertRaisesRegex(installer.InstallError, "previous Igloo app was restored"):
                self.install(launcher=launched.append)
        self.assertTrue(failed)
        self.assertEqual(self.installed_version(), "0.2.1")
        self.assertEqual([p.resolve() for p in launched], [self.target.resolve()])
        self.assertEqual(list(self.install_folder.iterdir()), [self.target])

    def test_first_rename_failure_relaunches_untouched_old_bundle(self):
        real_rename = os.rename
        failed = False

        def fail_target_to_backup(source, destination):
            nonlocal failed
            if Path(source).resolve() == self.target.resolve() and ".backup-" in Path(destination).name and not failed:
                failed = True
                raise OSError("first rename failed")
            return real_rename(source, destination)

        launches = []
        def launch(path, *, update_failed=False):
            launches.append((self.installed_version(path), update_failed))

        with patch.object(installer.os, "rename", side_effect=fail_target_to_backup):
            with self.assertRaisesRegex(installer.InstallError, "previous Igloo app was restored or left in place"):
                self.install(launcher=launch)
        self.assertTrue(failed)
        self.assertEqual(self.installed_version(), "0.2.1")
        self.assertEqual(launches, [("0.2.1", True)])
        self.assertEqual(list(self.install_folder.iterdir()), [self.target])

    def test_signal_after_first_rename_restores_backup(self):
        real_rename = os.rename

        def rename_then_interrupt(source, destination):
            if Path(source).resolve() == self.target.resolve() and ".backup-" in Path(destination).name:
                real_rename(source, destination)
                raise installer.InstallInterrupted("signal fixture")
            return real_rename(source, destination)

        launches = []
        with patch.object(installer.os, "rename", side_effect=rename_then_interrupt):
            with self.assertRaisesRegex(installer.InstallError, "signal fixture"):
                self.install(launcher=lambda path, **_kwargs: launches.append(path))
        self.assertEqual(self.installed_version(), "0.2.1")
        self.assertEqual([path.resolve() for path in launches], [self.target.resolve()])
        self.assertEqual(list(self.install_folder.iterdir()), [self.target])

    def test_failed_relaunch_restores_previous_bundle(self):
        launched_versions = []

        def launch(path):
            version = self.installed_version(path)
            launched_versions.append(version)
            if version == "0.2.2":
                raise OSError("fixture launch failure")

        with self.assertRaisesRegex(installer.InstallError, "previous Igloo app was restored"):
            self.install(launcher=launch, process_exit=lambda _path, **_kwargs: True)
        self.assertEqual(self.installed_version(), "0.2.1")
        self.assertEqual(launched_versions, ["0.2.2", "0.2.1"])
        self.assertEqual(list(self.install_folder.iterdir()), [self.target])

    def test_new_app_must_stay_alive_before_backup_is_removed(self):
        def live(path):
            self.assertTrue(any(".backup-" in item.name for item in self.install_folder.iterdir()))
            return True
        self.install(liveness=live)
        self.assertEqual(self.installed_version(), "0.2.2")
        self.assertEqual(list(self.install_folder.iterdir()), [self.target])

    def test_dead_new_app_rolls_back_and_marks_reopened_old_app(self):
        launches = []
        def launch(path, *, update_failed=False):
            launches.append((self.installed_version(path), update_failed))
        def live(path):
            return self.installed_version(path) == "0.2.1"
        with self.assertRaisesRegex(installer.InstallError, "did not stay open"):
            self.install(launcher=launch, liveness=live,
                         process_exit=lambda _path, **_kwargs: True)
        self.assertEqual(self.installed_version(), "0.2.1")
        self.assertEqual(launches, [("0.2.2", False), ("0.2.1", True)])
        self.assertEqual(list(self.install_folder.iterdir()), [self.target])

    def test_liveness_false_negative_keeps_a_possibly_running_bundle_and_backup(self):
        launches = []
        def launch(path, *, update_failed=False, failure_file=None):
            launches.append((self.installed_version(path), update_failed, failure_file))
        plan = installer.prepare_update(self.archive, self.digest, "0.2.2", 16, "bundled", self.target,
                                        signature_verifier=lambda _candidate: None)
        with self.assertRaisesRegex(installer.InstallError, "may still be running"):
            installer.complete_update(plan, launcher=launch, liveness=lambda _path: False,
                                       process_exit=lambda _path, **_kwargs: False)
        self.assertEqual(self.installed_version(), "0.2.2")
        self.assertTrue(any(".backup-" in item.name for item in self.install_folder.iterdir()))
        self.assertEqual([entry[:2] for entry in launches], [("0.2.2", False)])

    def test_second_signals_during_rollback_do_not_interrupt_backup_restore(self):
        real_rename = os.rename
        sent = set()

        def signal_during_rollback(source, destination):
            source_path, destination_path = Path(source), Path(destination)
            if destination_path.name == "failed-Igloo.app" and signal.SIGTERM not in sent:
                sent.add(signal.SIGTERM)
                os.kill(os.getpid(), signal.SIGTERM)
            if ".backup-" in source_path.name and destination_path.resolve() == self.target.resolve():
                if signal.SIGHUP not in sent:
                    sent.add(signal.SIGHUP)
                    os.kill(os.getpid(), signal.SIGHUP)
            return real_rename(source, destination)

        launches = []
        def launch(path, *, update_failed=False):
            launches.append((self.installed_version(path), update_failed))
            if not update_failed:
                raise OSError("fixture new app launch failure")

        with patch.object(installer.os, "rename", side_effect=signal_during_rollback):
            with self.assertRaisesRegex(installer.InstallError, "could not be installed"):
                self.install(launcher=launch, process_exit=lambda _path, **_kwargs: True)
        self.assertEqual(sent, {signal.SIGTERM, signal.SIGHUP})
        self.assertEqual(self.installed_version(), "0.2.1")
        self.assertEqual(launches, [("0.2.2", False), ("0.2.1", True)])
        self.assertEqual(list(self.install_folder.iterdir()), [self.target])

    def test_signal_is_deferred_before_rollback_removes_the_new_bundle(self):
        real_rename = os.rename
        real_rmtree = installer.shutil.rmtree

        def fail_quarantine(source, destination):
            if Path(destination).name == "failed-Igloo.app":
                raise OSError("fixture quarantine failure")
            return real_rename(source, destination)

        def signal_before_target_removal(path, *args, **kwargs):
            if Path(path).resolve() == self.target.resolve():
                os.kill(os.getpid(), signal.SIGTERM)
            return real_rmtree(path, *args, **kwargs)

        def launch(path, *, update_failed=False):
            if self.installed_version(path) == "0.2.2" and not update_failed:
                raise OSError("fixture new app launch failure")

        with patch.object(installer.os, "rename", side_effect=fail_quarantine), \
             patch.object(installer.shutil, "rmtree", side_effect=signal_before_target_removal):
            with self.assertRaises(installer.InstallError):
                self.install(launcher=launch, process_exit=lambda _path, **_kwargs: True)
        self.assertEqual(self.installed_version(), "0.2.1")
        self.assertEqual(list(self.install_folder.iterdir()), [self.target])

    def test_failed_old_app_relaunch_is_reported(self):
        def launch(path, *, update_failed=False):
            if self.installed_version(path) == "0.2.2":
                raise OSError("new launch failed")
            raise OSError("old launch failed")
        with self.assertRaisesRegex(installer.InstallError, "previous Igloo app was restored, but could not be reopened"):
            self.install(launcher=launch, process_exit=lambda _path, **_kwargs: True)
        self.assertEqual(self.installed_version(), "0.2.1")
        failure_files = list(self.install_folder.glob(".Igloo-update-failure-*.txt"))
        self.assertEqual(len(failure_files), 1)
        self.assertIn("previous Igloo app was restored", failure_files[0].read_text())

    def test_translocated_and_read_only_locations_are_refused(self):
        translocated = self.root / "AppTranslocation" / "d" / "Igloo.app"
        translocated.parent.mkdir(parents=True)
        write_bundle(translocated, "0.2.1", 15)
        with self.assertRaisesRegex(installer.InstallError, "translocated"):
            self.install(target=translocated)

        read_only_folder = self.root / "read-only"
        read_only_folder.mkdir()
        read_only = read_only_folder / "Igloo.app"
        write_bundle(read_only, "0.2.1", 15)
        read_only_folder.chmod(0o555)
        try:
            with self.assertRaisesRegex(installer.InstallError, "read-only"):
                self.install(target=read_only)
        finally:
            read_only_folder.chmod(0o755)
        self.assertEqual(self.installed_version(read_only), "0.2.1")

    def test_igloo_dev_cannot_install_release_update(self):
        dev = self.root / "Igloo Dev.app"
        write_bundle(dev, "0.2.1", 15, "local.igloo.dev")
        with self.assertRaisesRegex(installer.InstallError, "cannot install release updates"):
            self.install(target=dev)
        self.assertEqual(self.installed_version(dev), "0.2.1")

    def test_runtime_mismatch_is_refused(self):
        with self.assertRaisesRegex(installer.InstallError, "does not match this Igloo Python runtime"):
            installer.prepare_update(self.archive, self.digest, "0.2.2", 16, "system", self.target,
                                     signature_verifier=lambda _candidate: None)
        self.assertEqual(self.installed_version(), "0.2.1")

    def test_build_mismatch_is_refused(self):
        with self.assertRaisesRegex(installer.InstallError, "build does not match"):
            self.install(build=17)
        self.assertEqual(self.installed_version(), "0.2.1")

    def test_target_is_revalidated_immediately_before_the_swap(self):
        plan = installer.prepare_update(self.archive, self.digest, "0.2.2", 16, "bundled", self.target,
                                        signature_verifier=lambda _candidate: None)
        old_info = self.target / "Contents" / "Info.plist"
        info = plistlib.loads(old_info.read_bytes())
        info["CFBundleVersion"] = "18"
        old_info.write_bytes(plistlib.dumps(info))
        launches = []
        with self.assertRaisesRegex(installer.InstallError, "changed while the update was preparing"):
            installer.complete_update(plan, launcher=lambda path, **_kwargs: launches.append(path),
                                      liveness=lambda _path: True)
        self.assertEqual(self.installed_version(), "0.2.1")
        self.assertEqual([path.resolve() for path in launches], [self.target.resolve()])

    def test_plan_uses_resolved_target_and_staging_paths(self):
        plan = installer.prepare_update(self.archive, self.digest, "0.2.2", 16, "bundled", self.target,
                                        signature_verifier=lambda _candidate: None)
        self.addCleanup(installer.shutil.rmtree, plan.staging, ignore_errors=True)
        self.assertEqual(plan.target, plan.target.resolve(strict=True))
        self.assertEqual(plan.staging, plan.staging.resolve(strict=True))
        self.assertEqual(plan.staging.parent, plan.target.parent)

    def test_archive_rejects_zip_slip_absolute_path_symlink_and_extra_root(self):
        info = (self.target / "Contents" / "Info.plist").read_bytes()
        valid = [
            ("Igloo.app/Contents/Info.plist", info, stat.S_IFREG | 0o644),
            ("Igloo.app/Contents/MacOS/Igloo", b"app", stat.S_IFREG | 0o755),
        ]
        cases = [
            ("zip slip", valid + [("Igloo.app/../outside", b"x", stat.S_IFREG | 0o644)]),
            ("absolute", valid + [("/Igloo.app/outside", b"x", stat.S_IFREG | 0o644)]),
            ("symlink escape", valid + [("Igloo.app/Contents/outside", b"../../outside", stat.S_IFLNK | 0o777)]),
            ("symlink parent", valid + [("Igloo.app/Contents/link", b"../Resources", stat.S_IFLNK | 0o777)]),
            ("symlink normalized parent", valid + [("Igloo.app/Contents/link", b"a/../b", stat.S_IFLNK | 0o777)]),
            ("absolute symlink", valid + [("Igloo.app/Contents/link", b"/Igloo.app/Contents/Info.plist", stat.S_IFLNK | 0o777)]),
            ("extra root", valid + [("Other.app/Contents/x", b"x", stat.S_IFREG | 0o644)]),
        ]
        for name, entries in cases:
            with self.subTest(name=name):
                archive = self.root / (name.replace(" ", "-") + ".zip")
                write_entries(archive, entries)
                with self.assertRaises(installer.InstallError):
                    installer._validate_zip(archive)

    def test_archive_accepts_bare_macosx_directory_entry(self):
        info = (self.target / "Contents" / "Info.plist").read_bytes()
        archive = self.root / "sequestered.zip"
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
            zip_entry(bundle, "__MACOSX/", b"", stat.S_IFDIR | 0o755)
            zip_entry(bundle, "__MACOSX/Igloo.app/._Contents", b"x", stat.S_IFREG | 0o644)
            zip_entry(bundle, "Igloo.app/Contents/Info.plist", info, stat.S_IFREG | 0o644)
        installer._validate_zip(archive)
        staging = self.root / "sequestered-staging"
        staging.mkdir()
        installer._extract_archive(archive, staging)
        self.assertFalse((staging / "__MACOSX").exists())
        bare_file = self.root / "bare-file.zip"
        write_entries(bare_file, [("__MACOSX", b"x", stat.S_IFREG | 0o644)])
        with self.assertRaises(installer.InstallError):
            installer._validate_zip(bare_file)

    def test_archive_declared_and_streamed_size_caps(self):
        archive = self.root / "large.zip"
        write_entries(archive, [("Igloo.app/Contents/Info.plist", b"x" * 40, stat.S_IFREG | 0o644)])
        with patch.object(installer, "MAX_EXPANDED_BYTES", 32):
            with self.assertRaisesRegex(installer.InstallError, "too large"):
                installer._validate_zip(archive)
        staging = self.root / "staging"
        staging.mkdir()
        with patch.object(installer, "MAX_EXPANDED_BYTES", 16):
            with self.assertRaisesRegex(installer.InstallError, "too large"):
                installer._extract_archive(archive, staging)
        self.assertLessEqual((staging / "Igloo.app" / "Contents" / "Info.plist").stat().st_size, 0)


class InstallerProtocolTests(unittest.TestCase):
    def _main_plan(self, root: Path) -> installer.InstallPlan:
        target = root / "Igloo.app"
        write_bundle(target, "0.2.1", 15)
        staging = root / ".staging"
        staging.mkdir()
        return installer.InstallPlan(target.resolve(), staging.resolve(), staging / "Igloo.app",
                                     "bundled", "0.2.1", 15)

    @staticmethod
    def _main_args(target: Path, parent_pid: int) -> list[str]:
        return ["--archive", "a", "--sha256", "0" * 64, "--version", "1.0", "--build", "1",
                "--runtime", "bundled", "--target", str(target), "--parent-pid", str(parent_pid)]

    def test_processes_in_app_counts_only_the_main_executable(self):
        target = Path("/Applications/Igloo.app")
        executable = target / "Contents" / "MacOS" / "Igloo"
        backend = target / "Contents" / "Resources" / "python" / "bin" / "python3"
        result = type("Result", (), {"stdout": "411\n412\n"})()
        with patch.object(installer.subprocess, "run", return_value=result), \
             patch.object(installer, "_app_executable_path", side_effect=[executable, backend]):
            self.assertEqual(installer._processes_in_app(target), {411})

    def test_process_enumeration_failure_is_unknown_not_empty(self):
        with patch.object(installer.subprocess, "run", side_effect=OSError("fixture")):
            self.assertIsNone(installer._processes_in_app(Path("/Applications/Igloo.app")))

    def test_wait_for_app_alive_requires_a_stable_main_process(self):
        now = [0.0]

        def sleep(duration):
            now[0] += duration

        with patch.object(installer.time, "monotonic", side_effect=lambda: now[0]), \
             patch.object(installer.time, "sleep", side_effect=sleep), \
             patch.object(installer, "_processes_in_app", return_value={411}):
            self.assertTrue(installer._wait_for_app_alive(Path("/Applications/Igloo.app"),
                                                           timeout=1.0, stable_for=0.4))

    def test_wait_for_app_alive_does_not_treat_unknown_process_list_as_alive(self):
        now = [0.0]

        def sleep(duration):
            now[0] += duration

        with patch.object(installer.time, "monotonic", side_effect=lambda: now[0]), \
             patch.object(installer.time, "sleep", side_effect=sleep), \
             patch.object(installer, "_processes_in_app", return_value=None):
            self.assertFalse(installer._wait_for_app_alive(Path("/Applications/Igloo.app"),
                                                            timeout=0.5, stable_for=0.2))

    def test_wait_for_app_exit_requires_a_quiet_interval(self):
        now = [0.0]
        process_sets = iter([{411}, {411}, set(), set(), set()])

        def sleep(duration):
            now[0] += duration

        with patch.object(installer.time, "monotonic", side_effect=lambda: now[0]), \
             patch.object(installer.time, "sleep", side_effect=sleep), \
             patch.object(installer, "_processes_in_app", side_effect=lambda _target: next(process_sets, set())):
            self.assertTrue(installer._wait_for_app_exit(Path("/Applications/Igloo.app"),
                                                          timeout=1.0, stable_for=0.4))

    def test_wait_for_app_exit_is_false_while_the_main_process_remains(self):
        now = [0.0]

        def sleep(duration):
            now[0] += duration

        with patch.object(installer.time, "monotonic", side_effect=lambda: now[0]), \
             patch.object(installer.time, "sleep", side_effect=sleep), \
             patch.object(installer, "_processes_in_app", return_value={411}):
            self.assertFalse(installer._wait_for_app_exit(Path("/Applications/Igloo.app"),
                                                           timeout=0.5, stable_for=0.2))

    def test_real_termination_signal_raises_outside_a_transaction(self):
        state = installer._install_signal_handlers()
        try:
            with self.assertRaises(installer.InstallInterrupted):
                os.kill(os.getpid(), signal.SIGTERM)
        finally:
            installer._restore_signal_handlers(state)

    def test_wait_for_parent_exit_tracks_parent_relationship(self):
        with patch.object(installer.os, "getpid", return_value=400), \
             patch.object(installer.os, "getppid", side_effect=[512, 1]), \
             patch.object(installer, "_process_exists", return_value=True):
            installer.wait_for_parent_exit(512, timeout=1)

    def test_wait_for_parent_exit_treats_permission_error_as_existing_until_parent_changes(self):
        with patch.object(installer.os, "getpid", return_value=400), \
             patch.object(installer.os, "getppid", side_effect=[512, 512, 700]), \
             patch.object(installer.os, "kill", side_effect=PermissionError):
            installer.wait_for_parent_exit(512, timeout=1)

    def test_wait_for_parent_exit_rejects_invalid_pid(self):
        with patch.object(installer.os, "getpid", return_value=400):
            with self.assertRaisesRegex(installer.InstallError, "could not identify"):
                installer.wait_for_parent_exit(1)

    def test_main_refuses_root_without_preparing(self):
        output = io.StringIO()
        with patch.object(installer.os, "geteuid", return_value=0), \
             patch.object(installer.os, "getuid", return_value=0), \
             patch.object(installer, "prepare_update") as prepare, \
             redirect_stderr(output):
            result = installer.main(["--archive", "a", "--sha256", "0" * 64,
                "--version", "1.0", "--build", "1", "--runtime", "bundled",
                "--target", "/tmp/Igloo.app", "--parent-pid", "12"])
        self.assertEqual(result, 1)
        self.assertIn("never with administrator rights", output.getvalue())
        prepare.assert_not_called()

    def test_main_emits_ready_only_after_prepare_then_waits_and_completes(self):
        plan = object()
        stdout = io.StringIO()
        order = []
        with patch.object(installer.os, "geteuid", return_value=501), \
             patch.object(installer.os, "getuid", return_value=501), \
             patch.object(installer, "prepare_update", side_effect=lambda *args: order.append("prepare") or plan), \
             patch.object(installer, "wait_for_parent_exit", side_effect=lambda _pid: order.append("wait")), \
             patch.object(installer, "complete_update", side_effect=lambda value, **_kwargs: order.append(("complete", value))), \
             redirect_stdout(stdout):
            result = installer.main(["--archive", "a", "--sha256", "0" * 64,
                "--version", "1.0", "--build", "1", "--runtime", "bundled",
                "--target", "/tmp/Igloo.app", "--parent-pid", "12"])
        self.assertEqual(result, 0)
        self.assertEqual(stdout.getvalue(), "READY\n")
        self.assertEqual(order, ["prepare", "wait", ("complete", plan)])

    def test_main_wait_failure_relaunches_target_after_parent_exit(self):
        with tempfile.TemporaryDirectory(prefix="igloo-ready-failure-") as temporary:
            root = Path(temporary)
            target = root / "Igloo.app"
            write_bundle(target, "0.2.1", 15)
            staging = root / ".staging"
            staging.mkdir()
            plan = installer.InstallPlan(target.resolve(), staging.resolve(), staging / "Igloo.app",
                                         "bundled", "0.2.1", 15)
            launches = []

            def launch(path, *, update_failed=False, failure_file=None):
                reason = Path(failure_file).read_text() if failure_file is not None else None
                launches.append((Path(path).resolve(), update_failed, reason))

            stderr = io.StringIO()
            stdout = io.StringIO()
            with patch.object(installer.os, "geteuid", return_value=501), \
                 patch.object(installer.os, "getuid", return_value=501), \
                 patch.object(installer, "prepare_update", return_value=plan), \
                 patch.object(installer, "wait_for_parent_exit", side_effect=installer.InstallError("wait failed")), \
                 patch.object(installer, "_parent_is_alive", return_value=False), \
                 patch.object(installer, "_launch_app", side_effect=launch), \
                 patch.object(installer, "_wait_for_app_alive", return_value=True), \
                 redirect_stdout(stdout), redirect_stderr(stderr):
                result = installer.main(["--archive", "a", "--sha256", "0" * 64,
                    "--version", "1.0", "--build", "1", "--runtime", "bundled",
                    "--target", str(target), "--parent-pid", "12"])
            self.assertEqual(result, 1)
            self.assertEqual(stdout.getvalue(), "READY\n")
            self.assertEqual(len(launches), 1)
            self.assertEqual(launches[0][0], target.resolve())
            self.assertTrue(launches[0][1])
            self.assertIn("wait failed", launches[0][2])
            self.assertIn("reopened with the failure details", stderr.getvalue())

    def test_main_wait_failure_with_live_parent_does_not_relaunch(self):
        parent_pid = os.getppid()
        if parent_pid <= 1:
            self.skipTest("the test process has no live parent PID")
        with tempfile.TemporaryDirectory(prefix="igloo-live-parent-") as temporary:
            plan = self._main_plan(Path(temporary))
            launch = Mock()
            with patch.object(installer.os, "geteuid", return_value=501), \
                 patch.object(installer.os, "getuid", return_value=501), \
                 patch.object(installer, "prepare_update", return_value=plan), \
                 patch.object(installer, "wait_for_parent_exit", side_effect=installer.InstallError("wait failed")), \
                 patch.object(installer, "_launch_app", launch), \
                 redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                result = installer.main(self._main_args(plan.target, parent_pid))
        self.assertEqual(result, 1)
        launch.assert_not_called()

    def test_main_handles_real_sigterm_during_wait_and_cleanup(self):
        parent_pid = os.getppid()
        if parent_pid <= 1:
            self.skipTest("the test process has no live parent PID")
        with tempfile.TemporaryDirectory(prefix="igloo-signal-wait-") as temporary:
            plan = self._main_plan(Path(temporary))
            launch = Mock()
            stderr = io.StringIO()
            real_rmtree = installer.shutil.rmtree

            def interrupt_wait(_pid):
                os.kill(os.getpid(), signal.SIGTERM)

            def signal_during_cleanup(path, *args, **kwargs):
                os.kill(os.getpid(), signal.SIGTERM)
                return real_rmtree(path, *args, **kwargs)

            with patch.object(installer.os, "geteuid", return_value=501), \
                 patch.object(installer.os, "getuid", return_value=501), \
                 patch.object(installer, "prepare_update", return_value=plan), \
                 patch.object(installer, "wait_for_parent_exit", side_effect=interrupt_wait), \
                 patch.object(installer.shutil, "rmtree", side_effect=signal_during_cleanup), \
                 patch.object(installer, "_launch_app", launch), \
                 redirect_stdout(io.StringIO()), redirect_stderr(stderr):
                result = installer.main(self._main_args(plan.target, parent_pid))
        self.assertEqual(result, 1)
        self.assertIn("received signal", stderr.getvalue())
        launch.assert_not_called()

    def test_broken_pipe_on_ready_print_returns_a_failure_without_traceback(self):
        with tempfile.TemporaryDirectory(prefix="igloo-broken-ready-") as temporary:
            plan = self._main_plan(Path(temporary))
            stderr = io.StringIO()
            real_print = builtins.print

            def fail_stdout(*args, **kwargs):
                if kwargs.get("file") is None or kwargs.get("file") is sys.stdout:
                    raise BrokenPipeError("closed stdout")
                return real_print(*args, **kwargs)

            with patch.object(installer.os, "geteuid", return_value=501), \
                 patch.object(installer.os, "getuid", return_value=501), \
                 patch.object(installer, "prepare_update", return_value=plan), \
                 patch("builtins.print", side_effect=fail_stdout), \
                 redirect_stdout(io.StringIO()), redirect_stderr(stderr):
                result = installer.main(self._main_args(plan.target, os.getppid()))
        self.assertEqual(result, 1)
        self.assertIn("could not be completed", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())

    def test_broken_pipe_on_stderr_is_suppressed(self):
        class ClosedPipe:
            def write(self, _value):
                raise BrokenPipeError("closed stderr")

            def flush(self):
                raise BrokenPipeError("closed stderr")

        with patch.object(installer.os, "geteuid", return_value=0), \
             patch.object(installer.os, "getuid", return_value=0), \
             redirect_stderr(ClosedPipe()):
            self.assertEqual(installer.main(self._main_args(Path("/tmp/Igloo.app"), os.getppid())), 1)

    def test_signal_during_handler_restore_is_deferred(self):
        original_signal = signal.signal
        counts = {}

        def deliver_before_restore(signum, handler):
            counts[signum] = counts.get(signum, 0) + 1
            if signum == signal.SIGTERM and counts[signum] == 2:
                os.kill(os.getpid(), signal.SIGTERM)
            return original_signal(signum, handler)

        with patch.object(installer.os, "geteuid", return_value=0), \
             patch.object(installer.os, "getuid", return_value=0), \
             patch.object(installer.signal, "signal", side_effect=deliver_before_restore), \
             redirect_stderr(io.StringIO()):
            result = installer.main(self._main_args(Path("/tmp/Igloo.app"), os.getppid()))
        self.assertEqual(result, 1)


if __name__ == "__main__":
    unittest.main()
