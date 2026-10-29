#!/usr/bin/env python3
"""Install one verified Igloo release archive beside the running app."""

from __future__ import annotations

import argparse
import ctypes
from dataclasses import dataclass
import hashlib
import inspect
import os
from pathlib import Path, PurePosixPath
import plistlib
import re
import signal
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import uuid
import zipfile


RELEASE_BUNDLE_ID = "local.igloo"
MAX_ARCHIVE_BYTES = 512 * 1024 * 1024
MAX_EXPANDED_BYTES = 2 * 1024 * 1024 * 1024
MAX_SYMLINK_BYTES = 4096
VERSION_PATTERN = re.compile(r"(?:0|[1-9][0-9]{0,5})\.(?:0|[1-9][0-9]{0,5})(?:\.(?:0|[1-9][0-9]{0,5}))?\Z")
SHA256_PATTERN = re.compile(r"[a-f0-9]{64}\Z")


class InstallError(Exception):
    """A safe, user-facing update installation refusal."""


class InstallInterrupted(InstallError):
    """Raised when the installer receives a termination signal."""


class _SignalState:
    def __init__(self):
        self.previous: dict[int, object] = {}
        self.deferred = False
        self.pending: int | None = None
        self.committed = False

    def handle(self, signum, _frame):
        if self.committed:
            return
        if self.deferred:
            self.pending = signum
            return
        raise InstallInterrupted(f"The update installer received signal {signum}.")

    def check(self):
        if self.pending is not None:
            signum, self.pending = self.pending, None
            raise InstallInterrupted(f"The update installer received signal {signum}.")


_SIGNAL_STATE: _SignalState | None = None


@dataclass
class InstallPlan:
    target: Path
    staging: Path
    candidate: Path
    runtime: str
    old_version: str
    old_build: int


def _version(value: object) -> tuple[int, int, int]:
    if not isinstance(value, str) or not VERSION_PATTERN.fullmatch(value):
        raise InstallError("The update contains an invalid version.")
    parts = tuple(int(part) for part in value.split("."))
    return (parts + (0, 0, 0))[:3]


def _build(value: object) -> int:
    if isinstance(value, bool):
        raise InstallError("The update contains an invalid build number.")
    try:
        result = int(value)
    except (TypeError, ValueError) as error:
        raise InstallError("The update contains an invalid build number.") from error
    if result < 1 or str(result) != str(value):
        raise InstallError("The update contains an invalid build number.")
    return result


def _is_translocated(path: Path) -> bool:
    return any(part.casefold() == "apptranslocation" for part in path.parts)


def _has_write_and_search_bits(path: Path) -> bool:
    info = path.stat()
    groups = set(os.getgroups()) | {os.getegid()}
    if info.st_uid == os.geteuid():
        bits = stat.S_IWUSR | stat.S_IXUSR
    elif info.st_gid in groups:
        bits = stat.S_IWGRP | stat.S_IXGRP
    else:
        bits = stat.S_IWOTH | stat.S_IXOTH
    return info.st_mode & bits == bits


def _read_bundle(path: Path) -> tuple[dict, Path]:
    if path.is_symlink() or not path.is_dir():
        raise InstallError("The Igloo application bundle is invalid.")
    contents = path / "Contents"
    info_path = contents / "Info.plist"
    executable = contents / "MacOS" / "Igloo"
    if contents.is_symlink() or info_path.is_symlink() or not info_path.is_file():
        raise InstallError("The Igloo application bundle is incomplete.")
    if executable.is_symlink() or not executable.is_file() or not os.access(executable, os.X_OK):
        raise InstallError("The Igloo application bundle is incomplete.")
    try:
        with info_path.open("rb") as handle:
            info = plistlib.load(handle)
    except (OSError, plistlib.InvalidFileException, ValueError) as error:
        raise InstallError("The Igloo application metadata is invalid.") from error
    if not isinstance(info, dict) or info.get("CFBundlePackageType") != "APPL" or info.get("CFBundleExecutable") != "Igloo":
        raise InstallError("The Igloo application metadata is invalid.")
    return info, executable


