# Digit/fill serial encoding

Logical lighting state remains binary for all 18 channels (`0..17`).
`LightingCommand` and public desired-state snapshots continue to reject state 2.
Only `SerialLightingAdapter` projects digits into the optional wire protocol.

`LIGHTING_DIGIT_MODE=legacy` is the default and preserves existing CSV bytes.
`LIGHTING_DIGIT_MODE=fill_color` is an explicit candidate configuration option;
it must be enabled only for firmware verified to support this contract. USB
identity, an open port or successful host writes never prove support or colour.
This change does not edit any actual environment file or firmware.

| Logical digit | Logical fill | Wire digit | Meaning requested from firmware |
|---:|---:|---:|---|
| 0 | 0 | 0 | off |
| 1 | 0 | 1 | white |
| 1 | 1 | 1 | white |
| 0 | 1 | 2 | fill colour |

Fill, pictogram and compensation channels stay 0/1. Digit IDs are
`15,12,11,8,7,2,0`, paired with fill IDs `14,13,10,9,6,3,1`.
No RGB payload or new channel is introduced. Wire framing remains
`element_id,state,transition_ms` followed by LF.

The adapter computes each touched pair from the final logical values in the
batch before writing. Distinct channel input order does not change the result;
repeated updates to one channel use its last value. A fill-only update also emits
its digit. A digit-only update reconciles its fill first. The digit transition
uses its explicit command when present, otherwise its fill command's transition.
The fill is sent before its corresponding digit.

Logical pair state is committed only after a whole current batch succeeds.
Cancelled or failed partial batches cannot commit future values. Retry uses the
same final pair. Reconnect retains the last completed logical baseline, clears
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
