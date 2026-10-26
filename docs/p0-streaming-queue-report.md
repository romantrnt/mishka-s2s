# MISHKA S2S first-audio gate and closed-tag streaming follow-up

Date: 2026-10-26. Isolated branch `fix/p0-lifecycle-20261026`,
worktree `/home/server/mishka-fixes`. No live main/device/model/paid service
was used.

## Narrow fixes

- `AudioSynthesisQueue.wait_first_audio()` now waits for first audio **or**
  worker failure. A simultaneous error wins over a ready flag. Pending wait
  tasks are cancelled and joined on success, timeout, error and cancellation.
  The original worker error reaches the turn's existing fallback path without
  waiting the entire gate timeout.
- `StreamingTokenConsumer` signals a fully closed `</say>` block. After the
  existing output safety and asynchronous response validation, the engine
  flushes that block into TTS immediately; it does not wait for the rest of
  the LLM stream. An open/incomplete tag remains atomic and cannot play.
- When a turn is cancelled during LLM/first-audio waiting, its separately
  spawned consumer task is cancelled and joined. A red hangup regression
  exposed an orphan `Event.wait()` task before this cleanup.
- The existing overflow regression now uses three valid closed speech blocks
  and slow TTS, filling its size-one queue. It still asserts
  `tts_queue_overflow` distinct from cancellation. The former single
  unpunctuated response produced one semantic chunk and never exercised
  overflow.

## Evidence

The first-audio failure, simultaneous ready/failure, closed-tag latency and
revised overflow tests were red before the changes. Focused set including
the old failure-drain test, late invalid tag and hangup/no-orphan regression:
**8 passed, 47 deselected in 0.49 s**. The earlier independent-review
lifespan exception cleanup test is included in the final full run.

Final full headless suite: **336 passed, 13 failed, 1 xfailed in 37.67 s**.
Before this group: **328 passed, 16 failed, 1 xfailed**. Exactly three old
failure names disappeared: `test_tts_failure_drains_bounded_queue_without_hanging`,
`test_tts_queue_overflow_is_reported_separately_from_cancellation`, and
`test_completed_say_tag_reaches_playback_before_stream_finishes`.
Four new S2S regressions and one new lifespan regression were added, so the
passed count increased by eight. No new failing name appeared. Both staged
and unstaged `git diff --check` passed.

## Contract questions deliberately unchanged

- `test_real_streaming_chunk_reaches_playback_before_llm_finishes_and_logs_pipeline`
  still expects audio from an **open** `<say>` tag. Current parser requires
  `</say>` before playback to prevent an interrupted provider stream from
  sounding valid. Product must decide that latency/safety boundary; this
  phase did not weaken it.
- The three remaining SentenceChunker assertions disagree with the current
  semantic 2–3 sentence grouping, punctuation set and minimum 16-char
  configuration. Their expected policy needs review; no assertion was
  weakened here.
- Two Silero assertions expect plain text and peak-normalized ±0.92 samples
  while the current engine produces SSML and retains the source amplitude
  until the post-RVC boundary. The existing live-checkout TTS setup patch
  was not copied or changed.

The other seven standing failures are catalogued in
`docs/audit-remediation-status.md` and prior phase reports. No commit,
push, merge, deploy, reset or clean.
