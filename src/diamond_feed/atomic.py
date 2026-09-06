"""Durable staging and recoverable multi-file publication helpers."""

from dataclasses import dataclass
import json
import os
from pathlib import Path
import shutil
import stat
from typing import Iterable, Literal
import uuid


MANIFEST_PREFIX = ".diamond-feed-publication."
MANIFEST_SUFFIX = ".json"
MANIFEST_FIELDS = {"version", "transaction_id", "destinations", "backups"}


@dataclass(frozen=True, slots=True)
class StagedFile:
    destination: Path
    temporary: Path


@dataclass(frozen=True, slots=True)
class CommitResult:
    """A successful publication, including any best-effort cleanup debt."""

    status: Literal["committed", "committed-with-cleanup-pending"]
    cleanup_errors: tuple[str, ...] = ()

    @property
    def committed(self) -> bool:
        return True

    @property
    def cleanup_pending(self) -> bool:
        return self.status == "committed-with-cleanup-pending"


class PublicationOperationError(RuntimeError):
    """An operation failed and cleanup of its temporary artifacts also failed."""

    def __init__(self, primary_failure: str, cleanup_failures: Iterable[str]):
        self.primary_failure = primary_failure
        self.cleanup_failures = tuple(cleanup_failures)
        cleanup = "; ".join(self.cleanup_failures) or "none"
        super().__init__(
            f"publication operation failed; primary failure: {primary_failure}; "
            f"cleanup failures: {cleanup}"
        )


class PublicationRollbackError(RuntimeError):
    """A publication failed and its automatic rollback was incomplete."""

    def __init__(
        self,
        primary_failure: str,
        rollback_failures: Iterable[str],
        recovery_backups: Iterable[Path],
        destinations_without_backup: Iterable[Path],
    ):
        self.primary_failure = primary_failure
        self.rollback_failures = tuple(rollback_failures)
        self.recovery_backups = tuple(
            _lexical_absolute(path) for path in recovery_backups if _exists_no_follow(path)
        )
        self.destinations_without_backup = tuple(
            _lexical_absolute(path) for path in destinations_without_backup
        )
        recoverable = ", ".join(str(path) for path in self.recovery_backups) or "none"
        no_backup = ", ".join(str(path) for path in self.destinations_without_backup) or "none"
        failures = "; ".join(self.rollback_failures)
        super().__init__(
            f"publication rollback incomplete; primary failure: {primary_failure}; "
            f"rollback/cleanup failures: {failures}; recoverable backups: {recoverable}; "
            f"destinations with no backup: {no_backup}"
        )


@dataclass(frozen=True, slots=True)
class _CompletionManifest:
    path: Path
    transaction_id: str
    destinations: tuple[Path, ...]
    backups: tuple[Path, ...]


def _sibling(path: Path, suffix: str) -> Path:
    return path.with_name(f"{path.name}{suffix}")


def _lexical_absolute(path: Path) -> Path:
    """Return an absolute normalized spelling without resolving filesystem links."""
    return Path(os.path.abspath(os.path.normpath(os.fspath(path))))


def _lexical_key(path: Path) -> str:
    return os.path.normcase(str(_lexical_absolute(path)))


def _resolved_key(path: Path) -> str:
    return os.path.normcase(str(path.resolve(strict=False)))


