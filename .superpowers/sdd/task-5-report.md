# Task 5 report: atomic collection state and raw RSS

## RED / GREEN

- RED: added `tests/test_state_collect_render.py` before implementation and ran `python -m pytest tests/test_state_collect_render.py -q`. It failed as expected with missing `diamond_feed.state`, `diamond_feed.collect`, and `diamond_feed.render` imports (9 failures).
- GREEN: implemented versioned atomic JSON state, rules-aware merge and pending-AI queue handling, RSS 2.0/DC rendering, and an offline-testable collection CLI. The focused run is green: `9 passed`.

## Verification

- Focused: `python -m pytest tests/test_state_collect_render.py -q` — `9 passed in 0.49s`.
- Full suite: `python -m pytest -q` — `75 passed in 0.60s`.
- Compile check: `python -m compileall -q src`.
- Diff check: `git diff --check` after staging.

## Coverage and self-review

- State writes use a sibling `.tmp`, `flush`, `os.fsync`, and `os.replace`; failed writes clean up the temporary file. Loading rejects invalid, malformed, or unsupported schema versions.
- Merging filters every input, combines duplicate provenance, keeps the pending queue oldest-first, and only requeues an AI-processed record when a previously empty abstract becomes non-empty.
- RSS generation uses `ElementTree`, has DC metadata and DOI identifiers, escapes special XML content, sorts newest-first, and hard-caps output at 2,000 items.
- CLI tests use only temporary config/source files and faked fetchers/time. They cover adapter isolation, partial success persistence, failure TSV output, persisted watermarks, bootstrap/watermark dates, and all-failure preservation of prior state/feed with exit code 2.
- No RSS registry, credentials, or key-like strings were added. No network operation occurs in the tests.

## Commit

- `cfa7a55e588e36aaa7765926eb7da23ddb4bb7cc` — `feat: persist collection state and raw feed`

## Review repair: strict validation and transactional publication

- RED: added regression cases for the empty default registry, exact state/paper schemas, strict JSON types, canonical keys, pending queue invariants, timezone-aware timestamps, save-time validation, XML 1.0 forbidden characters, state staging failure, and both state/feed commit failures. The focused run failed in the expected 17 cases (`17 failed, 8 passed`).
- GREEN: introduced a shared staging/atomic publication helper, strict schema-v1 validation and UTC watermark normalization, staged state/feed publication with fsynced backups and rollback, XML 1.0 text sanitization, and a header-only default RSS registry.
- A second RED/GREEN cycle verified invalid in-memory DOI types produce a validation error before key calculation or disk writes.
- Focused after repair: `python -m pytest tests/test_state_collect_render.py -q` — `26 passed`.
- Full suite after repair: `python -m pytest -q` — `92 passed`.
- Recovery strategy: existing outputs are copied to fsynced sibling `.bak` files before the state-then-feed commit. Ordinary replace failures restore both old outputs and remove `.tmp`/`.bak` artifacts. If rollback itself fails, the surviving `.bak` is retained and its path is reported through `PublicationRollbackError` for explicit recovery.
- Repair commit: `6a75ea9c5d8103bf0a91d0a55ca716c4b3619542` — `fix: harden state and feed publication`.

## Review repair round 2: recovery diagnostics takeover

- Takeover: continued from the existing uncommitted `atomic.py`, `state.py`, and focused-test changes without resetting or rewriting them. The inherited focused run was `47 passed, 1 failed`; the remaining failure showed that a post-commit cleanup warning could be promoted to an exception even though both destinations already contained the new data.
- RED: changed the public outcome assertion to require an explicit `committed-with-cleanup-pending` result that remains successful under an all-warnings-as-errors filter. The focused run then failed in the expected three cleanup-result cases (`3 failed, 45 passed`). Separate RED cases also demonstrated that a rollback error omitted an earlier stale-completed cleanup failure and did not identify a destination whose expected backup disappeared during restoration.
- GREEN: successful publication now returns `CommitResult` with either `committed` or `committed-with-cleanup-pending`, plus every cleanup error, and emits no warning that can turn success into failure. Transaction-specific backups carry durable completion markers, so a later run safely retries cleanup of completed leftovers while unmarked recovery backups still block publication.
- Recovery diagnostics: `PublicationRollbackError` preserves the primary publish failure, aggregates restore, unused-backup, marker, staged-temporary, and prior stale-completed cleanup failures, lists only backup paths that actually still exist, and separately names destinations with no available backup.
- State validation: load and save reject unpaired Unicode surrogates in every persisted paper string, list item, paper/pending key, watermark key, and watermark value. Oversized integer confidence values are normalized to validation errors and `load_state` wraps them through its standard invalid-state `ValueError`.
- Focused final: `python -m pytest tests/test_state_collect_render.py -q` — `50 passed in 0.93s`.
- Full final: `python -m pytest -q` — `116 passed in 1.11s`.
- Static verification: `python -m compileall -q src` and `git diff --check` both completed successfully with no error output.
- Fault tests use only temporary local files and monkeypatched filesystem primitives; no network access occurs.
- Review-repair commit: `1fc5a98af6b88c633ef13f62d02b87a593044782` — `fix: preserve atomic recovery diagnostics`.

