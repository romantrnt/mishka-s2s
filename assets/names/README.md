# Recorded announcer names

`manifest.json` is shared by private welcome, local stories and per-turn conversation TTS. Only an exact supported nominative form is spliced once; aliases must be spelling variants, never different names. Missing recordings/forms use a whole phrase in the configured primary voice or a neutral phrase. No visit data is written here.

Source: [supplied Drive folder](https://drive.google.com/drive/folders/1aO0rjuDSLINPkfMSf7hJHGOBlB5aBSKS), 244 WAV files. Original files are retained outside Git at `/home/khugardin/Documents/dataset_tr/names/drive-originals/`; `source-index.json` beside that directory maps public library titles to source IDs.

239 mono PCM16 24 kHz clips are prepared and hash-checked. Five sources have ambiguous sustained segments and remain in `not_prepared`, with whole-primary TTS fallback. Original exports contain leading silence and trailing click-like transients; preparation retains the sustained utterance and records exact source frame bounds and SHA256. Pronunciation, edit naturalness and usage rights have not been independently verified by human listening/legal review. Technical WAV validation does not establish these.

Rebuild with the existing project Python environment:

```sh
python scripts/prepare_name_pack.py /home/khugardin/Documents/dataset_tr/names assets/names
```

The runtime does not require SciPy for reading the prepared clips; rebuilding uses the project's existing NumPy/SciPy dependencies. Do not add real visitor names or recordings to the manifest.
