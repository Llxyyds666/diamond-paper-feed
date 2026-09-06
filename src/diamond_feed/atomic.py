"""Durable staging and recoverable multi-file publication helpers."""

from dataclasses import dataclass
import os
from pathlib import Path
import shutil
from typing import Iterable, Literal
import uuid


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
        self.recovery_backups = tuple(path.resolve() for path in recovery_backups if path.exists())
        self.destinations_without_backup = tuple(path.resolve() for path in destinations_without_backup)
        recoverable = ", ".join(str(path) for path in self.recovery_backups) or "none"
        no_backup = ", ".join(str(path) for path in self.destinations_without_backup) or "none"
        failures = "; ".join(self.rollback_failures)
        super().__init__(
            f"publication rollback incomplete; primary failure: {primary_failure}; "
            f"rollback/cleanup failures: {failures}; recoverable backups: {recoverable}; "
            f"destinations with no backup: {no_backup}"
        )


def _sibling(path: Path, suffix: str) -> Path:
    return path.with_name(f"{path.name}{suffix}")


def stage_text(path: Path, contents: str) -> StagedFile:
    """Write and fsync text to the destination's sibling `.tmp` without publishing it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = _sibling(path, ".tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(contents)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return StagedFile(destination=path, temporary=temporary)


def discard_staged(staged: Iterable[StagedFile]) -> tuple[str, ...]:
    failures: list[str] = []
    for item in staged:
        try:
            item.temporary.unlink(missing_ok=True)
        except Exception as error:
            failures.append(f"remove staged temporary {item.temporary.resolve()}: {error}")
    return tuple(failures)


def _backup_marker(backup: Path) -> Path:
    return _sibling(backup, ".committed")


def _backup_candidates(destination: Path) -> list[Path]:
    prefix = f"{destination.name}."
    return sorted(
        path
        for path in destination.parent.iterdir()
        if path.name.startswith(prefix) and path.name.endswith(".bak")
    )


def _marker_candidates(destination: Path) -> list[Path]:
    prefix = f"{destination.name}."
    return sorted(
        path
        for path in destination.parent.iterdir()
        if path.name.startswith(prefix) and path.name.endswith(".bak.committed")
    )


def _cleanup_stale_committed(destination: Path) -> list[str]:
    """Best-effort cleanup of backups known to belong to completed commits."""
    cleanup_errors: list[str] = []
    for backup in _backup_candidates(destination):
        marker = _backup_marker(backup)
        if not marker.exists():
            continue
        try:
            backup.unlink(missing_ok=True)
        except Exception as error:
            cleanup_errors.append(f"remove stale committed backup {backup.resolve()}: {error}")
            continue
        try:
            marker.unlink(missing_ok=True)
        except Exception as error:
            cleanup_errors.append(f"remove stale commit marker {marker.resolve()}: {error}")
    for marker in _marker_candidates(destination):
        backup = marker.with_name(marker.name.removesuffix(".committed"))
        if backup.exists():
            continue
        try:
            marker.unlink(missing_ok=True)
        except Exception as error:
            cleanup_errors.append(f"remove orphaned commit marker {marker.resolve()}: {error}")
    return cleanup_errors


def _reject_unresolved_backups(destination: Path) -> None:
    unresolved = [path.resolve() for path in _backup_candidates(destination) if not _backup_marker(path).exists()]
    if unresolved:
        paths = ", ".join(str(path) for path in unresolved)
        raise RuntimeError(f"unresolved publication backup requires recovery: {paths}")


def _backup(path: Path, transaction_id: str) -> Path | None:
    if not path.exists():
        return None
    backup = _sibling(path, f".{transaction_id}.bak")
    temporary = _sibling(backup, ".tmp")
    try:
        with path.open("rb") as source, temporary.open("wb") as target:
            shutil.copyfileobj(source, target)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, backup)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return backup


def _mark_backup_committed(backup: Path) -> None:
    marker = _backup_marker(backup)
    staged = stage_text(marker, "committed\n")
    try:
        os.replace(staged.temporary, marker)
    except Exception:
        discard_staged([staged])
        raise


def commit_staged(staged: Iterable[StagedFile]) -> CommitResult:
    """Publish a staged group and preserve actionable recovery diagnostics.

    Backups use transaction-specific names.  A `.committed` companion marks a
    leftover backup as redundant after a successful commit, so a later run can
    remove it safely and continue.  Unmarked backups are never auto-deleted:
    they represent an incomplete rollback and retain the last recoverable data.
    """
    items = list(staged)
    destinations = [item.destination for item in items]
    if len(set(destinations)) != len(destinations):
        discard_staged(items)
        raise ValueError("staged destinations must be unique")

    cleanup_errors: list[str] = []
    try:
        for destination in destinations:
            cleanup_errors.extend(_cleanup_stale_committed(destination))
            _reject_unresolved_backups(destination)
    except Exception as preflight_error:
        cleanup_failures = discard_staged(items)
        if cleanup_failures:
            recovery_backups = [
                backup
                for destination in destinations
                for backup in _backup_candidates(destination)
                if not _backup_marker(backup).exists()
            ]
            raise PublicationRollbackError(
                f"preflight recovery check: {preflight_error}",
                cleanup_failures,
                recovery_backups,
                (),
            ) from preflight_error
        raise

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
        for backup in backups.values():
            primary_action = f"mark committed backup {backup.resolve()}"
            _mark_backup_committed(backup)
    except Exception as publish_error:
        primary_failure = f"{primary_action}: {publish_error}"
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
                if backup is None or not backup.exists():
                    destinations_without_backup.append(item.destination)
                if backup is None:
                    action = f"remove new destination {item.destination.resolve()} (no backup exists)"
                else:
                    action = f"restore {item.destination.resolve()} from {backup.resolve()}"
                rollback_failures.append(f"{action}: {rollback_error}")
        for destination, backup in backups.items():
            if destination in committed_destinations:
                continue
            try:
                backup.unlink(missing_ok=True)
            except Exception as cleanup_error:
                rollback_failures.append(f"remove unused backup {backup.resolve()}: {cleanup_error}")
        for backup in backups.values():
            marker = _backup_marker(backup)
            try:
                marker.unlink(missing_ok=True)
            except Exception as cleanup_error:
                rollback_failures.append(f"remove rollback commit marker {marker.resolve()}: {cleanup_error}")
        rollback_failures.extend(discard_staged(items))
        if rollback_failures:
            recovery_backups = [backup for backup in backups.values() if backup.exists()]
            raise PublicationRollbackError(
                primary_failure,
                [*cleanup_errors, *rollback_failures],
                recovery_backups,
                destinations_without_backup,
            ) from publish_error
        raise

    cleanup_errors.extend(discard_staged(items))
    for backup in backups.values():
        marker = _backup_marker(backup)
        try:
            backup.unlink(missing_ok=True)
        except Exception as cleanup_error:
            cleanup_errors.append(f"remove committed backup {backup.resolve()}: {cleanup_error}")
            continue
        try:
            marker.unlink(missing_ok=True)
        except Exception as cleanup_error:
            cleanup_errors.append(f"remove committed marker {marker.resolve()}: {cleanup_error}")
    status = "committed-with-cleanup-pending" if cleanup_errors else "committed"
    return CommitResult(status=status, cleanup_errors=tuple(cleanup_errors))


def atomic_write_text(path: Path, contents: str) -> CommitResult:
    return commit_staged([stage_text(path, contents)])
