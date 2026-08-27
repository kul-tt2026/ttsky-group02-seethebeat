# SPDX-FileCopyrightText: © 2026 Jonas Creyns, Giel Swenters
# SPDX-License-Identifier: Apache-2.0
"""
Guard: the VISUAL GEOMETRY in the RTL must equal the geometry in the golden model.

Why this exists (2026-08-27): `src/pixel_gen.v` was left with `WING_W = 160` after the
rebalance while `model/visual_ref.py` had moved to `120`. Nothing caught it -- the
transcription checks used hand-typed constants rather than the file, so they agreed with
each other and not with the RTL. It surfaced only as four cocotb failures whose pattern
took real work to decode, and it had silently reintroduced a bug the model had already
caught once (160-deep wings against a maximum fill of 31*4 = 124 can never fill).

This parses the actual Verilog and compares it to the actual model, so the two cannot
drift again. It needs no simulator, so it runs in the fast `golden-model` CI job and fails
long before anything is hardened.
"""

import io
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "..", "src")
sys.path.insert(0, HERE)
import visual_ref as V  # noqa: E402


def _parse_consts(path):
    """Pull `parameter`/`localparam` integer definitions out of a Verilog file.

    Handles the plain `NAME = <int expression>;` forms this project uses, evaluating each
    against the ones already parsed. Verilog integer division truncates, so `/` -> `//`.
    """
    text = io.open(path, encoding="utf-8").read()
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
    text = re.sub(r"//[^\n]*", " ", text)

    consts = {}
    pat = re.compile(r"\b(?:localparam|parameter)\s+(?:integer\s+)?(\w+)\s*=\s*")
    for m in pat.finditer(text):
        # Scan to the end of the expression: a ';', or a ',' / ')' at paren depth 0.
        # (A naive [^;,)]+ truncates "(CENTRE_R - CENTRE_L) / 4" at the first bracket.)
        i, depth, end = m.end(), 0, None
        while i < len(text):
            ch = text[i]
            if ch == "(":
                depth += 1
            elif ch == ")":
                if depth == 0:
                    end = i
                    break
                depth -= 1
            elif ch == ";" or (ch == "," and depth == 0):
                end = i
                break
            i += 1
        if end is None:
            continue
        e = text[m.end():end].strip().replace("/", "//")
        if not e or not re.fullmatch(r"[\w\s+\-*/()]+", e):
            continue                      # skip sized literals, $clog2, concatenations
        try:
            consts[m.group(1)] = int(eval(e, {"__builtins__": {}}, dict(consts)))
        except Exception:
            pass
    return consts


PIXEL_GEN = _parse_consts(os.path.join(SRC, "pixel_gen.v"))
VISUAL_STATE = _parse_consts(os.path.join(SRC, "visual_state.v"))


def _cmp(rtl, name, expected, where):
    assert name in rtl, "{}: {} not found -- was it renamed?".format(where, name)
    assert rtl[name] == expected, "{}: {} is {} in RTL but {} in the model".format(
        where, name, rtl[name], expected)


def test_screen_size_matches():
    _cmp(PIXEL_GEN, "H_VIS", V.H_VIS, "pixel_gen.v")
    _cmp(PIXEL_GEN, "V_VIS", V.V_VIS, "pixel_gen.v")


def test_region_boundaries_match():
    """The one that actually drifted. WING_W sets where the wings end and the centre
    begins, so a mismatch moves every zone boundary from that column onward."""
    _cmp(PIXEL_GEN, "BOTTOM_TOP", V.BOTTOM_TOP, "pixel_gen.v")
    _cmp(PIXEL_GEN, "WING_W", V.WING_W, "pixel_gen.v")
    _cmp(PIXEL_GEN, "CENTRE_L", V.CENTRE_L, "pixel_gen.v")
    _cmp(PIXEL_GEN, "CENTRE_R", V.CENTRE_R, "pixel_gen.v")


