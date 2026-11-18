# Remote phone interface

`/console` opens the original operator workbench with the MISHKA phone replacing
the former button diagram. The toolbar, runtime sidebar, lighting, story playback,
live dialogue and diagnostic panels retain the original `console.css` styling.
The turquoise shell, receiver, recessed paw, seven material colors and split 6/7
pads follow the supplied prototype references.
The procedural model is an interpretation of their proportions, not measured CAD.

## Use

- Lift or replace the receiver by clicking it, using the main action, or pressing
  Space outside a form or focused button. Canonical confirmation moves the receiver.
- Press keys on the paw or use 1–7. Pointer and keyboard use one gesture adapter.
  A release after 600 ms sends `hold`; after 5000 ms it sends `long_hold`. Cancellation
  and focus loss send only release. Repeated keydown cannot create duplicate presses.
- Enable sound with its explicit control. A normal user gesture also unlocks Web
  Audio under mobile autoplay policy. Enable the microphone separately when the
  backend is ready to listen. A persistent red text indicator means capture is live;
  its button stops capture directly. This is the browser microphone and speaker.
- Open **Инструменты** for Max synthetic RFID choices `demo-1`, `demo-2`, `unknown`,
  `archive`, session reset, input disconnect/reconnect/delay, a bounded journal,
  and separate text/WAV inputs. Engineer diagnostics remain visible in the
  workbench. The legacy duplicate device and raw-input surfaces stay hidden; all
  phone mutations still use the shared adapter. No reader location is invented
  on the physical model.
- **Плоский вид** uses the same semantic buttons, canonical state, transport, and
  audio. WebGL initialization or context loss also switches to this usable view.

The gateway establishes a same-origin HttpOnly authentication cookie. JavaScript
never asks for, stores, or exports the token. An unavailable/admission-blocked
runtime is shown explicitly and new device inputs are disabled. Browser connection
and injected virtual-input disconnection are shown separately.

## Ownership

`phone.js` owns gesture tracking, accessible controls, state presentation and the
closed tools drawer. `phone-model.js` owns Three.js geometry, projection and short
mechanical interpolation. It raycasts only the receiver and seven key meshes.
`console.js` retains the existing bounded FIFO, reserved release/audio callbacks,
urgent cleanup, observer lifecycle, PCM resampling and owned asset playback.

The shared render boundary rejects older simulator epochs and older journal cursors
within an epoch before updating controls or media fences. A clean relift may have a
new canonical state with a lower `state_version`; journal order remains authoritative.
If the server process is restarted, reload the page to establish its new epoch
namespace. Reconnection alone does not authorize an older snapshot.

All scenario progression, button availability, accepted handset state, prompts,
identity results and 18 lighting channels come from `state.simulator`. Physical
base colors stay visible when lighting is off. Fill, digit and symbol effects
follow their individual canonical channels; no decorative LED sequence runs in JS.
Static recorded prompt text may be displayed; dynamic/private transcript text stays
redacted. See `remote-device-contract.md` for the adapter's wire contract.

Hangup/reset/disconnect, page exit and lost authenticated WebSocket stop media,
clear queued PCM, invalidate pending microphone permission, and release gestures.
Late microphone tracks are stopped before capture nodes are created. Backend PCM
session/generation fields and local generation fences reject stale queued output.
After connection loss, replacing and lifting the receiver starts a clean media
boundary. Temporary backend microphone ineligibility retains the enabled track
while its socket/session/epoch owner remains current, but blocks PCM intake and
clears the resampler tail. Intake resumes when eligibility returns. Loss of that
owner or input admission stops the track; enabling it again requires an explicit
action. Reduced-motion preference removes mechanical interpolation.

## Bundled Three.js

`console/vendor/three.module.min.js` and `three.core.min.js` are the unmodified build
files from official npm `three@0.181.1`. The npm tarball SHA-512 integrity was checked
before extraction. `console/vendor/LICENSE.three` preserves its MIT license.
There are no CDN runtime requests, remote fonts, model downloads, or new JS framework.

## Verification

Run the existing and new focused checks from the repository root:

```bash
node tests/console_audio_regression.js app/foundation/console/console.js
node tests/console_queue_regression.js app/foundation/console/console.js
node tests/console_guide_regression.js app/foundation/console/console.js
node tests/console_phone_regression.js app/foundation/console
```

The new check exercises hold/long hold, duplicate-key suppression, guaranteed
release after rejected hold, epoch retention, cancellation, late microphone
permission, urgent simulator cleanup, and stale-snapshot rejection across hangup,
reset and clean relift without adding a test framework.
Browser fixture checks cover rendered desktop/mobile raised/down geometry, native
pointer/keyboard gestures, focus return, reduced motion, functional 2D switching,
context-loss fallback and horizontal overflow. Fixtures establish browser behavior;
they do not establish an admitted real-service voice path or physical hardware.