## Review repair round 3: transaction-safe completion evidence

- RED: the inherited `50 passed` focused baseline was extended with compound fault cases for transaction evidence ordering and exact matching, legacy partial markers after failed rollback, stage/backup primary-plus-cleanup failures, manifest publish-plus-temp-cleanup failure, preflight cleanup debt followed by publish failure and successful rollback, `save_state` result propagation, and CLI stderr visibility. The focused run failed in the expected nine cases (`9 failed, 50 passed`).
- GREEN: replaced per-backup committed markers with one atomic transaction manifest created only after every destination replacement succeeds. The manifest strictly records the transaction ID plus exact absolute destination and backup paths; preflight validates the whole manifest and actual transaction backup set before authorizing any cleanup.
- Recovery safety: legacy per-backup markers, missing/invalid manifests, and incomplete manifest backup sets never authorize deletion. If manifest creation fails after the commit point, new destination data remains committed, every unproven backup is retained as recovery debt, and the next publication blocks rather than deleting it. New rollback paths occur before completion evidence exists.
- Error preservation: `stage_text`, backup preparation, and completion-manifest publication preserve their primary I/O failure and structurally collect temporary-file cleanup failures. A prior stale-cleanup error remains attached through a later publish failure even when data rollback succeeds; incomplete rollback still aggregates every cleanup/restore failure and actual recovery path.
- Production visibility: `save_state` now returns `CommitResult`. Collection observes results from both failure-log publication and state/feed publication; cleanup-pending status and concrete artifact paths are written to stderr without changing a successful exit code.
- Focused: `python -m pytest tests/test_state_collect_render.py -q` — `59 passed in 1.68s`.
- Full suite: `python -m pytest -q` — `125 passed in 1.58s`.
- Static verification: `python -m compileall -q src` and `git diff --check` completed successfully.
- All fault tests use temporary local paths and monkeypatched filesystem operations; no network access occurs.
- Review-repair commit: `6694c431e0c33bbc2869122510aa076f1c6ecf9d` — `fix: make publication recovery transaction-safe`.

## Review repair round 4: complete cleanup-failure aggregation

- RED: added compound tests for collection's second-stage failure plus earlier staged-temp cleanup failure, duplicate-destination validation cleanup, manifest-layout validation cleanup, authorized preflight cleanup followed by an unresolved backup plus staged cleanup, and orphan manifest retry. The first focused run failed in all five expected cases (`5 failed, 59 passed`). A follow-up scope test then failed (`1 failed, 63 passed`) because an unrelated manifest destination was still inspected before relevance was established.
- GREEN: introduced one `raise_with_cleanup` protocol that preserves an existing `PublicationOperationError` primary/cleanup pair and appends every later cleanup failure. Collection multi-file staging, all `commit_staged` entry validation, and preflight now use that protocol.
- Validation cleanup: empty/duplicate destination and manifest parent/root/layout failures share the same guarded cleanup path. Every passed staged temporary is attempted; cleanup success re-raises the original validation error, while cleanup failure yields a structured operation error naming both primary and remaining artifact.
- Preflight aggregation: cleanup of authorized committed artifacts and unresolved-backup detection now run in one guarded operation. Any later preflight error carries all earlier cleanup debt, and outer staged cleanup is merged rather than replacing it.
- Manifest convergence and scope: preflight enumerates only controlled `.diamond-feed-publication.*.json` names in the relevant manifest directory. A strict, related orphan manifest can be removed even after all backups are gone; invalid and unrelated manifests remain untouched, and unrelated destinations are rejected before filesystem inspection.
- Cleanup audit: every production `discard_staged(...)` return is now consumed by `raise_with_cleanup`, manifest debt, rollback diagnostics, or `CommitResult.cleanup_errors`; no result is silently discarded.
- Focused: `python -m pytest tests/test_state_collect_render.py -q` — `64 passed in 1.25s`.
- Full suite: `python -m pytest -q` — `130 passed in 1.49s`.
- Static verification: `python -m compileall -q src` and `git diff --check` completed successfully.
- Tests remain local and offline, using temporary paths and narrowly scoped filesystem fault injection.
- Review-repair commit: `f3562bb086e15c75d99742997b0d436b8a7a13e3` — `fix: aggregate publication cleanup failures`.

