"""
vectors.py -- test-vector generators for the FFT golden model and (later) the RTL.

All generators return a list of N ints in Q1.15 (range [-32767, 32767]; we avoid the
-32768 edge on purpose, see fft_ref.py). Imaginary input is always zero (real audio).
"""

import math
import random

FULL = 32767            # ~ +1.0 in Q1.15
DEFAULT_AMP = 30000     # a bit below full scale, leaves headroom


def impulse(N=512, amp=FULL):
    """Unit impulse at n=0 -> flat spectrum (all bins equal)."""
    x = [0] * N
    x[0] = amp
    return x


def dc(N=512, amp=DEFAULT_AMP):
    """Constant signal -> all energy in bin 0."""
    return [amp] * N


def tone(bin_k, N=512, amp=DEFAULT_AMP, phase=0.0):
    """Cosine at an integer bin -> spikes at bin_k and N-bin_k."""
    return [int(round(amp * math.cos(2 * math.pi * bin_k * n / N + phase))) for n in range(N)]


def two_tone(bin_a, bin_b, N=512, amp=DEFAULT_AMP // 2):
    """Sum of two cosines -> two spike pairs."""
    a = tone(bin_a, N, amp)
    b = tone(bin_b, N, amp)
    return [a[n] + b[n] for n in range(N)]


def noise(N=512, amp=DEFAULT_AMP, seed=0):
    """Uniform random signal (reproducible via seed)."""
    rng = random.Random(seed)
    return [rng.randint(-amp, amp) for _ in range(N)]
