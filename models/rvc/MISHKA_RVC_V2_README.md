# Mishka RVC v2

The release was trained from the 24 supplied studio WAV files using RVC v2,
40 kHz, RMVPE, ContentVec and the pretrained HiFi-GAN generator/discriminator.
Original files were retained unchanged. Preprocessing used automatic silence
slicing without denoising, normalization or enhancement filters.

## Selected inference settings

- Model: `mishka_rvc_v2_e250_2250s.pth`
- Index: `mishka_rvc_v2_e250_2250s.index`
- Pitch: `0`
- F0: `rmvpe`
- Index rate: `0.30`
- Protect: `0.33`
- Volume envelope: `1.0`
- No autotune, denoising, post-processing or formant shift

The 250-epoch checkpoint with index rate 0.00 produced the best balance of
speaker similarity and intelligibility on three unseen Russian test phrases.
Higher index rates increased phoneme substitution and were rejected.

## Runtime constraint

The RVC model and ContentVec/RMVPE components must remain loaded in one
persistent worker. On the RTX 5090, a fresh CLI process took about 8.7 seconds,
while warmed calls converted a 5.52-second sample in 0.50-0.70 seconds
(real-time factor 0.09-0.13).

The model is trained from an unusually small corpus. It is suitable for
integration testing and real listening evaluation, but it must not be described
as production-accepted until it is tested with the project's actual Silero→RVC output.