## Review repair round 5: publication path topology and no-follow cleanup

- RED: added seven CLI layout cases covering both `x`/`x.tmp` orderings, resolved relative aliases, failures/state and failures/feed aliases, and failures/fixed-temporary collisions; added three `commit_staged` destination/temporary topology cases; and added three manifest/backup safety cases for symlinks and parent traversal. The isolated run failed in all expected cases (`13 failed, 64 deselected in 1.02s`). One real symlink case reproduced deletion of an external sentinel through a manifest path.
- GREEN: added reusable pre-staging output-layout validation based on resolved, `normcase` path identity. Collection validates state, feed, failures, and every fixed sibling `.tmp` before configuration/state loading, source dispatch, or output writes. Invalid layouts therefore preserve all prior outputs and cannot invoke adapters.
- Defense in depth: `commit_staged` independently rejects resolved/normcase duplicate destinations, duplicate temporaries, and destination/temporary intersections. Validation uses one guarded cleanup path, deduplicates cleanup attempts, preserves destinations involved in unsafe intersections, and structurally aggregates retained-artifact or unlink failures.
- Cleanup boundary: completion manifests and backups now use canonical lexical absolute paths plus `normcase` comparison rather than resolved targets. Manifest entries must be absolute and canonical, cannot contain parent traversal, and must exactly match the controlled manifest path, destination-derived backup names, and transaction ID.
- No-follow behavior: manifest reads require a regular `lstat` entry and re-check the opened descriptor; symlink or non-regular manifest/backup entries never authorize cleanup. Manifest/backup removal validates with `lstat` and unlinks only the lexical directory entry, so an external symlink target is never deleted. Invalid or unrelated evidence is retained, while related unauthorized backups block publication with structured recovery diagnostics and without out-of-scope scanning.
- GREEN focused regression selection: `python -m pytest tests/test_state_collect_render.py -q -k "unsafe_output_layout or resolved_destination_alias or temporary_topology or symlink_manifest or symlink_backup or parent_traversal"` — `13 passed, 64 deselected in 0.43s`.
- Task 5 focused: `python -m pytest tests/test_state_collect_render.py -q` — `77 passed in 1.73s`.
- Full suite: `python -m pytest -q` — `143 passed in 1.81s`.
- Static verification: `python -m compileall -q src` and `git diff --check` both exited successfully with no validation errors.
- All added tests are local and offline. Symlink tests use real temporary links when available and explicitly mock `lstat`/path reads when Windows link creation is unavailable; no credentials or key-like strings were introduced.

## Review repair round 6: linked-ancestor publication boundary

- Takeover: preserved the inherited uncommitted regression tests and reproduced the reported RED selection. `python -m pytest tests/test_state_collect_render.py -q -k "real_linked or windows_reparse_ancestor"` failed in all four expected cases (`4 failed, 77 deselected`); all three real Windows directory-symlink cases executed without a skip. A direct `stage_text` linked-parent sentinel case was then added and failed independently (`1 failed, 81 deselected`).
- GREEN: added a reusable lexical containment and no-follow guard that walks from the filesystem anchor to the requested path with component-by-component `lstat`. It rejects symbolic links, Windows reparse points, and non-directory intermediate components without resolving through them; a missing suffix is allowed for creation, while newly created parents are checked again before use.
- Publication boundary: collection validates destinations and fixed temporaries before configuration/state/source reads or adapter dispatch. Staging, destination publication, rollback, backup creation and inspection, manifest-directory enumeration and reading, artifact removal, and manifest staging/replacement all revalidate their lexical ancestors immediately before filesystem operations.
- Windows and cleanup safety: link detection combines `stat.S_ISLNK` with `FILE_ATTRIBUTE_REPARSE_POINT` through `st_file_attributes` while remaining portable when the attribute is absent. Cleanup operates on lexical entries only; linked ancestors are never traversed, leaf manifest/backup links remain unauthorized, and every external sentinel stayed unchanged.
- GREEN regression selection: `python -m pytest tests/test_state_collect_render.py -q -k "real_linked or windows_reparse_ancestor"` — `5 passed, 77 deselected in 0.52s`.
- Task 5 focused: `python -m pytest tests/test_state_collect_render.py -q` — `82 passed in 2.11s`.
- Full suite: `python -m pytest -q` — `148 passed in 2.24s`.
- Static verification: `python -m compileall -q src` and `git diff --check` both exited successfully with no validation errors.
- All tests are local and offline; no credentials or key-like strings were introduced.