def _validate_target(target_path: str | os.PathLike[str], runtime: str) -> tuple[Path, dict]:
    raw = Path(target_path).expanduser()
    if not raw.is_absolute() or _is_translocated(raw):
        raise InstallError("Igloo cannot update from a translocated or invalid location. Move it to a writable folder first.")
    if raw.is_symlink():
        raise InstallError("Igloo cannot update a linked application bundle.")
    try:
        target = raw.resolve(strict=True)
        parent = target.parent
        if _is_translocated(target) or _is_translocated(parent):
            raise InstallError("Igloo cannot update from a translocated location. Move it to a writable folder first.")
        if not parent.is_dir() or not os.access(parent, os.W_OK | os.X_OK) or not _has_write_and_search_bits(parent):
            raise InstallError("This Igloo location is read-only for your account. Move Igloo to a writable folder first.")
        if os.statvfs(parent).f_flag & getattr(os, "ST_RDONLY", 0):
            raise InstallError("This Igloo location is read-only for your account. Move Igloo to a writable folder first.")
    except OSError as error:
        raise InstallError("Igloo cannot update from this location. Move it to a writable folder first.") from error

    info, _ = _read_bundle(target)
    if info.get("CFBundleIdentifier") != RELEASE_BUNDLE_ID:
        raise InstallError("Igloo Dev and other app copies cannot install release updates.")
    if info.get("IglooRuntime") != runtime:
        raise InstallError("The update does not match this Igloo Python runtime.")
    _version(info.get("CFBundleShortVersionString"))
    _build(info.get("CFBundleVersion"))
    return target, info


def _validate_zip(archive: Path) -> None:
    try:
        with zipfile.ZipFile(archive) as bundle:
            entries = bundle.infolist()
            if not entries:
                raise InstallError("The update archive is empty.")
            total_size = 0
            names: set[str] = set()
            entry_kinds: dict[str, int] = {}
            has_info = False
            for entry in entries:
                name = entry.filename
                if "\\" in name or name.startswith("/"):
                    raise InstallError("The update archive contains an unsafe path.")
                raw_parts = name.rstrip("/").split("/")
                if not raw_parts or any(part in {"", ".", ".."} for part in raw_parts):
                    raise InstallError("The update archive contains an unsafe path.")
                top = raw_parts[0]
                if top != "Igloo.app" and top != "__MACOSX":
                    raise InstallError("The update archive contains an unexpected item.")
                if top == "__MACOSX" and len(raw_parts) < 2 and not entry.is_dir():
                    raise InstallError("The update archive contains an unexpected item.")
                normalized = "/".join(raw_parts).casefold()
                if normalized in names:
                    raise InstallError("The update archive contains duplicate paths.")
                names.add(normalized)
                if entry.flag_bits & 0x1:
                    raise InstallError("The update archive is encrypted and cannot be installed.")
                total_size += entry.file_size
                if total_size > MAX_EXPANDED_BYTES:
                    raise InstallError("The update archive is too large to install.")
                mode = (entry.external_attr >> 16) & 0xFFFF
                kind = stat.S_IFMT(mode)
                if entry.is_dir() and kind == 0:
                    kind = stat.S_IFDIR
                if kind not in {0, stat.S_IFREG, stat.S_IFDIR, stat.S_IFLNK}:
                    raise InstallError("The update archive contains an unsupported file.")
                if kind == stat.S_IFLNK:
                    if top != "Igloo.app":
                        raise InstallError("The update archive contains an unsafe link.")
                    if entry.file_size > MAX_SYMLINK_BYTES:
                        raise InstallError("The update archive contains an unsafe link.")
                    with bundle.open(entry) as source:
                        link = source.read(MAX_SYMLINK_BYTES + 1).decode("utf-8", errors="strict")
                    if len(link.encode("utf-8")) > MAX_SYMLINK_BYTES:
                        raise InstallError("The update archive contains an unsafe link.")
                    if not link or Path(link).is_absolute() or "\\" in link:
                        raise InstallError("The update archive contains an unsafe link.")
                    if ".." in PurePosixPath(link).parts:
                        raise InstallError("The update archive contains an unsafe link.")
                entry_kinds[normalized] = kind
                if name.rstrip("/") == "Igloo.app/Contents/Info.plist":
                    has_info = True
            for name, kind in entry_kinds.items():
                parts = name.split("/")
                for index in range(1, len(parts)):
                    parent_kind = entry_kinds.get("/".join(parts[:index]))
                    if parent_kind in {stat.S_IFREG, stat.S_IFLNK}:
                        raise InstallError("The update archive contains conflicting paths.")
                if kind in {stat.S_IFREG, stat.S_IFLNK} and any(
                    other.startswith(name + "/") for other in entry_kinds
                ):
                    raise InstallError("The update archive contains conflicting paths.")
            if not has_info:
                raise InstallError("The update archive does not contain an Igloo app.")
    except InstallError:
        raise
    except (OSError, zipfile.BadZipFile, RuntimeError, UnicodeError) as error:
        raise InstallError("The update archive could not be read.") from error


