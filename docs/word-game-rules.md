# Word game: announced rules and private state

The introductory speech determines the actual rule, rather than an LLM deciding
whether a child's answer is legal:

- Ages 1–3 alternate familiar animals or foods; 4–5 alternate animals.
- Ages 6–7 use common nouns in their dictionary form, on the previous word's last
  letter. Terminal Ь, Ъ and Ы are skipped. Names and repeated lemmas are excluded.
- Legacy policy aliases 3–5 and 5–7 use the corresponding animal/chain rule.

Thus `обезьяну` is normalized to `обезьяна`. In animal mode a subsequent `слон`
can be valid. In chain mode Mishka must first answer on А (currently `акула`),
then the child's next word must start on А. Uncertain ASR, alternatives and
unrecognized categories cause a short clarification, without consuming a round.
The animal/food vocabulary is authored and bounded; it is not a complete taxonomy.
The chain mode accepts dictionary-known unambiguous common nouns beyond that bank.

The existing output validators, cancellation tokens and owned PCM queue still
apply to authored replies. No LLM invents the next word. A first PCM delivery
reserves a private pending state; it does not prove the word was heard. An
interrupted turn permits either continuation from the proposed word or correction
under the previous rule. A stale generation cannot commit either state.

Successful turns retain the existing age-specific 4/5/6-turn limits. A separate
24-word bound prevents repeated interrupted turns growing RAM indefinitely.
Natural completion and holding/releasing 7 restore the original caller; a game
opened from conversation returns to that conversation. Session reset discards
word state. Child words are not added to the journal or public snapshot.

## Dependencies and evidence

Install the project requirements in a dedicated environment, not a running
shared environment. `pymorphy3==2.0.6`, `pymorphy3-dicts-ru==2.4.417150.4580142`
are pinned; the engine's declared dependencies include `dawg2-python`.
If morphology cannot load, the game explains the limitation and retains button 7
and normal inactivity handling; it does not fall back to an unchecked LLM.

The installed dictionary package's METADATA identifies its Python code as MIT
and its dictionary data as Creative Commons Attribution-ShareAlike 3.0.
Preserve its distributed attribution/license metadata when packaging dependencies:
[dictionary project](https://github.com/no-plagiarism/pymorphy3-dicts),
[OpenCorpora](https://opencorpora.org/),
[CC BY-SA 3.0](https://creativecommons.org/licenses/by-sa/3.0/).
No dictionary binaries are vendored in this repository.

`tests/test_word_game_rules.py` exercises actual morphology, synthetic ASR and
the real application/validator/PCM-port path, including announced age modes,
invalid/repeated input, interruption, natural return, held-button exit, bounded
RAM and a new visit. `tests/test_stage1_nested_game_integrity.py` checks caller
identity/history/budget and late callbacks. These do not establish real microphone,
human hearing or physical-board acceptance.
