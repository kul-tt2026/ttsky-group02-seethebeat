# Golden reference models

Bit-accurate Python reference implementations of every hardware block. These are the
**source of truth** the RTL is tested against — the cocotb tests in `../test/` compare
hardware output to these models, so a passing test means the Verilog matches a model we
can read and reason about.

> Rule: build the model **before** the Verilog for each block, and validate the model
> against a floating-point reference (e.g. `numpy.fft`) first.

## Planned contents (added as we reach each phase — see `../../PART1_FFT_PLAN.md`)
- `fft_ref.py` — fixed-point radix-2 DIT 512-point FFT mirroring the hardware exactly
  (same widths, Q-format, per-stage scaling, bit-reversal). *Phase 1.*
- `cordic.py` — bit-accurate CORDIC (rotation + vectoring modes, **multiplier-free**
  gain compensation via `SCALE_STEPS` shift-adds + `GUARD` bits — see *Step 2.5*).
  *Phase 2.* Once done, `fft_ref.py` is updated to use it so the golden model matches
  the CORDIC-based hardware.
- `vectors.py` — test-vector generators (impulse, single tone at a known bin, two-tone,
  full-scale/overflow, random) shared by all tests.

## How the cocotb tests use these
From a test in `../test/`, put the model on the path:

```python
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "model"))
from fft_ref import fft512_fixed        # etc.
```

## Running model self-checks (no hardware)
```sh
pip install -r requirements.txt
python -m pytest model/          # once model unit tests exist
```