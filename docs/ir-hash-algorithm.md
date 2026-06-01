# Stadium IR hash algorithm

Reference for the 32-character hexadecimal `irhash` value that Helix
Stadium uses to identify impulse responses in `.hsp` presets.

helixgen computes this hash locally — bit-identical to what the device
produces — so user-IR presets can be generated without any device
round-trip.

## What it is

When you load a WAV file into a `With Pan`-style IR block in a Helix
Stadium preset, the `.hsp` file does not store a filename or slot
number — it stores a 32-character hex string called `irhash`. The device
matches that string against the impulse responses already loaded into
its Cab IR storage. If a match is found, the IR block resolves to the
loaded IR; if not, the slot displays "No Model" on the device.

The hash is content-derived: it's deterministic, depends only on the
WAV bytes (after the preprocessing below), and is independent of
filename, slot, or directory.

## Algorithm

The hash is **MD5 of the data chunk content** of a WAV file produced by
this preprocessing pipeline:

1. Open the source WAV with libsndfile.
2. Stream-read float frames; write them to a temp WAV (`tmp1`) as
   **PCM_24, 48 kHz, same channel count as source**. (This step is a
   lossy float → int24 quantization at the libsndfile level — see below.)
3. Re-read `tmp1` as float.
4. If the source is stereo, take the **left channel only**. (Stadium
   deinterleaves and discards the right channel.)
5. Determine output length `N`:
   - If `frames > 8191` → `N = 8192` (truncate)
   - Else if `frames` is already a power of 2 → `N = frames`
   - Else → `N = next power of 2 ≥ frames` (pad with zeros)
6. Copy `min(frames, N)` floats into the output buffer.
7. **If `frames ≥ N`** (truncation or exact-pow2): apply an exponential
   fade to the last 128 samples — multiply each sample
   `out[N - 128 + i]` by `expf(i × -1/25.6)` for `i ∈ [0, 128)`. When
   zero-padding (`frames < N`), the fade is skipped.
8. Write the output buffer to a second temp WAV (`tmp2`) as
   **PCM_24, 48 kHz, mono** (channels forced to 1).
9. MD5 of `tmp2`'s data chunk content → the irhash.

## Key constants

| Constant       | Value      | Role                                           |
|----------------|------------|------------------------------------------------|
| Truncation cap | `8192`     | Output frame count when input has > 8191 frames |
| Truncation threshold | `8191` (`0x1FFF`) | Boundary between truncate and pad branches |
| Fade window    | `128`      | Number of trailing samples the exp fade touches |
| Fade exponent  | `-1/25.6` (`-0.0390625`, exact float32) | `expf(i × k)` decay coefficient |
| Output sample rate | `48000` | Forced regardless of source rate (currently helixgen errors on non-48 kHz; Stadium itself runs source through libsamplerate) |
| Output channels | `1`       | Forced mono in the final write |

## Why two round-trips matter

libsndfile-1.2.2's float → PCM_24 quantization introduces a 1-LSB
rounding error on a small number of high-amplitude samples (≈ a
half-dozen samples in a 24000-frame IR). Stadium's pipeline incurs
this loss **twice** — once writing `tmp1`, once writing `tmp2` — so
the final hashed bytes can differ from the source by up to 2 LSB on
those specific samples.

This is load-bearing for hash reproducibility. helixgen's reference
implementation in [`src/helixgen/ir.py`](../src/helixgen/ir.py)
calls libsndfile directly via `ctypes` so the same code path runs.
The `soundfile` Python wrapper takes a different libsndfile code path
that's lossless and would NOT reproduce Stadium's hash.

## Reference implementation

[`compute_stadium_irhash(wav_path)`](../src/helixgen/ir.py) in
`src/helixgen/ir.py`. About 100 lines including the lazy
libsndfile loader and the 2 MB ctypes buffer management.

```python
from helixgen.ir import compute_stadium_irhash
h = compute_stadium_irhash("/path/to/IR.wav")
# h == 32-char lowercase hex string
```

