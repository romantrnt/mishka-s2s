# Temporary story-menu content limitation

The current runtime maps 1–4 to stories, 5 to help, and 7 to back. Existing
recordings PHRASE_054 and PHRASE_055 promise a fifth story on button 5. They
cannot truthfully describe the active mapping.

This interim patch preserves help. It suppresses those two menu recordings,
shows the actual mapping in the operator WebPanel, and plays approved
PHRASE_075 ("Можно показать кнопкой") once per session. It does not present
that hint as a menu or ask to repeat it. Any button interrupts the hint. Button 5 immediately keeps its existing help
priority; other buttons require a new press after release for the normal action. Hangup/identity reset
clears the one-time hint. The existing main-menu repeat behavior is unchanged.

The canonical visual effect describes the same mapping, but no new physical
screen or hardware renderer is implemented. An audio-only child does not
receive a complete spoken list. This is NOT completion of the requested
narrated-menu experience and not production acceptance.

Content decision to obtain later:

1. Keep the present help priority and approve a corrected recording listing
   four stories plus help and back; then restore one-time menu/repeat behavior.
2. If five stories are required here, explicitly agree a navigation design
   that keeps help accessible before producing a matching approved recording.

No new recordings, voice assets, Max code, hardware configuration or live
processes were changed. Existing clinical/safety semantics are not redefined.