def test_zone_splits_match():
    _cmp(PIXEL_GEN, "BOTTOM_SPLIT", V.BOTTOM_SPLIT, "pixel_gen.v")
    _cmp(PIXEL_GEN, "WING_SPLIT", V.WING_SPLIT, "pixel_gen.v")
    _cmp(PIXEL_GEN, "CENTRE_SPLIT", V.CENTRE_SPLIT, "pixel_gen.v")


def test_animation_constants_match():
    """The breathing effect's constants live in both places too, so guard them the same way
    -- a FRAME_W mismatch would change the breathing period without any test noticing."""
    _cmp(PIXEL_GEN, "FRAME_W", V.FRAME_W, "pixel_gen.v")
    _cmp(PIXEL_GEN, "WOBBLE_MAX", V.WOBBLE_MAX, "pixel_gen.v")


def test_visual_state_shape_matches():
    _cmp(PIXEL_GEN, "NBANDS", V.NBANDS, "pixel_gen.v")
    _cmp(PIXEL_GEN, "BAND_W", V.BAND_W, "pixel_gen.v")
    _cmp(PIXEL_GEN, "FLASH_W", V.FLASH_W, "pixel_gen.v")
    _cmp(VISUAL_STATE, "NBANDS", V.NBANDS, "visual_state.v")
    _cmp(VISUAL_STATE, "BAND_W", V.BAND_W, "visual_state.v")
    _cmp(VISUAL_STATE, "FLASH_W", V.FLASH_W, "visual_state.v")


def test_every_zone_can_actually_fill():
    """The failure mode a wrong WING_W reintroduces: if a zone is deeper than a full-scale
    band can reach, that meter can never look full. Checked against the model's own MULs,
    so it holds for any future geometry."""
    for depth, mul, what in ((V.WING_W, V.MUL_WING, "wings"),
                             (V.V_VIS - V.BOTTOM_TOP, V.MUL_BASS, "bass"),
                             (V.BOTTOM_TOP, V.MUL_CENTRE, "centre")):
        reach = V.BAND_MAX * mul
        assert reach >= depth, \
            "{}: {} px deep but a full-scale band only reaches {}".format(what, depth, reach)
        # and not so oversized that the top of the range does nothing visible
        assert reach < depth + mul * 2, \
            "{}: reach {} wastes range over a depth of {}".format(what, reach, depth)


def test_wobble_step_is_what_the_rtl_hardcodes():
    """pixel_gen.v builds the amplitude as {cfg2, 1'b0}, i.e. a hardcoded x2. If the model
    ever wants a different step the concatenation must change too, so pin it here -- the
    same pattern as the fill multipliers below."""
    assert V.WOBBLE_STEP == 2,         "model WOBBLE_STEP is {} but pixel_gen.v hardcodes x2".format(V.WOBBLE_STEP)


def test_fill_multipliers_are_what_the_rtl_hardcodes():
    """pixel_gen.v builds the fill as base = band<<2, then base / base<<1 / base<<1+base.
    That hardcodes x4 / x8 / x12 -- if the model ever wants different multipliers, the RTL
    expression must change too, so pin them here."""
    assert (V.MUL_WING, V.MUL_BASS, V.MUL_CENTRE) == (4, 8, 12), \
        "model MULs are {} but pixel_gen.v hardcodes (4, 8, 12)".format(
            (V.MUL_WING, V.MUL_BASS, V.MUL_CENTRE))


def _main():
    checks = [test_screen_size_matches, test_region_boundaries_match,
              test_zone_splits_match, test_visual_state_shape_matches,
              test_animation_constants_match,
              test_every_zone_can_actually_fill,
              test_wobble_step_is_what_the_rtl_hardcodes,
              test_fill_multipliers_are_what_the_rtl_hardcodes]
    print("SeeTheBeat RTL-vs-model geometry sync check")
    print("-" * 58)
    ok = 0
    for c in checks:
        try:
            c()
            print("  PASS  {}".format(c.__name__))
            ok += 1
        except AssertionError as e:
            print("  FAIL  {}  --> {}".format(c.__name__, e))
    print("-" * 58)
    print("{}/{} checks passed".format(ok, len(checks)))
    return 0 if ok == len(checks) else 1


if __name__ == "__main__":
    raise SystemExit(_main())
