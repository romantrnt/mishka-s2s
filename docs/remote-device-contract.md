# Remote virtual-device contract

The remote stand uses `HardwareIntegrationStand`, its PTY JSONL/CRC32 transport,
`ScenarioApplication`, canonical runtime, content, local safety and resource
cleanup. `RemoteDeviceSimulator` adds virtual input faults and a bounded observer
journal. It contains no scenario transitions. Real model execution depends on
the selected provider profile and successful runtime warmup; a mock-provider
test never establishes GPU, microphone, speaker or physical-device acceptance.

## Authentication and scope

The API is installed only with `hardware_mode=simulator` or `fake`. All four
routes require a configured operator token, supplied with `X-Stand-Token` or
the existing HttpOnly `stand_token` cookie. Query-string tokens are rejected.
Missing server configuration returns 503; missing or incorrect credentials
return 401. Physical stands expose no simulator routes.

An attached remote simulator accepts peripheral mutations through `/stand/ws`
and the Stand HTTP routes. Its alternate generic `/ws/{session_id}` transport
closes with code 1008 and reason `stand_ingress_required` after authenticating
the operator. Generic canonical dispatch bypasses the virtual input adapter and
cannot be used as another receiver. Standalone applications and physical stands
retain their existing canonical WebSocket behavior. Hangup/reset remain
available through the official Stand receiver during input disconnection.

`local_gpu` / `local_cpu` with Max profiles and simulated devices require explicit
`identity_mode=max_json`, `max_client_source=local_demo`,
`max_synthetic_data=true`, absolute Max bundle/state paths,
`hardware_mode=simulator`, `mutation_auth_policy=token_required`, a configured
token and `allow_query_token=false`. The physical demo's device and fail-closed
hardware checks remain mandatory. `production_ready` remains false for every
synthetic or simulated configuration. `/ready` still requires actual software
lifecycle, graph and history readiness.

The launcher supplies the unchanged Max client and capture callback:

```python
api = create_stand_app(settings, phone_client=phone,
                       simulator_scan=capture.scan)
```

`capture.scan(code, timeout_s)` is the existing asynchronous
`SyntheticCapture.scan` interface. Only `MaxJsonBoundary` owns
`start/poll/patient/clear/close`. The remote API never accepts a UID, patient JSON,
authorization result, profile reference or internal identity event.

## Routes and actions

| Method and path | Contract |
| --- | --- |
| `GET /stand/simulator/state` | Same panel projection as `/stand/state` and `stand_state.state`, with the additional `simulator` object. |
| `GET /stand/simulator/journal?after=0` | At most 300 redacted records, `cursor`, `oldest_cursor`, `gap`, `limit`, `epoch`. A gap means fetch current state. Reset preserves this journal. |
| `POST /stand/simulator/action` | One strict action object; response `{result, epoch, state}`. |
| `POST /stand/simulator/fault` | Controlled virtual input disconnect, reconnect or delay; same response envelope. |

```json
{"action":"handset","state":"up","epoch":0}
{"action":"handset","state":"down"}
{"action":"button","button_id":1,"press":"press","epoch":0}
{"action":"button","button_id":1,"press":"release"}
{"action":"button","button_id":7,"press":"hold","duration_ms":700}
{"action":"button","button_id":7,"press":"long_hold","duration_ms":1500}
{"action":"rfid","card":"demo-1"}
{"action":"reset"}
```

