# MISHKA exhibition menu and game control checkpoint

Date: 2026-10-26. Branch `fix/p0-lifecycle-20261026`, isolated
`/home/server/mishka-fixes`. Clean HEAD `d8af32c40adacabdb28e84b1cc766cc0f20f6782`.

## Closed control bugs

- Exhibition `menu_ready` buttons 1, 3 and 5 now enter the existing
  `game_menu`, `support_intake` and `wellbeing_check` modes, respectively.
  Each retains `ExhibitionMenuSelected` telemetry and applies the matching
  existing lighting preset. Button 3 retains the handset guard at the
  canonical boundary. Buttons 2 and 6 still enter their existing story and
  dialogue modes. An application+graph regression checks all five active
  routes. The seeded handset-down canonical guard is tested separately:
  application ingress intentionally rejects physical input for a handset-down
  exhibition session before routing it.
- In `game_active`, an informational release/hold ACK and a timeout during
  `game_exit_confirmation` preserve that pending choice. Press 7, release
  7, press 7 confirms exit; press 7, release 7, press 6 cancels. Release,
  hold and timeout neither score nor change level. Other modes retain their
  preexisting `button_edge_ack` substate. The condition is local to game
  confirmation; global `_decision` reason behavior was not removed.

## Proven blocker, no invented UX

The current target formula can show button 7 at level 2 and button 5 at
level 6. These controls are reserved for exit and help, so neither target is
reachable as a game answer. Two **strict xfail** regressions demonstrate the
actual application output for those levels. No arbitrary remapping, scoring
rule, game sequence, silent completion, or new phrase was introduced.
An approved unavailable-step transition and audible cue are needed before
a fail-closed runtime response is chosen. The game catalog's 13 entries do
not imply 13 implemented mechanics.

Exhibition button 4 still selects the menu item without activating the
placeholder story generator. Button 7 still has the wrong PHRASE_099 fallback
and lacks a typed audible day/plan result; both need separate product work.
This phase does not claim either feature complete.

## Evidence and custody

All new cases were run red before fixes. New menu/game file:
**5 passed, 2 xfailed in 0.95 s**. Final full headless suite:
**341 passed, 13 failed, 3 xfailed in 38.33 s**. Previous checkpoint:
**336 passed, 13 failed, 1 xfailed**. The 13 failing names are unchanged;
the two additional xfails are the explicit target-control blockers.
Staged and unstaged `git diff --check` passed.

Tests used only the existing `/home/server/mishka/.venv`, in-memory
profiles/fake audio, and an absent /tmp environment file. No dependency,
physical pin/color, hardware, paid model/service, credential, live main,
push, merge or deploy was changed. Prior staged and unstaged work remains.

## Final review amendment: pending confirmation blocks game scoring

A fresh application+graph regression reproduced press 7 then target 1:
`game_progress` previously scored while the game still awaited exit confirmation.
The game-active confirmation branch now accepts only 7 (confirm) and 6
(cancel); other game buttons emit the existing pending diagnostic without
changing score, level or target. The prior help/safety branch and clinical
handset-down continue to take precedence. Outside confirmation, target 1
still scores normally. Release/hold/timeout and story/onboarding checks
remain passing. The two unsupported target 5/7 xfails are unchanged.

Targeted menu/game file after amendment: **7 passed, 2 xfailed in 1.56 s**.
Final full headless suite: **343 passed, 13 failed, 3 xfailed in 38.70 s**.
The 13 failure names match the earlier checkpoint; there are no new
failures. Staged and unstaged `git diff --check` passed. No live main,
hardware, push or deploy.