Validated against 27 known `(hash, wav)` pairs (1 ground-truth export
from real hardware + 26 entries from commercial IR pack registrations)
and end-to-end on a real Stadium XL device on 2026-05-31 (generated
preset → device displayed the correct IR name in the block slot).

## Caveats and scope

- **48 kHz sources only.** Non-48 kHz sources raise `NotImplementedError`
  with a `sox in.wav -r 48000 out.wav` suggestion. Stadium uses
  `SRC_SINC_BEST_QUALITY` from libsamplerate to resample non-48k
  sources before applying the rest of the pipeline; porting that
  resampler bit-exactly is a separate reverse-engineering project and
  is not yet done.
- **Bit depth.** The output WAV format preserves the source bit depth
  (PCM_S8 / PCM_16 / PCM_24 / PCM_32). The 27-pair validation set
  covers PCM_24 sources; PCM_16 and PCM_32 are not exercised by the
  test suite but follow the same code path.
- **Floating-point WAV sources** (`WAVE_FORMAT_IEEE_FLOAT`) are
  untested. Most commercial IR packs ship PCM_24.

## Related code

- CLI: `helixgen ir-scan <dir>` walks a directory, computes hashes,
  caches them in `~/.helixgen/irs/mapping.json` for use by
  `helixgen generate`.
- MCP tool: `compute_irhash(model, wav_b64)` in `mcp_server/tools.py`
  exposes the same primitive over the MCP transport for the hosted
  helixgen deployment.
- Test suite: `tests/test_ir_cli.py` exercises the full pipeline with
  synthesized WAVs (no paid IR fixtures shipped).

## ELI5

Helix Stadium needs a way to recognize the same IR file regardless of its
filename or which slot it lives in. So it assigns each IR a **fingerprint**:
a 32-character code computed from the audio data itself. When you import an
IR onto the device, Stadium computes the fingerprint and remembers it.
Presets reference IRs by fingerprint — so when you load a preset, the
device scans its loaded IRs, finds one whose fingerprint matches, and uses
that one. Rename or move the WAV on disk, doesn't matter: same audio,
same fingerprint.

helixgen's job is to compute that exact same fingerprint **without** the
device, so a preset can be generated for an IR you haven't (yet)
round-tripped through the Helix Stadium app.

**What does Stadium actually do?** Roughly:

1. Read the WAV file as floating-point audio samples.
2. Write those floats to a temp WAV as 24-bit integers. **This rounds a few
   samples by 1 bit.**
3. Read the temp file back. Cut to 8192 samples. Fade the tail to silence
   with a specific exponential curve.
4. Write that out as a *second* temp WAV. **Rounds the same samples again.**
5. Take the MD5 hash of the audio bytes in step 4's file. That's the
   fingerprint.

**Why the float-to-int rounding matters.** The float → int24 conversion in
steps 2 and 4 is slightly lossy — a handful of high-amplitude samples shift
by 1 bit each time. Stadium runs the conversion twice, so the error stacks
to about 2 bits on those samples. helixgen has to reproduce that exact
behavior, or its fingerprint won't match Stadium's.

That detail is why the reference implementation talks to libsndfile
directly via `ctypes`. The friendlier Python wrapper (`soundfile`) takes a
"smarter" code path internally that avoids the rounding — which sounds
great in isolation, but produces the *wrong* fingerprint here because
Stadium itself takes the rounding path. Matching the wrong abstraction
would silently give you the wrong answer.

**The payoff.** Before this work, knowing an IR's fingerprint required
round-tripping the file through the device: import it via the Helix Stadium
app, export a preset that references it, then parse the preset to recover
the fingerprint. That works for one IR at a time but is tedious for whole
libraries. Now `helixgen ir-scan ~/IRs/` does the whole library in one
pass at about a millisecond per file. You can generate presets referencing
IRs the device has never seen, and as long as you later import those WAVs
into the device through the Librarian, the fingerprints line up and the
preset loads correctly.