Button IDs are integers 1–7. Hold duration is an integer 0–120000 ms. The card
enum is `demo-1`, `demo-2`, `unknown`, `archive` (Max's `demo-3`). It reaches Max's
capture frame handler, actual HTTPS, SQLite and JSON validation. Max's known
duplicate suppression is preserved and reported as `duplicate_suppressed_by_max`.
No capture callback means 409 `scan_unavailable`; arbitrary profile payloads
are rejected with 422.

Button commands describe edges, not atomic clicks. Send a separate release
after a press; another press with a new sequence while the button is still
down must not select another scene or answer another question. Hold ticks and
release keep their existing meaning, including the short-answer/held-exit
gesture on button 7. Use the release event type: `button` with a payload field
`state: released` is not a release alias. A client that exposes clicks must
translate each click into an ordered press/release pair.

## Physical input → application → output

| Physical or protocol action | Existing backend event | Existing runtime output |
| --- | --- | --- |
| Lift handset; native keyboard Space released | `handset {state:up}` → `HandsetLifted` | Canonical welcome/scenario, readiness wave, owned recorded/PCM audio. |
| Replace handset; native Space pressed | `handset {state:down}` → `HandsetPlaced` | Canonical session cleanup/suspension policy, audio/capture cancellation, profile revocation and lighting policy. Safety-stop hangup ends local context. |
| Native button 1–7 press/release | `button` / `button_released` → `ButtonPressed` / `ButtonReleased` | Real selected scenario/menu/game transition, explicit lighting and owned audio. |
| Supported UART hold/long hold | `button_hold` / `button_long_hold` → `ButtonHeld` / `ButtonLongHeld` | Canonical edge acknowledgement or applicable scenario behavior. The current native keyboard adapter generates press/release, not synthesized hold gestures. |
| Named synthetic RFID card | Max synthetic capture → unchanged Max TLS/SQLite → validated ready JSON → private `ClientProfileReady` | `profile_ready`, actual private context and profile-ready lighting. No generic RFID ingress exists in MISHKA. |
| Virtual input disconnect | `HardwareDisconnected` | Actual local `safety_stop`, audio cleanup and canonical safety lighting. No adult notification is invented. |
| Virtual input reconnect | `HardwareReconnected` | Recovery metadata only; existing SAFE_STOP remains until its canonical cleanup procedure. |

Lighting comes from the actual `FakeLightingOutput` element snapshot, using
the same explicit element IDs as the physical output. `led` is a compatibility
projection. UART commands include actual `audio_play`, `audio_stop`,
`audio_pause`, `audio_resume` and `display`; the journal strips text and paths
unneeded for tracing. State `commands` remains the existing redacted panel
contract. PCM uses the existing browser transport, not a second voice engine.

## State and frontend ownership

`state.simulator` contains:

- `contract`, `epoch`, `journal_cursor`, selected provider `profile`,
  `identity_mode`, `synthetic_profile`, redacted `identity`, `production_ready=false`.
- Actual `session`: stable session ID, lifecycle state, node, scenario,
  canonical state version and profile status. No name, age, UID or card JSON.
- `admission.input_ready` and `admission.connected`.
- `devices.handset.state` and the native `space_level` mapping.
- `devices.buttons`: IDs, canonical color names, enabled/pressed projections
  and supported protocol press kinds. UI housing color may follow the reference;
  backend button 7 remains named `purple`.
- `devices.lighting`: explicit canonical element states.
- `devices.audio`: current canonical audio status, playback lease identity and
  browser generation. `display` is the latest redacted display command.
- `devices.rfid`: named synthetic cards and availability.
- `microphone.eligible`, using the existing Stand capture predicate, and
  `explicit_enable_required=true`. The UI must also obtain browser consent.
- `prompt`: current static recorded phrase ID/transcript only. Dynamic/private
  speech is never copied into observer state or the journal.
- Fault status and bounded delayed-ingress count.

The UI consumes backend affordances and expected inputs. It must not reproduce
the scenario FSM. Hangup/reset must stop browser microphone capture immediately,
then send canonical cleanup. A WebSocket observer disconnect does not simulate
placing the physical handset; reconnect fetches current state and epoch.

## Faults, epochs and stale work

```json
{"device":"input","mode":"delay","delay_ms":2000}
{"device":"input","mode":"disconnect"}
{"device":"input","mode":"reconnect"}
{"device":"input","mode":"delay","delay_ms":0}
```

Delay is limited to 0–2000 ms and at most 32 delayed ingress waiters. No fault
command changes providers, model files, authentication, the host or real devices.
The disconnected barrier also covers existing Stand HTTP/WS/UART input. Hangup
and reset remain available while disconnected or warming up; reset does not
reconnect the input adapter.

The server increments `epoch` before asynchronous hangup, reset, disconnect,
reconnect and shutdown. A supplied old epoch rejects normal actions with 409
`stale_simulator_epoch`; cleanup remains available with an old epoch. Delayed
inputs and inputs waiting on UART locks cannot revive an expired session.

An RFID request is owned independently of its HTTP waiter. Cancelling that
waiter cannot expose a late scan. On epoch change, the Max boundary temporarily
rejects the old scan; after its callback settles, the owner clears the Max
queue before removing that guard. This covers frames queued before entering
Max's own handler as well as HTTPS already in flight. An indeterminate scan
timeout keeps identity scans closed until restart; guest interaction and cleanup
remain available. New scans cannot overlap a pending scan.

Browser `audio_pcm_output` carries `session_id` and `generation` in addition to
`token`. Drop PCM from an obsolete session/browser generation. Existing
`AudioStarted/Completed/Failed` acknowledgements retain their full playback lease
identity; do not manufacture a current lease for an old acknowledgement.

## Verification

`tests/test_remote_simulator.py` drives the actual Stand/PTY/canonical runtime
with mock speech providers: press kinds, lighting/audio commands, repeated
sessions, stale completions, delay/hangup, disconnect/reconnect SAFE_STOP,
authentication, validation, journal bounds/redaction and secure configuration.

`tests/test_remote_simulator_max.py` additionally uses the unchanged external
Max package, actual temporary HTTPS/SQLite and its actual client. It delays the
capture before the Max handler and inside its HTTPS resolve, then exercises
hangup/reset/new lift, a cancelled HTTP waiter and a fresh subsequent card.
Run with `MISHKA_TEST_MAX_BUNDLE=/absolute/path/to/max/source`.
These are model-mock tests; real-provider/audio/UI acceptance is separate.