def _copy_verified_archive(source: Path, destination: Path, expected_sha256: str) -> None:
    if not SHA256_PATTERN.fullmatch(expected_sha256):
        raise InstallError("The update checksum is invalid.")
    try:
        if source.is_symlink() or not source.is_file() or source.stat().st_size > MAX_ARCHIVE_BYTES:
            raise InstallError("The verified update archive is unavailable or too large.")
        digest = hashlib.sha256()
        with source.open("rb") as input_file, destination.open("xb") as output_file:
            while True:
                chunk = input_file.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
                output_file.write(chunk)
            output_file.flush()
            os.fsync(output_file.fileno())
        os.chmod(destination, 0o600)
    except InstallError:
        raise
    except OSError as error:
        raise InstallError("The verified update archive could not be staged.") from error
    if digest.hexdigest() != expected_sha256:
        raise InstallError("The update failed its SHA-256 check. Try downloading it again.")


def _extract_archive(archive: Path, staging: Path) -> Path:
    try:
        with zipfile.ZipFile(archive) as bundle:
            expanded_bytes = 0
            links = []
            for entry in bundle.infolist():
                raw_parts = entry.filename.rstrip("/").split("/")
                if raw_parts[0] == "__MACOSX":
                    continue
                destination = staging.joinpath(*raw_parts)
                mode = (entry.external_attr >> 16) & 0xFFFF
                kind = stat.S_IFMT(mode)
                if entry.is_dir() or kind == stat.S_IFDIR:
                    destination.mkdir(parents=True, exist_ok=True)
                    continue
                if kind == stat.S_IFLNK:
                    links.append((entry, destination))
                    continue
                destination.parent.mkdir(parents=True, exist_ok=True)
                for parent in (destination.parent, *destination.parent.parents):
                    if parent == staging.parent:
                        break
                    if parent.is_symlink() or not parent.is_dir():
                        raise InstallError("The update archive contains an unsafe path.")
                with bundle.open(entry) as source, destination.open("xb") as output:
                    while True:
                        chunk = source.read(1024 * 1024)
                        if not chunk:
                            break
                        expanded_bytes += len(chunk)
                        if expanded_bytes > MAX_EXPANDED_BYTES:
                            raise InstallError("The update archive is too large to install.")
                        output.write(chunk)
                permissions = mode & 0o777
                os.chmod(destination, permissions or 0o644)
            for entry, destination in links:
                with bundle.open(entry) as source:
                    payload = source.read(MAX_SYMLINK_BYTES + 1)
                if len(payload) > MAX_SYMLINK_BYTES:
                    raise InstallError("The update archive contains an unsafe link.")
                expanded_bytes += len(payload)
                if expanded_bytes > MAX_EXPANDED_BYTES:
                    raise InstallError("The update archive is too large to install.")
                link = payload.decode("utf-8", errors="strict")
                if not link or Path(link).is_absolute() or "\\" in link:
                    raise InstallError("The update archive contains an unsafe link.")
                destination.parent.mkdir(parents=True, exist_ok=True)
                for parent in (destination.parent, *destination.parent.parents):
                    if parent == staging.parent:
                        break
                    if parent.is_symlink() or not parent.is_dir():
                        raise InstallError("The update archive contains an unsafe path.")
                if destination.is_dir() and not destination.is_symlink():
                    destination.rmdir()
                elif destination.exists() or destination.is_symlink():
                    destination.unlink()
                os.symlink(link, destination)
    except InstallError:
        raise
    except (OSError, zipfile.BadZipFile, RuntimeError, UnicodeError) as error:
        raise InstallError("The update archive could not be extracted.") from error
    candidate = staging / "Igloo.app"
    if not candidate.is_dir():
        raise InstallError("The update archive does not contain an Igloo app.")
    return candidate


