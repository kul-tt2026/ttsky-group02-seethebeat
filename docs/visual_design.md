# SeeTheBeat — visual design specification

**The single source of truth for what the chip draws: bands, zones, colours, effects, and
which of those can still change after tape-out.**

Keep this file current. When `src/pixel_gen.v`, `src/visual_state.v` or
`model/visual_ref.py` change, change this too — they are the implementation of what is
written here, and `model/test_visual_ref.py` is what proves they agree.

*Last updated: 2026-08-27 (geometry rebalance + visual_state refresh path).*

---

## 1. The one-paragraph summary

The MCU decides, the chip draws. Firmware computes bin magnitudes, sums them into 16
frequency bands, detects the kick, applies attack/decay, and publishes a small
**`visual_state`** block. Once per frame during vertical blanking the chip fetches that
block over the bus and latches it. Then, for every one of the 480,000 visible pixels, the
chip computes the colour from scratch as **f(px, py, visual_state)** — no frame buffer, no
stored objects, nothing that moves on its own.

**Why it has to work this way:** an 800×600 frame at 6 bpp is 360 kB. On-chip memory at
2×2 tiles is roughly **160 bytes**. There is no frame buffer and there never can be, so a
pixel's colour must be produced in the single clock that pixel is emitted. Everything below
follows from that.

---

## 2. `visual_state` — the whole interface between firmware and silicon

| Field | Count | Width | CFG address | Meaning |
| --- | --- | --- | --- | --- |
| `band[0..15]` | 16 | 5 bits (0–31) | `0`–`15` | energy in that frequency band |
| `flash` | 1 | 5 bits (0–31) | `16` | global kick-flash level |

**85 flip-flops total**, plus a 16:1 read mux. This is the largest single area item in the
visual back-end and the main knob if utilisation gets tight — cost scales directly as
`NBANDS × BAND_W`.

- **Band 0 is the lowest frequency.** Which FFT bins feed which band is *firmware's* choice,
  so the frequency→zone mapping is not committed to silicon (see §6).
- Fetched by `CFGRD` (bus opcode `11`) from a separate address space — the FFT buffer
  already fills all 1024 words of the normal space. See `docs/bus_protocol.md`.
- **Power-on defaults are a ramp: 1, 3, 5 … 31.** This is deliberate and load-bearing: with
  no firmware at all, the chip already draws every zone at a different height, so a monitor
  check validates geometry, colour and the blanking gate at once. It replaces what would
  otherwise have been a throwaway test pattern.

---

## 3. Zone geometry — **committed to silicon**

Screen is 800 × 600. Zone boundaries are comparators on `(px, py)` in `pixel_gen.v`.

```
 px:  0        120                           680        800
      +--------+------------------------------+----------+  py=0
      |   L4   | C12 | C13 | C14 | C15         |    R8    |   highs HANG DOWN
      +--------+   (4 columns of 140,          +----------+   from the top,
      |   L5   |    filling DOWNWARD           |    R9    |   360 deep
      +--------+     from py=0, 360 deep)      +----------+
      |   L6   |                               |    R10   |
      +--------+                               +----------+
      |   L7   |                               |    R11   |
      +--------+------------------------------+----------+  py=360
      |   B0   |    B1     |    B2    |    B3             |
      |          bass, 240 deep, fills UPWARD             |
      +--------------------------------------------------+  py=600
       wings 120 deep, fill inward, rows of 90
```

| Group | Bands | Region | Cell size | Depth | Fills | Screen area |
| --- | --- | --- | --- | --- | --- | --- |
| Bass | 0–3 | bottom strip, `py ≥ 360` | 200 × 240 | **240** | upward | **40 %** |
| Low-mid | 4–7 | left wing, `px < 120` | 120 × 90 | 120 | rightward | 9 % |
| High-mid | 8–11 | right wing, `px ≥ 680` | 120 × 90 | 120 | leftward | 9 % |
| Highs | 12–15 | centre, `120 ≤ px < 680` | 140 × 360 | **360** | **downward from the top** | **42 %** |

**Rationale (Giel, 2026-08-27):** bass was given twice its original area and the highs a
quarter less, because the low end carries the energy a DJ is watching for. The highs were
also flipped to hang *down from the top* so high frequencies read as high on the screen and
the bass rises to meet them — and so that shrinking them leaves no dead black band in the
middle.

---

## 4. How a pixel gets its colour

For each pixel, in one clock:

1. **Decode the zone** from `(px, py)` → a zone index 0–15.
2. **Look up that zone's band value** in `visual_state`.
3. **Fill test** — `depth < band × MUL`, where `depth` is the distance from the edge the
   zone fills *from*, so one comparison serves all four directions.

   | Region | Depth | `MUL` | Full-scale reach |
   | --- | --- | --- | --- |
   | wings | 120 | ×4 | 124 |
   | bass | 240 | ×8 | 248 |
   | centre | 360 | ×12 | 372 |

   Each is scaled so a full-scale band just covers its zone without wasting range. All are
   multiples of 4, so the hardware is `base = band<<2` plus one shift and one adder —
   **no multiplier**.

