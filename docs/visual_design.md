# SeeTheBeat — visual design specification

**The single source of truth for what the chip draws: bands, zones, colours, effects, and
which of those can still change after tape-out.**

Keep this file current. When `src/pixel_gen.v`, `src/visual_state.v` or
`model/visual_ref.py` change, change this too — they are the implementation of what is
written here, and `model/test_visual_ref.py` is what proves they agree.

*Last updated: 2026-08-28 (geometry rebalance, refresh path, breathing edge, look config,
pixel pipeline).*

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
| `cfg` | 1 | 5 bits | `17` | look config: `{dim[1:0], palette[1:0], bw}` |
| `cfg2` | 1 | 5 bits | `18` | breathing amplitude, in 2-px units (0 = off) |

The chip also keeps an 8-bit **`frame` counter** (not part of `visual_state` — it is
generated on chip from `frame_start`), which drives the breathing edge in §4.

**95 flip-flops total** (16×5 bands + 5 flash + 2×5 config), plus a 16:1 read mux. This is the largest single area item in the
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
7. **Breathing edge (animation).** The fill threshold gains a small time-varying offset
   so a bar's tip drifts in and out instead of sitting still between beats:

   - `frame` is an 8-bit counter incremented once per `frame_start`; it wraps every 256
     frames (~4.3 s at 60 Hz). It is the **only** clock a stateless renderer has — nothing
     can animate by being remembered, because nothing can be stored.
   - `wobble = triangle(frame)`, **clipped to the firmware-set amplitude** (config word 18,
     in 2-px units → 0–62 px), one full breath per wrap.
   - **The amplitude is a knob, not a constant.** The first version fixed it at 7 px — and
     at 7 px the entire breathing range was *smaller than one band step* in the bass (8 px)
     and centre (12 px), i.e. 82% of the screen, so the effect sat below the quantisation of
     the thing it modulates and was simply invisible. The general lesson: **an effect that
     modulates a quantised quantity must span several of its steps to be seen at all.**
     Rather than guess a new constant, the amplitude moved into config — a value you cannot
     retune after tape-out is a value you will get wrong.
   - `cfg2 = 0` means no breathing, which is a legitimate setting and where an unwritten
     config region leaves the chip.
   - **A triangle, not a sine, by necessity.** The CORDIC is iterative — 21 clocks per
     result — while the renderer needs a value *every pixel clock*, so a per-pixel sine is
     impossible by construction. A triangle off the counter's own bits costs a handful of
     gates and is indistinguishable once it drives a soft edge.
   - **A silent band stays perfectly black.** The wobble may only extend a bar that is
     already lit (`fill = 0 if band == 0`). Without that guard the whole screen would
     shimmer faintly through quiet passages — the opposite of the mostly-black look.
   - `wobble(0) == 0`, so a frame-0 render is exactly the un-animated picture. Every static
     test relies on this.

8. **Blanking gate** — outside the visible area the output is forced to black. Light in the
   porches makes a monitor refuse to lock or shift the image sideways.

### The pixel path is PIPELINED (2026-08-28)

Steps 1–8 above are split across **one register stage**, after the band lookup:

```
stage 1   (px,py) -> zone decode -> depth, group -> visual_state 16:1 mux -> band
          ---- register: band[4:0], depth[10:0], group[1:0], active ----
stage 2   fill (+wobble) -> compare -> palette/hue -> level/cap -> flash+saturate -> pin
```

**Why:** this chain was the design's critical path in every harden report — the worst
endpoint was always a colour bit (`uo_out[4]`, then `uo_out[5]`). Adding the config knobs
pushed its raw slack from +0.043 ns to −0.474 ns in a single batch, and Phase 5's effects
target the same cone. The split roughly halves it for **21 flops**.

**`hsync`/`vsync` are delayed by the same one clock** in `project.v`, so sync stays aligned
with colour and the monitor sees an identical waveform shifted by 25 ns — nothing on screen
moves. Delaying the colour but *not* the sync is the classic way to break a VGA output.

**`flash`, `frame`, `cfg` and `cfg2` are deliberately not pipelined.** They are per-frame
constants written during vblank, so the one-cycle skew can only touch a pixel inside the
blanking interval, where the output is forced black anyway. Registering them would cost 23
more flops to fix something invisible.

**The function is unchanged** — only its timing. That is why `model/visual_ref.py` needs no
pipeline of its own, and why the RTL-vs-model comparison is still bit-exact.

**Output packing** (Tiny VGA Pmod): `uo_out = {hsync, B0, G0, R0, vsync, B1, G1, R1}`.
The pin *names* are the trap — the pin labelled `R1` carries `r[1]`, the MSB. Colour is
2 bits per channel = **64 colours**; the current design uses about 9 of them.

---