def _exists_no_follow(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    return True


def _remove(path: Path, action: str) -> str | None:
    try:
        path.unlink(missing_ok=True)
    except Exception as error:
        return f"{action} {_lexical_absolute(path)}: {error}"
    return None


def _remove_regular_artifact(path: Path, action: str) -> str | None:
    """Remove a manifest or backup only when its directory entry is a regular file."""
    lexical_path = _lexical_absolute(path)
    try:
        metadata = lexical_path.lstat()
    except FileNotFoundError:
        return None
    except Exception as error:
        return f"inspect {action} {lexical_path}: {error}"
    if not stat.S_ISREG(metadata.st_mode):
        kind = "symbolic link" if stat.S_ISLNK(metadata.st_mode) else "non-regular file"
        return f"refuse {action} {lexical_path}: {kind} is not safe to remove automatically"
    try:
        lexical_path.unlink()
    except Exception as error:
        return f"{action} {lexical_path}: {error}"
    return None


def _read_regular_text_no_follow(path: Path) -> str:
    """Read a regular file while rejecting links and entry swaps."""
    lexical_path = _lexical_absolute(path)
    before = lexical_path.lstat()
    if not stat.S_ISREG(before.st_mode):
        raise OSError("completion manifest is not a regular file")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(lexical_path, flags)
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise OSError("completion manifest changed to a non-regular file")
        if (
            before.st_dev != opened.st_dev
            or (before.st_ino and opened.st_ino and before.st_ino != opened.st_ino)
        ):
            raise OSError("completion manifest changed while opening")
        with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
            descriptor = -1
            return handle.read()
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def validate_output_layout(destinations: Iterable[Path]) -> None:
    """Reject aliases and fixed sibling-temporary collisions before staging."""
    destination_list = list(destinations)
    if not destination_list:
        raise ValueError("output layout requires at least one destination")
    destination_keys = [_resolved_key(path) for path in destination_list]
    if len(destination_keys) != len(set(destination_keys)):
        raise ValueError("output destinations must be unique after path resolution")
    temporary_keys = [_resolved_key(_sibling(path, ".tmp")) for path in destination_list]
    if len(temporary_keys) != len(set(temporary_keys)):
        raise ValueError("output fixed sibling temporaries must be unique after path resolution")
    collisions = set(destination_keys).intersection(temporary_keys)
    if collisions:
        raise ValueError("output destination collides with a fixed sibling temporary path")


def raise_with_cleanup(
    primary_error: Exception,
    primary_action: str,
    cleanup_failures: Iterable[str],
) -> None:
    if isinstance(primary_error, PublicationOperationError):
        primary_failure = primary_error.primary_failure
        failures = (*primary_error.cleanup_failures, *cleanup_failures)
    else:
        primary_failure = f"{primary_action}: {primary_error}"
        failures = tuple(cleanup_failures)
    if failures:
        raise PublicationOperationError(primary_failure, failures) from primary_error
    raise primary_error


def stage_text(path: Path, contents: str) -> StagedFile:
    """Write and fsync text to the destination's sibling `.tmp` without publishing it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = _sibling(path, ".tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(contents)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception as primary_error:
        cleanup = _remove(temporary, "remove staged temporary")
        raise_with_cleanup(
            primary_error,
            f"stage text for {path.resolve()}",
            () if cleanup is None else (cleanup,),
        )
    return StagedFile(destination=path, temporary=temporary)


def discard_staged(staged: Iterable[StagedFile]) -> tuple[str, ...]:
    failures: list[str] = []
    for item in staged:
        failure = _remove(item.temporary, "remove staged temporary")
        if failure is not None:
            failures.append(failure)
    return tuple(failures)


def _backup_path(destination: Path, transaction_id: str) -> Path:
    return _sibling(destination, f".{transaction_id}.bak")


def _backup_candidates(destination: Path) -> list[Path]:
    lexical_destination = _lexical_absolute(destination)
    prefix = f"{lexical_destination.name}."
    return sorted(
        path
        for path in lexical_destination.parent.iterdir()
        if path.name.startswith(prefix) and path.name.endswith(".bak")
    )


def _backup_transaction_id(destination: Path, backup: Path) -> str | None:
    prefix = f"{destination.name}."
    if not backup.name.startswith(prefix) or not backup.name.endswith(".bak"):
        return None
    transaction_id = backup.name[len(prefix) : -len(".bak")]
    if len(transaction_id) != 32 or any(character not in "0123456789abcdef" for character in transaction_id):
        return None
    return transaction_id


def _manifest_parent(destinations: Iterable[Path]) -> Path:
    lexical_parents = [str(_lexical_absolute(path).parent) for path in destinations]
    if not lexical_parents:
        raise ValueError("at least one staged destination is required")
    try:
        return Path(os.path.commonpath(lexical_parents))
    except ValueError as error:
        raise ValueError("staged destinations must share a filesystem root") from error


def _manifest_path(destinations: Iterable[Path], transaction_id: str) -> Path:
    return _manifest_parent(destinations) / f"{MANIFEST_PREFIX}{transaction_id}{MANIFEST_SUFFIX}"


def _manifest_transaction_id(path: Path) -> str | None:
    if not path.name.startswith(MANIFEST_PREFIX) or not path.name.endswith(MANIFEST_SUFFIX):
        return None
    transaction_id = path.name[len(MANIFEST_PREFIX) : -len(MANIFEST_SUFFIX)]
    if len(transaction_id) != 32 or any(character not in "0123456789abcdef" for character in transaction_id):
        return None
    return transaction_id


def _manifest_candidates(directory: Path) -> list[tuple[Path, str]]:
    candidates: list[tuple[Path, str]] = []
    for path in directory.iterdir():
        transaction_id = _manifest_transaction_id(path)
        if transaction_id is not None:
            candidates.append((path, transaction_id))
    return sorted(candidates)


def _manifest_member(value: str) -> Path:
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("manifest paths must be canonical lexical absolute paths")
    lexical_path = _lexical_absolute(path)
    if os.path.normcase(value) != os.path.normcase(str(lexical_path)):
        raise ValueError("manifest paths must be canonical lexical absolute paths")
    return lexical_path


def _load_manifest(
    path: Path,
    transaction_id: str,
    relevant_destinations: set[str] | None = None,
) -> _CompletionManifest | None:
    try:
        payload = json.loads(_read_regular_text_no_follow(path))
        if type(payload) is not dict or set(payload) != MANIFEST_FIELDS:
            return None
        if payload["version"] != 1 or type(payload["version"]) is not int:
            return None
        if payload["transaction_id"] != transaction_id or type(payload["transaction_id"]) is not str:
            return None
        if type(payload["destinations"]) is not list or not all(type(item) is str for item in payload["destinations"]):
            return None
        if type(payload["backups"]) is not list or not all(type(item) is str for item in payload["backups"]):
            return None
        destinations = tuple(_manifest_member(item) for item in payload["destinations"])
        backups = tuple(_manifest_member(item) for item in payload["backups"])
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        return None
    destination_keys = {_lexical_key(destination) for destination in destinations}
    backup_keys = {_lexical_key(backup) for backup in backups}
    if (
        not destinations
        or len(destinations) != len(destination_keys)
        or len(backups) != len(backup_keys)
    ):
        return None
    if _lexical_key(_manifest_path(destinations, transaction_id)) != _lexical_key(path):
        return None
    possible_backups = {
        _lexical_key(_backup_path(destination, transaction_id))
        for destination in destinations
    }
    if not backup_keys <= possible_backups:
        return None
    if relevant_destinations is not None and not relevant_destinations.intersection(destination_keys):
        return None
    try:
        actual_backups = [
            candidate
            for destination in destinations
            for candidate in _backup_candidates(destination)
            if _backup_transaction_id(destination, candidate) == transaction_id
        ]
        if any(not stat.S_ISREG(candidate.lstat().st_mode) for candidate in actual_backups):
            return None
    except OSError:
        return None
    if not {_lexical_key(candidate) for candidate in actual_backups} <= backup_keys:
        return None
    return _CompletionManifest(_lexical_absolute(path), transaction_id, destinations, backups)


def _preflight(destinations: Iterable[Path]) -> list[str]:
    """Clean only backups authorized by one complete transaction manifest."""
    destination_list = list(destinations)
    relevant_destinations = {_lexical_key(path) for path in destination_list}
    manifest_directory = _manifest_parent(destination_list)
    manifests: dict[Path, _CompletionManifest] = {}
    authorized: set[str] = set()
    for manifest_path, transaction_id in _manifest_candidates(manifest_directory):
        manifest = _load_manifest(manifest_path, transaction_id, relevant_destinations)
        if manifest is None:
            continue
        manifests[manifest.path] = manifest
        authorized.update(_lexical_key(backup) for backup in manifest.backups)

    cleanup_errors: list[str] = []
    try:
        for manifest in manifests.values():
            for backup in manifest.backups:
                if not _exists_no_follow(backup):
                    continue
                failure = _remove_regular_artifact(backup, "remove stale committed backup")
                if failure is not None:
                    cleanup_errors.append(failure)
            if any(_exists_no_follow(backup) for backup in manifest.backups):
                continue
            failure = _remove_regular_artifact(
                manifest.path, "remove completed publication manifest"
            )
            if failure is not None:
                cleanup_errors.append(failure)

        unresolved = [
            _lexical_absolute(backup)
            for destination in destination_list
            for backup in _backup_candidates(destination)
            if _lexical_key(backup) not in authorized
        ]
        if unresolved:
            paths = ", ".join(str(path) for path in unresolved)
            raise RuntimeError(f"unresolved publication backup requires recovery: {paths}")
    except Exception as preflight_error:
        raise_with_cleanup(preflight_error, "preflight recovery", cleanup_errors)
    return cleanup_errors


def _backup(path: Path, transaction_id: str) -> Path | None:
    if not path.exists():
        return None
    backup = _backup_path(path, transaction_id)
    temporary = _sibling(backup, ".tmp")
    try:
        with path.open("rb") as source, temporary.open("wb") as target:
            shutil.copyfileobj(source, target)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, backup)
    except Exception as primary_error:
        cleanup = _remove(temporary, "remove backup temporary")
        raise_with_cleanup(
            primary_error,
            f"create backup for {path.resolve()}",
            () if cleanup is None else (cleanup,),
        )
    return backup


def _write_completion_manifest(
    destinations: Iterable[Path],
    backups: Iterable[Path],
    transaction_id: str,
) -> tuple[Path, tuple[str, ...]]:
    destination_list = tuple(_lexical_absolute(path) for path in destinations)
    backup_list = tuple(_lexical_absolute(path) for path in backups)
    manifest = _manifest_path(destination_list, transaction_id)
    contents = json.dumps(
        {
            "version": 1,
            "transaction_id": transaction_id,
            "destinations": [str(path) for path in destination_list],
            "backups": [str(path) for path in backup_list],
        },
        sort_keys=True,
        separators=(",", ":"),
    ) + "\n"
    try:
        staged = stage_text(manifest, contents)
    except PublicationOperationError as error:
        return manifest, (error.primary_failure, *error.cleanup_failures)
    except Exception as error:
        return manifest, (f"write completion manifest {manifest.resolve()}: {error}",)
    try:
        os.replace(staged.temporary, manifest)
    except Exception as primary_error:
        cleanup_failures = discard_staged([staged])
        return manifest, (
            f"publish completion manifest {manifest.resolve()}: {primary_error}",
            *cleanup_failures,
        )
    return manifest, ()


def _publish_failure_details(error: Exception, primary_action: str) -> tuple[str, tuple[str, ...]]:
    if isinstance(error, PublicationOperationError):
        return error.primary_failure, error.cleanup_failures
    return f"{primary_action}: {error}", ()


def _validate_staged_layout(items: list[StagedFile]) -> None:
    if not items:
        raise ValueError("at least one staged destination is required")
    destination_keys = [_resolved_key(item.destination) for item in items]
    if len(destination_keys) != len(set(destination_keys)):
        raise ValueError("staged destinations must be unique after path resolution")
    temporary_keys = [_resolved_key(item.temporary) for item in items]
    if len(temporary_keys) != len(set(temporary_keys)):
        raise ValueError("staged temporaries must be unique after path resolution")
    if set(destination_keys).intersection(temporary_keys):
        raise ValueError("staged temporary must not alias any destination")


def _discard_invalid_staged(items: list[StagedFile]) -> tuple[str, ...]:
    """Discard safe temporaries while retaining any path that aliases an output."""
    protected = {_resolved_key(item.destination) for item in items}
    failures: list[str] = []
    cleaned: set[str] = set()
    for item in items:
        temporary_key = _resolved_key(item.temporary)
        if temporary_key in protected:
            failures.append(
                "retain unsafe staged temporary "
                f"{_lexical_absolute(item.temporary)} because it aliases a destination"
            )
            continue
        if temporary_key in cleaned:
            continue
        cleaned.add(temporary_key)
        failure = _remove(item.temporary, "remove staged temporary")
        if failure is not None:
            failures.append(failure)
    return tuple(failures)


def commit_staged(staged: Iterable[StagedFile]) -> CommitResult:
    """Publish a staged group and preserve actionable recovery diagnostics.

    Transaction backups are auto-cleanable only when one atomic manifest names
    the exact transaction.  The manifest is published after every destination,
    so no rollback path can leave completion evidence beside recovery data.
    """
    items = list(staged)
    destinations = [item.destination for item in items]
    try:
        _validate_staged_layout(items)
        _manifest_parent(destinations)
        cleanup_errors = _preflight(destinations)
    except Exception as validation_error:
        cleanup_failures = _discard_invalid_staged(items)
        raise_with_cleanup(validation_error, "publication validation", cleanup_failures)

    transaction_id = uuid.uuid4().hex
    backups: dict[Path, Path] = {}
    committed: list[StagedFile] = []
    primary_action = "prepare publication"
    try:
        for item in items:
            primary_action = f"create backup for {item.destination.resolve()}"
            backup = _backup(item.destination, transaction_id)
            if backup is not None:
                backups[item.destination] = backup
        for item in items:
            primary_action = f"publish {item.temporary.resolve()} to {item.destination.resolve()}"
            os.replace(item.temporary, item.destination)
            committed.append(item)
    except Exception as publish_error:
        primary_failure, primary_cleanup = _publish_failure_details(publish_error, primary_action)
        rollback_failures: list[str] = []
        destinations_without_backup: list[Path] = []
        committed_destinations = {item.destination for item in committed}
        for item in reversed(committed):
            backup = backups.get(item.destination)
            try:
                if backup is None:
                    item.destination.unlink(missing_ok=True)
                else:
                    os.replace(backup, item.destination)
            except Exception as rollback_error:
                if backup is None or not _exists_no_follow(backup):
                    destinations_without_backup.append(item.destination)
                if backup is None:
                    action = f"remove new destination {item.destination.resolve()} (no backup exists)"
                else:
                    action = f"restore {item.destination.resolve()} from {backup.resolve()}"
                rollback_failures.append(f"{action}: {rollback_error}")
        for destination, backup in backups.items():
            if destination in committed_destinations:
                continue
            failure = _remove_regular_artifact(backup, "remove unused backup")
            if failure is not None:
                rollback_failures.append(failure)
        rollback_failures.extend(discard_staged(items))
        all_cleanup_failures = [*cleanup_errors, *primary_cleanup, *rollback_failures]
        if rollback_failures:
            recovery_backups = [
                backup for backup in backups.values() if _exists_no_follow(backup)
            ]
            raise PublicationRollbackError(
                primary_failure,
                all_cleanup_failures,
                recovery_backups,
                destinations_without_backup,
            ) from publish_error
        if cleanup_errors or primary_cleanup:
            raise PublicationOperationError(
                primary_failure, [*cleanup_errors, *primary_cleanup]
            ) from publish_error
        raise

    cleanup_errors.extend(discard_staged(items))
    if backups:
        manifest, manifest_errors = _write_completion_manifest(
            destinations, backups.values(), transaction_id
        )
        cleanup_errors.extend(manifest_errors)
        if manifest_errors:
            cleanup_errors.extend(
                "completion evidence unavailable; recovery backup retained: "
                f"{_lexical_absolute(backup)}"
                for backup in backups.values()
                if _exists_no_follow(backup)
            )
        else:
            for backup in backups.values():
                failure = _remove_regular_artifact(backup, "remove committed backup")
                if failure is not None:
                    cleanup_errors.append(failure)
            if not any(_exists_no_follow(backup) for backup in backups.values()):
                failure = _remove_regular_artifact(
                    manifest, "remove completed publication manifest"
                )
                if failure is not None:
                    cleanup_errors.append(failure)
    status = "committed-with-cleanup-pending" if cleanup_errors else "committed"
    return CommitResult(status=status, cleanup_errors=tuple(cleanup_errors))


def atomic_write_text(path: Path, contents: str) -> CommitResult:
    return commit_staged([stage_text(path, contents)])
