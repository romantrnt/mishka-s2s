# Live f17a763 lighting incident, 2026-11-15

## Evidence retained before intervention

The running Python process remained alive. LED output reported one incomplete/unconfirmed serial line, a failed output generation, an empty queue and no in-flight write or overflow. Desired targets were all zero; this was not evidence of optical darkness. The original diagnostics did not retain the underlying errno or failure time, so the primary transport failure and its ordering against the game's freeze cannot be reconstructed.

The last accepted canonical button event was sequence 14. Later game/audio callbacks completed and `ClientProfileCleared` closed the session. This was not a received hangup. A non-consuming kernel key-level query reported button 3 pressed and SPACE released. These are last observed Linux HID levels, not proof of what the user was physically holding. Canonical trace does not establish absence of raw input: readiness admission can reject ordinary controls before they reach that trace. USB nodes remained present and there was no new kernel disconnect entry. No restart, device write or firmware change was performed during evidence capture.

The historical sketch consumes serial commands before checking buttons and prints acknowledgements. It is not verified as the installed firmware. MCU/USB backpressure, I2C stall and underlying transport failure remain hypotheses, not established root causes.

## Independently reproduced software defects

- Actual private PCM completion entered `LISTENING`, replacing the entire frame with legacy flat channel 14, now the fill of button 1. The agreed correction uses the existing dialogue listening design: seven breathing fills with the bear and button-7 digit as fixed anchors. It does not invent a new design or permit late dialogue callbacks to own another scene.
- The diagnostic projection discarded HID counters and last observed levels. It now exposes fixed, bounded fields distinguishing received supported controls from normalized event count, without paths, profile or speech contents. This is a best-effort non-atomic observation, not an event acknowledgement. The initial-level diagnostic also mixed Linux key codes with logical button IDs; all seven mappings are now covered.
- Native POSIX/PTY fault tests reproduce a last-lit device with desired dark and a failed output generation. A single dark-only recovery is not a firmware acknowledgement and cannot guarantee darkness on an unavailable link.
- A native serial failure in a real application dialogue path did not revoke an already admitted physical capture owner. This synthetic red proof does not establish that speech was processed during the reported live game incident. The correction must fence capture/playback and old automatic producers before being called ready.

## Acceptance boundary

No fix can honestly establish the original USB/MCU cause from evidence that was not recorded. A controlled repeat with the new sanitized diagnostics is required. No automatic recovery may replay old lit targets, revive a prior conversation/game, synthesize a handset edge or erase an urgent incident. Source changes are isolated from main, Max, model configuration and the live installation until separately approved.
