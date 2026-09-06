"""Durable staging and recoverable multi-file publication helpers."""

from dataclasses import dataclass
import os
from pathlib import Path
import shutil
from typing import Iterable


@dataclass(frozen=True, slots=True)
class StagedFile:
    destination: Path
    temporary: Path


class PublicationRollbackError(RuntimeError):
    """A publication failed and at least one backup could not be restored."""


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


def discard_staged(staged: Iterable[StagedFile]) -> None:
    for item in staged:
        item.temporary.unlink(missing_ok=True)


def _backup(path: Path) -> Path | None:
    if not path.exists():
        return None
    backup = _sibling(path, ".bak")
    if backup.exists():
        raise RuntimeError(f"stale publication backup requires recovery: {backup}")
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


def commit_staged(staged: Iterable[StagedFile]) -> None:
    """Publish a staged group, restoring old destinations if any replace fails.

    Existing destinations are first copied to fsynced sibling `.bak` files.  A
    normal exception restores every already-replaced destination and removes all
    transaction artifacts.  If restoration itself fails, the surviving `.bak`
    is deliberately retained and named in `PublicationRollbackError`, providing
    an explicit recovery copy instead of silently discarding the last good data.
    """
    items = list(staged)
    destinations = [item.destination for item in items]
    if len(set(destinations)) != len(destinations):
        discard_staged(items)
        raise ValueError("staged destinations must be unique")

    backups: dict[Path, Path] = {}
    committed: list[StagedFile] = []
    try:
        for item in items:
            backup = _backup(item.destination)
            if backup is not None:
                backups[item.destination] = backup
        for item in items:
            os.replace(item.temporary, item.destination)
            committed.append(item)
    except Exception as publish_error:
        rollback_errors: list[str] = []
        committed_destinations = {item.destination for item in committed}
        for item in reversed(committed):
            backup = backups.get(item.destination)
            try:
                if backup is None:
                    item.destination.unlink(missing_ok=True)
                else:
                    os.replace(backup, item.destination)
            except Exception as rollback_error:
                rollback_errors.append(f"{item.destination}: {rollback_error}")
        for destination, backup in backups.items():
            if destination not in committed_destinations:
                backup.unlink(missing_ok=True)
        discard_staged(items)
        if rollback_errors:
            detail = "; ".join(rollback_errors)
            raise PublicationRollbackError(f"publication rollback incomplete; recover retained .bak file(s): {detail}") from publish_error
        raise
    else:
        for backup in backups.values():
            backup.unlink(missing_ok=True)
    finally:
        discard_staged(items)


def atomic_write_text(path: Path, contents: str) -> None:
    commit_staged([stage_text(path, contents)])
