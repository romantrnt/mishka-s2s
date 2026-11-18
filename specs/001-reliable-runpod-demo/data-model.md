# Existing Domain Model and Constraints

These names describe the current domain; they do not prescribe new storage,
wire fields or a schema migration. Existing source/contract definitions stay
authoritative. A task must preserve a compliant path, and fix it only when a
reproduction or integration evidence demonstrates a gap.

| Entity.field | Canonical constraint for this feature | Existing source / relationship |
| --- | --- | --- |
| SessionPolicy.runtime_mode | Current demo is exhibition; clinical is a distinct policy and cannot be silently substituted | Settings in app/foundation/config.py; canonical HandsetPlaced policy in canonical_contract.py |
| SessionPolicy.identity_mode | Current demo is max_json; current config disallows max_json with clinical | config.py and scripts/run_remote_stand.py; profile belongs to session policy |
| ProfileContext.authorization_deadline | Earliest applicable Max client lease/card expiry/ready deadline remains effective; do not restart at dequeue or extend through child input | app/foundation/max_json.py; Max owns client/JSON/certificates/DB and its existing 120s client limit |
| ProfileContext.private_values | Known facts may personalize only while authorized; no raw card/UID/name/diagnosis/private transcript in observer state, journal or evidence | max_json.py, identity.py; docs/remote-device-contract.md; no new child recording |
| EngagementPolicy.wait_seconds | 120 seconds of waiting after real engagement; active playback/owned work suspends waiting; paused story starts waiting after pause announcement completion | app/foundation/engagement_idle.py; tests/test_engagement_fixed_story.py |
| MediaOwner.identity | Playback/PCM/capture must match the current session and existing token/generation/playback lease identity; obsolete callbacks cannot acquire a new owner | owned_speech.py, s2s.py, stand.py, console/console.js; existing lease tuple, no replacement identifier |
| MicrophonePolicy.explicit_enable_required | True; backend eligibility is necessary but never grants browser permission; late tracks are stopped after owner loss | remote_devices.py and console/console.js, phone.js |
| ObserverSnapshot.order | Nonnegative epoch and journal cursor, ordered within the current observer namespace; old epoch or lower cursor cannot replace current state; ambiguous restarted namespace requires reload | docs/remote-device-contract.md, remote_devices.py, console/console.js and phone.js |
| VirtualButton.button_id | Integer 1–7; semantic availability comes from the current backend scene; repeated held input cannot synthesize a fresh press | remote_devices.py, canonical_contract.py and console/phone.js |
| InputFault.delay_ms | Integer 0–2000; at most 32 delayed-ingress waiters; cleanup remains available and reset does not imply input reconnect | remote_devices.py; docs/remote-device-contract.md |

## Relationships and transitions

- A session owns its profile reference, activity and media; guest activity has
  no fabricated profile. Max expiry clears authorized private use independently
  of engagement. In exhibition, normal handset-down closes context; urgent
  safety recovery is not replaced by ordinary reset semantics.
- Activities retain existing scene state, age bands, content IDs and game
  progress. Button meaning is contextual; 7 may be an answer, not an exit.
- Active playback → pause announcement → paused waiting → resume or idle-root
  exit. Guest playback beyond 120 seconds tests activity, not credential renewal.
- Connection loss → stop local media and ordinary queued work → fresh trusted
  snapshot, or explicit reload. Terminal release and cleanup retain admission.
- Reset/hangup/disconnect invalidate owners before asynchronous work settles.
  New input cannot revive a terminated activity through delayed callbacks.

No new database, profile fields, retention policy, client protocol or firmware
mapping is designed here. Broader Max/clinical/hardware changes remain outside
the current task boundary.
