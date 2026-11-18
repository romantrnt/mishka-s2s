# Immersive games candidate — 2026-11-16

Base: `162bedaaa1741095a674606ee6e4f54c37826720`. Work branch: `feat/immersive-games-20261116`.
This document describes the candidate; it is not a deployment or physical acceptance claim.

## Product choices

- A personal greeting proceeds to the menu without the unanswered “How are you?” exchange. Personalized name validation and private RAM audio remain.
- Stable game menu: **1** free light waves, **2** quizzes, **3** repeat the pattern, **4** odd one out, **5** spoken word game. Existing recordings introduce the menu and start a selected game.
- Within games, 5 can be a choice. Button 7 answers a question when it is one of the offered answers; otherwise it exits. Natural spoken exit and safety handling remain separate from scoring.
- Content is original: 50 distinct five-option odd-one-out sets and 24 quiz prompts for each of three age groups. Youngest quiz prompts deliberately revisit seven colors and two confirmed pictograms. About 17% of each shuffled deck comes from curated adjacent difficulty; toddler questions do not silently acquire number questions.
- Youngest odd-one-out options use color labels. Physical color and pictogram meanings are fixed; only verbal numbered/color-labeled item assignments are shuffled. The weather symbol is called “значок погоды” because its physical drawing has not been confirmed.
- Pattern level is retained only within the current session. Re-entering the game menu supplies new randomized content; handset/session cleanup still owns the boundary.
- Light waves follow logical button order in both directions. No measured physical button geometry is claimed. Smoothness uses the board’s existing transition duration, without firmware changes or software PWM.

## Short live check after installation

Use an authorized bracelet and the middle or older age group for the number-answer check. Let the selected game finish its instruction before answering. In the current game menu, a fresh press on an offered game (1–5) interrupts the invitation, narration or repeat prompt and selects that game immediately. Release an already held key before selecting. A non-selection key such as 6 only skips the menu audio; it does not select a game.

1. Lift the handset: the name is spoken, then the menu starts without waiting for a “How are you?” answer.
2. Enter games and choose 1. Tap different buttons rapidly and say “не хочу играть” while tapping. Voice exit must still work; re-enter and check button 7 as well. Waves should follow the new origin and leave no frozen frame after exit.
3. Re-enter games and choose 2. Answer a question; for a question offering 7, it must score an answer, not exit. Say “не хочу играть” to leave.
4. Choose 3. Repeat one shown sequence, leave with 7, and re-enter: progress is retained during the same session.
5. Choose 4. Listen to five options and watch the matching buttons. A wrong answer gives a hint; a correct answer moves on. Leave by voice.
6. Choose 5. Exchange one familiar word with Mishka while tapping buttons 1–4 and 6; irrelevant taps must not lose the turn. Then say “хватит играть”. Finally replace the handset during any game’s speech: audio and lights must stop, and the next session must not continue the old prompt.

Physical sound, pronunciation, speech recognition and perceived animation quality require this live check; software checks do not establish them.

## Historical candidate verification

The review recorded for this candidate reported no open P1/P2 findings. Its final probes independently reproduced and then verified the fixes for stale word-game state after help, ignored keys cancelling a word turn, and voice exit lost during button activity. The measurements below describe that revision, not acceptance of the current Runpod browser path.

The final focused batch covered canonical graph, game exits/instruction terminals, headless turn-taking, the four immersive-game modules, menu content, owned sequence playback, and private welcome: **265 passed in 55.60 seconds**. The Max integration module was initially skipped because its external bundle was not configured. The existing `/home/server/rfid-lab-20261113/phone-ubuntu-client-local-demo.zip` was then supplied with `MISHKA_TEST_MAX_BUNDLE`; only that module was rerun: **32 passed, 2 subtests passed in 6.47 seconds**. Total: **297 passed**, plus two subtests; the initial module skip is resolved. The external ZIP remained byte-identical.

`python scripts/verify_canonical_contract.py` returned `canonical contract acceptance: OK`; `git diff --check` passed. The Max integration used local HTTPS/SQLite and synthetic capture; it did not establish physical RFID or live profile behavior. No Max production source, firmware, database, or `.env` changes are included. The full repository suite was not run. Live remained at the base commit during verification; hardware audio, recognition, and animation acceptance remain pending.

## Historical serial timeout follow-up

On the first live game-1 test, voice exit reached `root_menu`; about one second later native UART writing timed out and hardware fail-closed handling latched `lighting_unavailable`. The process and HID reader remained alive. A light-only replay on the board passed before this patch, so the original transient USB/firmware cause is not established.

The serial owner now suppresses only identical positive-transition targets whose complete CSV lines were successfully written. Zero-transition menu/reset/dark frames always transmit in full. Reopen and uncertain I/O invalidate the cache; an RX epoch prevents an old blocked TX completion from restoring it. Existing generation, partial-line, timeout and fail-closed behavior is unchanged.

For three checked wave origins the real engine generated 180/126/162 commands; serial suppression reduced each to 49. Two focused regression batches passed (33 each); the final 12 new checks passed after a test-only review addition. Independent review found no open P1/P2 issues and verified generation cancellation through the real output worker. The post-patch physical replay of rapid waves, a pause and root-menu transition observed 119 writes and 119 firmware replies, with no faults (maximum host write 20.799 ms). This establishes reduced traffic and that replay only; the original combined speech/button incident still needs physical retest.
