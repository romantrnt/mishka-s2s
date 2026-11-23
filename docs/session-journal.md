# Opt-in session journal

`DIALOGUE_JOURNAL_ENABLED` defaults to `false`. Disabled applications do not construct a journal, generate journal visit keys, retain journal turns, create journal tasks, use journal worker threads, or write journal directories/files/logs. Existing bounded conversation context remains session-owned RAM for dialogue operation; this flag does not disable the live conversation itself.

The feature is disabled in the shipped configuration. Enable it only through an explicitly authorized operational configuration. `DIALOGUE_JOURNAL_DIR` selects a dedicated private directory; its default is `runtime/private/session-journal`. Nothing in this integration enables an environment variable or writes to an actual deployment store.

## What is recorded

Only canonically admitted, safety-validated `bounded_dialogue_turn` input and the completed validated `TurnResult.chunks` are eligible. One user entry is written before provider execution; successful response chunks form one assistant entry. A provider failure can therefore leave a truthful user-only turn. Rejected/untrusted STT, onboarding/profile/name capture, menu narration, generated stories, schedules and raw model output are outside this journal. Records are never forwarded to the event publisher, ordinary application logs, history repository or shared TTS cache.

The application assigns a fresh random UUID to a visit when its first eligible dialogue turn arrives. It does not use the reusable stand/session ID, name, UID or profile reference as the storage identity. `SessionJournal` hashes this visit key using its own random runtime key. Each stored visit has one `.jsonl` turn log and one `.summary.json` file after completion. Summary content consists of bounded verbatim excerpts, with whitespace normalized and ellipses for truncation; it contains no generated or clinical interpretation.

## Ownership and completion

A canonically accepted new-session, hangup, End/cancel, reset, anonymous restart, hardware disconnect, interaction-idle expiry, identity cleanup or exhibition reset revokes the old visit mapping synchronously. The explicit application reset endpoint also ends the visit. Routine provider/resource cleanup does not.

Disk operations run through `asyncio.to_thread` only when enabled. A visit lock orders its writes and summary; cancellation drains an already submitted write before releasing that lock. Finish work is tracked independently of the cancelled dialogue task. A late provider result must pass the existing session/revision/epoch guards and retain the exact visit owner; it cannot create a new visit or append to a subsequent visit that reuses the same session ID.

Async shutdown revokes mappings before its first external await, drains journal finish work and closes the journal. The synchronous `cancel_identity_work()` path schedules the same owned close when an event loop is available and closes directly when no loop is running. Disabling the feature is checked before any of these storage paths.

## Storage limits and failures

- At most **32 stored visits per journal runtime**, including finished visits. Further journal writes return `False` until a new journal instance starts. This is an intentional per-instance limit, not a retention policy or global disk limit: restarting creates new private names and existing files remain.
- At most **128 turns per visit**, **2,000 characters per turn**, roles `user` and `assistant` only.
- At most six summary excerpts, each at most 160 characters plus a truncation marker.
- Private directories use `0700`; files use `0600`. Symlink/FIFO/nonprivate storage paths are rejected. Summary replacement is atomic; partial append errors are rolled back where the filesystem permits it. If rollback also fails, the visit is poisoned: later append/finish is rejected and no successful summary is produced.
- Storage failure is isolated from dialogue. Journal methods return `False`; they do not print text, filenames, credentials or provider exception bodies. Application provider-error warnings contain an exception type only.

No retention daemon, export endpoint, automatic deletion, clinical summary, consent workflow or production enablement is introduced here. An operator must define any approved retention/export process separately before operational use. A hung filesystem operation may delay journal draining at shutdown; this implementation does not claim a storage-service deadline.

## Local evidence

Tests use synthetic text and isolated pytest temporary directories only. `tests/test_session_journal.py` covers the storage boundary; `tests/test_session_journal_integration.py` covers application configuration, disabled operation, admitted dialogue, reusable session IDs, in-flight provider/hangup races, cancellation ordering, accepted/rejected boundaries, provider error redaction, resource cleanup and both shutdown paths. These tests do not establish consent, operational retention or live deployment acceptance.
