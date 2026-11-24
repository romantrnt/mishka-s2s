# Menu choice and dialogue entry

Menu narration remains useful for a first visit: spoken choices and the existing
lighting tour explain the controls. Narration alone forces experienced visitors
to wait; immediate choice alone leaves a new visitor guessing. The combined
rule is therefore: keep the tour, but let one fresh press choose any actually
available root action immediately, including before its light or spoken mention.
Brightness indicates presentation, not permission to use an available control.

The chosen edge cancels the old tour/clock and takes the normal root dispatcher
once. An already held key is not a fresh choice; release is required before
another action on that key. Callback owners prevent an old narration or wave
from replacing the selected scene. Submenus retain their committed catalogue
mapping: an available current choice can interrupt narration, while a changed
mapping cannot silently select a replacement slot. An unavailable library shows
its honest empty chooser and exit instead of launching missing content.

Web controls use the same availability contract independently of light targets.
During the tour the short instruction says that waiting is unnecessary; the
existing transcript panel retains the full recorded speech. Do not teach a
hidden first-press-to-skip, second-press-to-choose convention.

Speech captured for the menu's repeat question retains its original question
owner through VAD, STT and delayed completion. An ordinary old answer cannot
become a turn in a newly selected conversation. Safety classification still
precedes that phase rejection, preserving urgent/help priority within the same
visit; epoch/session guards reject all old completions after a new visit.

Button 6 starts calm `preparing` breathing before awaited old-audio cleanup,
validation or synthesis. `output` follows owned PCM preparation; `listening`
follows the matching playback terminal event. A prepared/submitted PCM frame
does not establish when sound physically reached the listener. Exit and hangup
revoke delayed preparation and playback owners. Cold synthesis latency remains
a separate performance concern, not a reason to display the static button-1
frame or pretend the conversation is already ready.

## Evidence and limits

The [W3C cognitive-accessibility feedback pattern](https://www.w3.org/WAI/WCAG2/supplemental/patterns/o4p10-status-feedback/)
recommends prompt, clear feedback about each action. Its
[control/action relationship pattern](https://www.w3.org/WAI/WCAG2/supplemental/patterns/o1p06-control-actions/)
supports consistent, understandable mappings. The
[NN/G child usability study](https://www.nngroup.com/articles/children-ux-physical-development/)
describes observed motor/discoverability difficulties and the need to account
for age and input method. These sources support this design direction; they
do not establish successful first-use acceptance for our device, all ages 1–7,
or children with autism.

Automated coverage exercises seven tutorial phases × seven choices, unlit but
enabled controls, duplicate/held/early-release edges, late callbacks, reset and
hangup. A local browser with explicit mock-provider/readiness fixtures exercised
one press during the tutorial into dialogue, button-7 return with microphone
off, release and hangup. Gated CPU timing measurements are synthetic. Real
phone press-to-feedback, first audible speech, listening readiness and
uncoached first-use testing remain separate physical acceptance checks.
