# Native lighting incident: evidence and bounded mitigation

Base: `f17a7637487dd481b10a4a0e086ea3167c7fce14`. This change is local software only; no firmware flash, Max change, deployment, or physical acceptance is implied.

## Established observations

The read-only live diagnostic supplied by the incident coordinator showed one failed serial batch, no pending/in-flight batch, no overflow, a failed output generation, and an unfinished-line recovery fence. Desired targets were dark while the user still observed a lit button. The event loop and HID reader were alive. These facts show a completed write failure and a latched degraded output, not a blocked Python writer.

The live record lacks the first error's syscall cause, time and output generation. The serial failure may have occurred during later authorization-expiry dark, rather than causing the earlier game/input stall. Kernel key levels still reported button 3 held and SPACE released after the user reported releasing/placing the handset. Historical firmware source is not proof of installed firmware or of a particular USB/MCU fault. The root cause of those missing input edges remains unproven.

## Reproduction

`tests/test_astra_native_lighting_incident.py` uses the production `PosixSerialTransport`, real pseudo-tty I/O, and a persistent line parser. It covers native EIO/timeout at zero or partial write boundaries, actual PTY disconnection/replacement, and owner replacement during an in-flight write. It reproduces the desired-dark/device-still-lit mismatch and verifies explicit dark reconciliation. These are host/native transport seam tests, not optical hardware tests.

## Mitigation

Physical stand construction explicitly enables one automatic dark-only compensation for the physical output lifetime. The same single writer attempts the existing framing fence followed by all 18 dark targets, with a one-second admission budget between lines. One already admitted native line can finish after that budget. There is no periodic loop, unbounded queue, replay of lit targets, or application callback that revives game/audio/profile state.

A successful compensation is only a completed **host write**. The failed-generation latch remains set and ordinary hardware admission remains closed. Only creating a fresh physical output/stand owner through the existing outer service lifecycle can reconcile that latch; ordinary scenario output generations cannot. New-generation and close guards supersede the old compensation. Persistent transport failure remains visible and does not claim achieved dark. This cannot repair a stalled MCU, force a broken USB device to receive data, or replace a physical verification.

Fixed-shape diagnostics preserve original/root exception codes, numeric errno, monotonic failure time, stage, output generation and compensation result. Exception messages are not copied. The first failure evidence survives a successful reopen. No new credentials or access route are introduced. Firmware acknowledgement remains false.

Standalone `PhysicalLightingOutput` keeps its previous opt-in behavior by default so low-level manual-reconciliation consumers/tests retain their contract; the production physical stand explicitly opts in.

## Follow-up: physical fault ownership boundary

Independent follow-up proved two gaps in the first mitigation: an already-issued capture guard remained valid after native serial failure, and a genuine sequence `GameTimer` could call ordinary effect invalidation, clear the failed generation and relight output. The original narrower checkpoint must not be described as holding admission closed until lifecycle recovery.

The strengthened physical owner now has a sticky fault latch which ordinary scenario generation changes cannot clear. A current-output callback sets the stand's capture/playback fence on the serial writer before scheduling asynchronous cleanup. The owner loop cancels game/menu/welcome/render work and uses the existing generic `HardwareDisconnected` transition with source `physical_lighting_loss`. It does not assert a HID-reader failure or generate `HandsetPlaced`, and preserves an existing urgent incident. Native `SoundDevicePlaybackController` checks the hardware fence before stream start and each new write, including generic bounded-dialogue playback; one already-dispatched native write can still finish. Cleanup device stops are bounded and do not claim physical completion on timeout.

The automatic dark-only compensation runs at most once for the physical output lifetime and never starts during close. Repeated explicit dark generations remain permitted but do not create a new automatic-compensation budget. Failed lit frames are rejected even if a later timer tries to invalidate the generation. A real hangup may update canonical handset state while the fault stays latched; failure to display its SAFE_STOP is reported as unavailable output, not recategorized as HID failure. Startup faults without an owner loop remain closed. A terminal close failure does not schedule new application cleanup.

**Recovery boundary:** fresh buttons, lift/hangup, ordinary `clear_all`/`reset_dark`, `invalidate_pending`, serial reopen and `start_hardware` on the same failed stand do not clear this sticky owner. This follow-up adds no recovery API. Recovery requires the existing outer service/stand recreation lifecycle, creating a fresh output and stand owner after the physical connection is restored; it does not resume the old session. A sustained USB/MCU stall may still require authorized device recovery, followed by readiness and physical verification. No software host-write result is firmware/optical acknowledgement.

The two captured device stops are attempted independently under the same one-second cleanup wait budget. A failure or stall in playback stop cannot suppress capture stop, or vice versa. Cleanup rechecks the captured fault owner before each resource admission and before late bookkeeping; no replacement resource is looked up after an await. Late completion remains fenced and does not claim that the device stopped at the timeout.