def _verify_code_signature(candidate: Path) -> None:
    try:
        subprocess.run(["/usr/bin/codesign", "--verify", "--deep", "--strict", str(candidate)],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=60)
    except (OSError, subprocess.SubprocessError) as error:
        raise InstallError("The downloaded Igloo app failed its code signature check.") from error


def prepare_update(archive_path: str | os.PathLike[str], expected_sha256: str,
                   expected_version: str, expected_build: int, runtime: str,
                   target_path: str | os.PathLike[str], signature_verifier=_verify_code_signature) -> InstallPlan:
    if runtime not in {"bundled", "system"}:
        raise InstallError("The update has an unsupported Python runtime.")
    expected_version_parts = _version(expected_version)
    expected_build_number = _build(expected_build)
    target, old_info = _validate_target(target_path, runtime)
    old_version = _version(old_info.get("CFBundleShortVersionString"))
    old_build = _build(old_info.get("CFBundleVersion"))
    if (expected_version_parts, expected_build_number) <= (old_version, old_build):
        raise InstallError("This update is not newer than the installed Igloo version.")

    staging = Path(tempfile.mkdtemp(prefix=".igloo-update-", dir=target.parent)).resolve(strict=True)
    if staging.parent != target.parent:
        shutil.rmtree(staging, ignore_errors=True)
        raise InstallError("Igloo could not create a private staging folder beside the app.")
    os.chmod(staging, 0o700)
    try:
        private_archive = staging / "update.zip"
        _copy_verified_archive(Path(archive_path), private_archive, expected_sha256)
        _validate_zip(private_archive)
        candidate = _extract_archive(private_archive, staging)
        new_info, _ = _read_bundle(candidate)
        if new_info.get("CFBundleIdentifier") != RELEASE_BUNDLE_ID:
            raise InstallError("The downloaded app has the wrong bundle identifier.")
        if new_info.get("CFBundleShortVersionString") != expected_version:
            raise InstallError("The downloaded app version does not match the verified release.")
        if _build(new_info.get("CFBundleVersion")) != expected_build_number:
            raise InstallError("The downloaded app build does not match the verified release.")
        if new_info.get("IglooRuntime") != runtime:
            raise InstallError("The downloaded app does not match this Igloo Python runtime.")
        signature_verifier(candidate)
        return InstallPlan(target=target, staging=staging, candidate=candidate, runtime=runtime,
                           old_version=str(old_info["CFBundleShortVersionString"]),
                           old_build=_build(old_info["CFBundleVersion"]))
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def _write_failure_file(target: Path, message: str) -> Path | None:
    path = target.parent / f".Igloo-update-failure-{uuid.uuid4().hex}.txt"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(message[:2048])
            handle.write("\n")
        return path
    except OSError:
        return None


def _launch_app(path: Path, *, update_failed: bool = False,
                failure_file: Path | None = None) -> None:
    try:
        command = ["/usr/bin/open", "-n", str(path)]
        if update_failed:
            command.extend(["--args", "--update-install-failed"])
            if failure_file is not None:
                command.extend(["--update-install-failure-file", str(failure_file)])
        subprocess.run(command, check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=30)
    except (OSError, subprocess.SubprocessError) as error:
        raise InstallError("Igloo could not be reopened after the update.") from error


def _app_executable_path(pid: int) -> Path | None:
    try:
        library = ctypes.CDLL("/usr/lib/libproc.dylib")
        proc_pidpath = library.proc_pidpath
        proc_pidpath.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
        proc_pidpath.restype = ctypes.c_int
        buffer = ctypes.create_string_buffer(4096)
        length = proc_pidpath(pid, buffer, len(buffer))
        if length <= 0:
            return None
        return Path(os.fsdecode(buffer.value))
    except (OSError, AttributeError):
        return None