4. **Brightness** — the band's top two bits, but **floored at 1** when lit. A
   quiet-but-present band must draw a dim bar, not nothing. The fill *height* carries the
   fine detail (all 5 bits); brightness is only the coarse cue, and the Pmod has just 4
   levels per channel anyway.
5. **Hue** — a fixed 3-bit mask per group:

   | Group | Colour | Mask `{r,g,b}` |
   | --- | --- | --- |
   | Bass | red | `100` |
   | Low-mid | magenta | `101` |
   | High-mid | cyan | `011` |
   | Highs | green | `010` |

6. **Kick flash** — the top 2 bits of `flash` are added to all three channels and
   **saturated**, never wrapped. A wrapped flash would read as a black frame exactly on the
   beat, the worst possible artefact.
7. **Blanking gate** — outside the visible area the output is forced to black. Light in the
   porches makes a monitor refuse to lock or shift the image sideways.

**Output packing** (Tiny VGA Pmod): `uo_out = {hsync, B0, G0, R0, vsync, B1, G1, R1}`.
The pin *names* are the trap — the pin labelled `R1` carries `r[1]`, the MSB. Colour is
2 bits per channel = **64 colours**; the current design uses about 9 of them.

---

## 5. Refresh timing

`vga_timing` emits `frame_start`, a one-clock pulse at the beginning of vertical blanking.
That asks `fft_ctrl` — the chip's single bus master — to stream 17 `CFGRD`s and write each
returned value into `visual_state`.

- Blanking is **183,168 clocks per frame** (27.6 % of 663,168); the refresh needs well under
  200. There is no bandwidth concern.
- Updating **only** in blanking is what stops a bar changing height halfway down the screen.
- A refresh is only ever started from the bus master's idle state, so it never interleaves
  with FFT traffic — responses carry no tags, so two interleaved readers would mis-route
  each other's data.
- **If a transform is still running at vblank, the refresh is skipped for that frame** and
  the visuals hold their previous values. At 60 Hz this is imperceptible.

---

## 6. Frozen vs firmware — read this before tape-out

### Frozen in silicon (cannot change after tape-out)

- Zone **geometry**: which pixels belong to each zone, cell sizes, fill directions, `MUL`.
- Zone **hue** — the bottom strip is red, permanently.
- The brightness curve (top 2 bits, floored at 1) and flash saturation.
- `visual_state`'s **shape**: 16 bands × 5 bits + 5-bit flash, and its CFG address map.
- VGA mode (800×600, all porches, both sync polarities) and the Pmod pin packing.
- FFT size (512 points) and the bus protocol.

### Firmware-controlled (changeable forever)

- **Every band value and the flash level, each frame** — so all bar heights, brightnesses
  and the kick effect.
- **Which FFT bins feed which band.** "Bass at the bottom" really means "bands 0–3 at the
  bottom" — firmware decides what band 0 *contains*, so the frequency→zone mapping is
  entirely firmware. You could put treble at the bottom. What you cannot change is that the
  bottom is red.
- Attack/decay envelopes — a bar that snaps up and falls back slowly costs **zero silicon**.
- Beat threshold and sensitivity; sample rate, decimation, windowing, gain and what counts
  as "full scale".

### 🔴 The dependency that makes all of the above real

Firmware control exists **only** because the refresh path is wired. If `visual_state`'s
write port were ever tied off again, every "firmware-controlled" item above would freeze
with it.

---

## 7. Proposed upgrades (not implemented)

Costed and discussed in `PART2_VISUALS_VGA_PLAN.md` § *Firmware-tunable visuals*. Summary:

| Upgrade | Cost | Effect |
| --- | --- | --- |
| B&W toggle | ~4 GE | greyscale |
| Brightness cap | ~15 GE | dim the whole scene |
| Palette select | ~80 GE | 4 hue sets |
| **Per-zone hue in `visual_state`** | **~290 GE** | full firmware colour control |
| **Soft fade to black + 4×4 ordered dither** | **~180 GE** | ~16 apparent levels per channel instead of 4; kills the blocky edges |

The last two compete for the same gates: per-zone hue is *insurance* (a wrong colour becomes
a firmware edit), fade+dither is *aesthetics*. Decide against the post-harden number.

Note the "more than 4 colours" question: the Pmod ceiling is 64 colours, and the limit is 4
*levels per channel*, not 4 colours. The headroom is already there — the current design
simply spends little of it.

---

## 8. Where things live

| File | Role |
| --- | --- |
| `model/visual_ref.py` | golden model — geometry, fill, colour, `VisualState` |
| `model/test_visual_ref.py` | 11 self-checks, exhaustive over all 480,000 visible pixels |
| `src/pixel_gen.v` | the renderer, purely combinational |
| `src/visual_state.v` | the register file + power-on defaults |
| `src/vga_timing.v` | beam position, sync, `frame_start` |
| `src/fft_ctrl.v` | bus master; owns both the transform and the refresh |
| `test_units/test_pixel_gen.py`, `test_visual_state.py` | RTL vs model |
| `docs/bus_protocol.md` | `CFGRD` and the config address map |
