# Operator screen

Open `/console/operator` from the **Оператор** link in the phone panel. It uses the existing private stand authentication. HTML, assets and `/stand/operator` fail closed if no stand token is configured. The remote gateway keeps its existing strict cookie/origin rules; no token is placed in a URL or browser storage.

The page polls the read-only `/stand/operator` endpoint once per second while visible. It never connects to the phone WebSocket and cannot take microphone, playback or control ownership. Diagnostics remain available while the main runtime is warming or degraded.

The timeline contains accepted canonical events, state changes, component health and fixed component-level errors. It is not a tail of process log files, a transcript, raw UART or provider text. Only fixed enums and bounded numeric metadata enter the projection; names, RFID UID, profile/session IDs, paths, exception messages, conversation content and secrets are omitted. Existing optional conversation journaling remains disabled by default and is not enabled by this screen.

At most 240 observations live in process memory. Cursors stay monotonic across session resets; a process restart creates a fresh stream. The UI identifies an evicted cursor or restarted stream and refreshes the bounded history. There is no disk persistence or cross-process history. Reloading the page can recover retained observations, including a signal raised before the page opened.

Active alerts reflect only the current canonical `safety_stop` with `help_requested` or `urgent_safety`. A synchronous, in-memory observer runs after transition authorization and before audio cleanup awaits, so an urgent signal does not wait for old playback to drain. Leaving the safety state or resetting/ending the session ends the current signal; history explicitly states that adult assistance is unconfirmed. There is no delivery, acknowledgement or help-completed action. On a lost connection, the last signal is marked stale rather than resolved.

Checks: `python -m pytest -q tests/test_operator_console.py` and `node tests/operator_console_regression.js`. Related safety, stand-auth and session lifecycle tests remain applicable. Browser rendering and real garage deployment are separate evidence from local synthetic tests.