def _processes_in_app(target: Path) -> set[int] | None:
    try:
        result = subprocess.run(["/bin/ps", "-axo", "pid="], check=True,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    expected_executable = (target.resolve() / "Contents" / "MacOS" / "Igloo").resolve()
    matches = set()
    for value in result.stdout.splitlines():
        try:
            pid = int(value.strip())
        except ValueError:
            continue
        if pid == os.getpid():
            continue
        executable = _app_executable_path(pid)
        if executable is None:
            continue
        try:
            if executable.resolve() != expected_executable:
                continue
        except (OSError, ValueError):
            continue
        matches.add(pid)
    return matches


def _wait_for_app_alive(target: Path, timeout: float = 20.0, stable_for: float = 4.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        processes = _processes_in_app(target)
        if processes:
            stable_deadline = time.monotonic() + stable_for
            while time.monotonic() < stable_deadline:
                time.sleep(min(0.2, max(0.0, stable_deadline - time.monotonic())))
                current = _processes_in_app(target)
                if current is None or not (processes & current):
                    break
            else:
                return True
        time.sleep(0.2)
    return False


def _wait_for_app_exit(target: Path, timeout: float = 30.0, stable_for: float = 2.0) -> bool:
    """Return true only after the app's main executable is absent for a quiet interval."""
    deadline = time.monotonic() + timeout
    absent_since = None
    while time.monotonic() < deadline:
        processes = _processes_in_app(target)
        if processes == set():
            absent_since = absent_since or time.monotonic()
            if time.monotonic() - absent_since >= stable_for:
                return True
        else:
            absent_since = None
        time.sleep(0.2)
    return False


def _invoke_with_optional_keywords(function, path: Path, **kwargs):
    try:
        parameters = inspect.signature(function).parameters.values()
        accepts_keywords = any(parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in parameters)
        accepted = {key: value for key, value in kwargs.items()
                    if accepts_keywords or key in inspect.signature(function).parameters}
    except (TypeError, ValueError):
        accepted = kwargs
    return function(path, **accepted)


def _target_is_launchable(plan: InstallPlan) -> bool:
    try:
        info, _ = _read_bundle(plan.target)
        return (info.get("CFBundleIdentifier") == RELEASE_BUNDLE_ID and
                info.get("IglooRuntime") == plan.runtime)
    except (OSError, InstallError):
        return False


def _launch_failed_target(plan: InstallPlan, message: str, launcher, liveness) -> BaseException | None:
    failure_file = _write_failure_file(plan.target, message)
    try:
        _invoke_with_optional_keywords(launcher, plan.target, update_failed=True,
                                       failure_file=failure_file)
        if not _invoke_with_optional_keywords(liveness, plan.target,
                                              timeout=20.0, stable_for=4.0):
            raise InstallError("The reopened Igloo app did not stay open.")
        if failure_file is not None:
            try:
                failure_file.unlink(missing_ok=True)
            except OSError:
                pass
        return None
    except BaseException as error:
        return error


def _parent_is_alive(pid: int) -> bool:
    if pid <= 1 or os.getppid() != pid:
        return False
    return _process_exists(pid)


def complete_update(plan: InstallPlan, launcher=_launch_app, liveness=_wait_for_app_alive,
                    process_exit=_wait_for_app_exit, transaction_state: dict | None = None) -> None:
    backup = None
    old_moved = False
    target_quiescent: bool | None = None
    state = _SIGNAL_STATE
    owned_signal_state = False
    try:
        if state is None:
            state = _install_signal_handlers()
            owned_signal_state = True
        if transaction_state is not None:
            transaction_state["entered"] = True
        backup = plan.target.with_name(f".{plan.target.name}.backup-{uuid.uuid4().hex}")
        validated_target, current = _validate_target(plan.target, plan.runtime)
        if (validated_target != plan.target or
                current.get("CFBundleIdentifier") != RELEASE_BUNDLE_ID or
                current.get("CFBundleShortVersionString") != plan.old_version or
                _build(current.get("CFBundleVersion")) != plan.old_build):
            raise InstallError("The installed Igloo app changed while the update was preparing.")
        state.deferred = True
        state.check()
        os.rename(plan.target, backup)
        old_moved = True
        state.check()
        os.rename(plan.candidate, plan.target)
        state.check()
        _invoke_with_optional_keywords(launcher, plan.target, update_failed=False)
        state.check()
        if not _invoke_with_optional_keywords(liveness, plan.target,
                                              timeout=20.0, stable_for=4.0):
            target_quiescent = _invoke_with_optional_keywords(
                process_exit, plan.target, timeout=30.0, stable_for=2.0)
            if not target_quiescent:
                raise InstallError("The updated Igloo app may still be running; its bundle and previous backup were kept.")
            raise InstallError("The updated Igloo app did not stay open after relaunch.")
        state.check()
        # Once backup cleanup starts, rollback is no longer possible. Ignore any
        # later signal rather than report failure after destroying the backup.
        state.committed = True
        shutil.rmtree(backup, ignore_errors=True)
    except BaseException as error:
        if state is None:
            state = _SIGNAL_STATE
        if state is not None:
            state.deferred = True
        if transaction_state is not None and (state is not None or backup is not None):
            transaction_state["entered"] = True
        if backup is None:
            backup = plan.target.with_name(f".{plan.target.name}.backup-{uuid.uuid4().hex}")
        # A signal can arrive after rename(2) succeeds but before the next
        # Python assignment, so the backup's presence is the transaction fact.
        old_moved = old_moved or backup.exists()
        rollback_errors = []
        target_present = plan.target.exists() or plan.target.is_symlink()
        safe_to_remove_target = not old_moved or not target_present
        if old_moved and target_present:
            if target_quiescent is not None:
                safe_to_remove_target = target_quiescent
            else:
                try:
                    safe_to_remove_target = _invoke_with_optional_keywords(
                        process_exit, plan.target, timeout=30.0, stable_for=2.0)
                except BaseException as failure:
                    rollback_errors.append(failure)
                    safe_to_remove_target = False
        if target_present and old_moved and safe_to_remove_target:
            failed_candidate = plan.staging / "failed-Igloo.app"
            try:
                os.rename(plan.target, failed_candidate)
            except BaseException as failure:
                rollback_errors.append(failure)
                try:
                    shutil.rmtree(plan.target)
                except BaseException as cleanup_failure:
                    rollback_errors.append(cleanup_failure)
        restored = bool(plan.target.exists() and not backup.exists())
        restore_error = None
        if old_moved and backup.exists():
            try:
                if not (plan.target.exists() or plan.target.is_symlink()):
                    os.rename(backup, plan.target)
                    restored = True
                elif safe_to_remove_target:
                    # A failed quarantine/removal must never suppress the
                    # independent attempt to restore the original bundle.
                    try:
                        shutil.rmtree(plan.target)
                    except BaseException as cleanup_failure:
                        rollback_errors.append(cleanup_failure)
                    if not (plan.target.exists() or plan.target.is_symlink()):
                        os.rename(backup, plan.target)
                        restored = True
                    else:
                        restore_error = InstallError("The update bundle could not be removed for rollback.")
                else:
                    restore_error = InstallError("The updated app may still be running; its backup was kept.")
            except BaseException as failure:
                restore_error = failure
        relaunch_error = None
        message = str(error) if isinstance(error, InstallError) else "The update could not be installed."
        if rollback_errors:
            message += " Rollback encountered an additional filesystem error."
        if restore_error is not None:
            message += " Restoring the previous Igloo app failed; its backup was kept."
        elif not restored or not plan.target.exists():
            message += " The previous Igloo app could not be restored."
        elif old_moved:
            message += " The previous Igloo app was restored."
        else:
            message += " The previous Igloo app was restored or left in place."
        new_bundle_kept = bool(restore_error is not None and old_moved and
                               backup.exists() and plan.target.exists())
        if plan.target.exists() and _target_is_launchable(plan) and not new_bundle_kept:
            relaunch_error = _launch_failed_target(plan, message, launcher, liveness)
        if relaunch_error is not None and restored:
            message += " The previous Igloo app was restored, but could not be reopened. Start it manually."
        elif relaunch_error is not None:
            message += " The available Igloo app could not be reopened. Start it manually."
        elif new_bundle_kept:
            message += " The updated app remains at the target and was not reopened while rollback was incomplete."
        elif plan.target.exists() and _target_is_launchable(plan):
            message += " Igloo was reopened with the installation failure details."
        if state is not None:
            state.pending = None
        raise InstallError(message) from error
    finally:
        if state is not None:
            state.deferred = True
            state.pending = None
            if owned_signal_state:
                _restore_signal_handlers(state)
        shutil.rmtree(plan.staging, ignore_errors=True)


def install_update(archive_path: str | os.PathLike[str], expected_sha256: str,
                   expected_version: str, expected_build: int, runtime: str,
                   target_path: str | os.PathLike[str], launcher=_launch_app,
                   liveness=_wait_for_app_alive, signature_verifier=_verify_code_signature,
                   process_exit=_wait_for_app_exit) -> None:
    plan = prepare_update(archive_path, expected_sha256, expected_version,
                          expected_build, runtime, target_path, signature_verifier=signature_verifier)
    complete_update(plan, launcher=launcher, liveness=liveness, process_exit=process_exit)


def _process_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def wait_for_parent_exit(pid: int, timeout: float = 120.0) -> None:
    if pid <= 1 or pid == os.getpid():
        raise InstallError("The update installer could not identify the running app.")
    deadline = time.monotonic() + timeout
    while True:
        current_parent = os.getppid()
        if current_parent == 1 or current_parent != pid:
            return
        if not _process_exists(pid):
            return
        if time.monotonic() >= deadline:
            raise InstallError("Igloo did not close, so the update was canceled.")
        time.sleep(0.1)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Install a verified Igloo release and reopen the app.")
    parser.add_argument("--archive", required=True, help="path to the downloaded release ZIP")
    parser.add_argument("--sha256", required=True, help="expected SHA-256 for the release ZIP")
    parser.add_argument("--version", required=True, help="verified release version")
    parser.add_argument("--build", required=True, type=int, help="verified release build number")
    parser.add_argument("--runtime", required=True, choices=("bundled", "system"), help="the selected Python runtime")
    parser.add_argument("--target", required=True, help="the currently running Igloo app bundle")
    parser.add_argument("--parent-pid", required=True, type=int, help="the running app process that must exit before replacement")
    parser.add_argument("--helper-workspace", help=argparse.SUPPRESS)
    return parser


def _install_signal_handlers() -> _SignalState:
    global _SIGNAL_STATE
    state = _SignalState()
    _SIGNAL_STATE = state
    try:
        for value in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
            state.previous[value] = signal.signal(value, state.handle)
    except BaseException:
        _restore_signal_handlers(state)
        raise
    return state


def _restore_signal_handlers(state: _SignalState) -> None:
    global _SIGNAL_STATE
    state.deferred = True
    for signum, handler in state.previous.items():
        signal.signal(signum, handler)
    state.pending = None
    state.deferred = False
    if _SIGNAL_STATE is state:
        _SIGNAL_STATE = None


def _safe_stderr(message: str) -> None:
    try:
        print(message, file=sys.stderr, flush=True)
    except OSError:
        pass


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    plan = None
    ready = False
    transaction_state = {"entered": False}
    state = None
    try:
        state = _install_signal_handlers()
        if os.geteuid() == 0 or os.geteuid() != os.getuid():
            raise InstallError("Igloo updates run only as the signed-in user and never with administrator rights.")
        plan = prepare_update(args.archive, args.sha256, args.version, args.build, args.runtime, args.target)
        print("READY", flush=True)
        ready = True
        wait_for_parent_exit(args.parent_pid)
        complete_update(plan, transaction_state=transaction_state)
        return 0
    except Exception as error:
        if state is None:
            state = _SIGNAL_STATE
        if state is not None:
            state.deferred = True
        if plan is not None:
            shutil.rmtree(plan.staging, ignore_errors=True)
        message = str(error) if isinstance(error, InstallError) else \
            "The update could not be completed. The previous Igloo app was kept or restored."
        if plan is not None and ready and not transaction_state["entered"] and not _parent_is_alive(args.parent_pid):
            if state is not None:
                state.deferred = True
            relaunch_error = _launch_failed_target(plan, message, _launch_app, _wait_for_app_alive)
            if relaunch_error is not None:
                message += " The previous Igloo app could not be reopened. Start it manually."
            else:
                message += " The previous Igloo app was reopened with the failure details."
        _safe_stderr(message)
        return 1
    finally:
        if state is None:
            state = _SIGNAL_STATE
        if state is not None:
            state.deferred = True
            _restore_signal_handlers(state)
        if args.helper_workspace:
            shutil.rmtree(args.helper_workspace, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