## 5. Refresh timing

`vga_timing` emits `frame_start`, a one-clock pulse at the beginning of vertical blanking.
That asks `fft_ctrl` — the chip's single bus master — to stream 19 `CFGRD`s and write each
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

## 5b. The look config — three firmware knobs

Config word 17 selects how the chip renders, and **every field is designed so that zero
means "as before"**:

| Bits | Field | Values |
| --- | --- | --- |
| `[0]` | **B&W** | 0 = colour, 1 = greyscale (all three channels driven equally) |
| `[2:1]` | **palette** | 0 classic (red/magenta/cyan/green), 1 ice, 2 fire, 3 neon |
| `[4:3]` | **dim** | 0 = full brightness … 3 = black. Applied as the saturation ceiling, so it dims the kick flash too |

And config word **18** is a fourth knob: **breathing amplitude**, 0–31 in 2-pixel units
(0 = off, 31 = 62 px).

**Why dim and not cap:** an unwritten MCU config region reads back 0. Encoding brightness as
a *cap* would make 0 mean "black screen", so any firmware that forgot the config would ship a
dead display. As a *dim* amount, 0 means full brightness and the failure mode is benign.

**These are physical-knob ready at zero silicon cost.** A pot → RP2350 ADC → firmware → this
config word. The chip never knows a knob exists.

Note the wider point about knobs: anything expressible as *"change the numbers the MCU
publishes"* — sensitivity, attack/decay, beat threshold, bin→band mapping, bass/treble
balance — is **already free** and needs no config bits at all. Only knobs that change how the
chip *interprets* the numbers need silicon. Another 5-bit config word costs ~30 GE plus
whatever logic it gates.

---

## 5c. DESIGN PRINCIPLE — everything should be knob-able

**Decided with Giel, 2026-08-27. Apply this to every visual parameter added from here on.**

When adding a visual parameter, the default assumption is that it should be **live-adjustable
from a physical knob**, and it needs a reason *not* to be. A DJ visualiser that can only be
retuned by reflashing firmware is a worse instrument than one with a brightness knob, and on
this architecture the knob is usually free.

**The chain:** pot → RP2350 ADC → firmware → config word in the MCU's config region → chip
fetches it in the vblank refresh. **The chip never knows a knob exists.**

### The two classes — check which one a new parameter falls into

| | Cost | Examples |
| --- | --- | --- |
| **Free knobs** — expressible as *"change the numbers the MCU publishes"* | **zero silicon, zero config bits** | sensitivity / gain, attack & decay, beat threshold, bin→band mapping, bass/treble balance, freeze, per-band trim |
| **Silicon knobs** — change how the chip *interprets* the numbers | ~30 GE per 5-bit config word, plus the logic it gates, **and it lands on the `uo_out` critical path** | palette, B&W, brightness cap (all built), effect enables, breathing speed |

Always ask whether a parameter can be moved into the first row. Most can: because the MCU
computes the band values, anything that is a transform *of those values* is free.

### If the demo board runs short of ADC pins

A pin shortage does **not** have to mean fewer knobs — these are all board/firmware level,
zero silicon:

- **Analog mux** (e.g. 74HC4051): 8 pots on 1 ADC pin + 3 GPIO.
- **One pot + a button** that cycles which parameter the pot is editing.
- **Rotary encoder** on 2 GPIO, no ADC at all.

⚠ **Open question:** how many ADC-capable pins the TT demo board actually leaves free, given
the RP2350 is already generating the 40 MHz clock and running hard-real-time PIO bus service.
Confirm this before committing to a knob count.

### Priority order, if we must choose

Should pins genuinely be scarce, add knobs in this order:

1. **Brightness / dim** — the control a DJ reaches for most, because room lighting changes. *(silicon: built)*
2. **Sensitivity / gain** — essential to adapt to track loudness. *(free)*
3. **Palette** — the mood control. *(silicon: built)*
4. **Beat sensitivity** — how hard the kick punches. *(free)*
5. **B&W** — a switch rather than a pot, so it can use a spare GPIO. *(silicon: built)*
6. **Effect intensity / enables** — once Phase 5's effects exist. *(silicon: not yet built)*

Note items 2 and 4 need **no silicon at all**, so they should be wired even if the config
word is full.

---

## 6. Frozen vs firmware — read this before tape-out

### Frozen in silicon (cannot change after tape-out)

- Zone **geometry**: which pixels belong to each zone, cell sizes, fill directions, `MUL`.
- Zone **hue** — the bottom strip is red, permanently.
- The brightness curve (top 2 bits, floored at 1) and flash saturation.
- The breathing effect's triangle shape, its ~4.3 s period, and its 62 px hardware
  ceiling. **The amplitude itself is firmware-controlled** (config word 18).
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
