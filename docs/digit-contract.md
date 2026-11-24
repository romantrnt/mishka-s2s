# Digit/emblem fill-colour serial encoding

Logical lighting state remains binary for all 18 channels (`0..17`).
`LightingCommand` and public desired-state snapshots continue to reject state 2.
Only `SerialLightingAdapter` projects digits and emblems into the optional wire protocol.

`LIGHTING_DIGIT_MODE=legacy` is the default and preserves existing CSV bytes.
`LIGHTING_DIGIT_MODE=fill_color` is an explicit candidate configuration option;
it must be enabled only for firmware verified to support this contract. USB
identity, an open port or successful host writes never prove support or colour.
This change does not edit any actual environment file or firmware.

| Logical indicator | Logical fill | Wire indicator | Meaning requested from firmware |
|---:|---:|---:|---|
| 0 | 0 | 0 | off |
| 1 | 0 | 1 | white |
| 1 | 1 | 1 | white |
| 0 | 1 | 2 | fill colour |

Fill and compensation channels stay 0/1. Emblems share the indicator encoding:
channel 4 uses fill 3 (button 6), channel 17 uses fill 1 (button 7).
Compensation channels 5 and 16 remain binary. The existing setting name and
legacy default are retained. Digit IDs are
`15,12,11,8,7,2,0`, paired with fill IDs `14,13,10,9,6,3,1`.
No RGB payload or new channel is introduced. Wire framing remains
`element_id,state,transition_ms` followed by LF.

The adapter computes each touched shared-fill group from final logical values.
A fill-only update emits its digit and its mapped emblem; an indicator-only
update reconciles its fill and sibling indicator after a cancelled partial frame.
Last command per channel wins regardless of distinct input order. Each explicit
indicator keeps its transition; implicit indicators use the fill command or the
first changed indicator. Fills are sent before their indicators.

Logical group state is committed only after a whole current batch succeeds.
Cancelled or failed partial batches cannot commit future values. Retry uses the
same final group. Reconnect retains the last completed logical baseline, clears
the written-line suppression cache, and never establishes firmware state. The
physical owner still controls generation fences and complete dark resets.

Main-owned integration seam in `HardwareIntegrationStand`:

```python
SerialLightingAdapter(
    self.settings.lighting_device,
    baudrate=self.settings.lighting_baudrate,
    timeout_s=self.settings.lighting_timeout_s,
    digit_mode=self.settings.lighting_digit_mode,
)
```

The mode does not enable itself by deployment profile or device discovery.
Candidate activation and physical colour acceptance are separate operations.
Tests use synthetic serial transports and do not prove actual LED colours.

An independent configuration hunk adds `DIALOGUE_JOURNAL_ENABLED` (default false)
and `DIALOGUE_JOURNAL_DIR` (default `runtime/private/session-journal`). The directory
is redacted from public configuration. Journal storage and application hooks are
separate owners; this module neither creates a journal nor enables recording.
