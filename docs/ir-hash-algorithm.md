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
