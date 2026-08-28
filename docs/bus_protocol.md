# SeeTheBeat — chip ↔ MCU bus protocol (v1, 2026-08-19)

The on-chip FFT engine keeps its working buffer in the MCU's SRAM and streams it over the
pins every stage. The **chip is bus master**; the **MCU is a memory slave** (answers reads
and writes). The MCU generates the chip clock, so both ends run in **exact lockstep** on a
single clock — no clock-domain crossing.

This file is the authoritative wire spec. The Verilog master (`src/mcu_bus.v`), the Python
golden model (`model/mcu_bus_model.py`), and the future RP2040 PIO firmware must all match
it exactly.

## Pins

| Signal       | Pins         | Dir      | Meaning                                                           |
| ------------ | ------------ | -------- | ----------------------------------------------------------------- |
| `read_data`  | `ui_in[7:0]` | MCU→chip | 8-bit read-data lane (clean byte framing).                        |
| `resp_valid` | `uio[7]`     | MCU→chip | `1` = the byte on `ui_in` is valid **this** cycle.                |
| `mcu_status` | `uio[6]`     | MCU→chip | MCU-ready / frame-ready. Read by the FFT controller, not the bus. |
| `cmd`        | `uio[5:0]`   | chip→MCU | 6-bit command/address/write-data lane.                            |
| `uio_oe`     | —            | chip     | held **`0x3F`** (bits 5:0 = outputs, bits 7:6 = inputs).          |

`uio_out[7:6]` are driven `0` (their `oe`=0, so ignored). Reads gate on `resp_valid`; the
master **never** assumes a fixed response latency.

## Command lane encoding

The **opcode is the top 2 bits of the first transfer (T0)**. Payload transfers (T1, T2, …)
carry no opcode — the slave identifies them by its FSM position within the transaction.

| Op           | T0[5:4] | Transfers on `uio[5:0]`                                                                                |
| ------------ | ------- | ------------------------------------------------------------------------------------------------------ |
| `NOP`        | `00`    | `000000` — driven whenever the master is idle / waiting.                                               |
| `READ`       | `01`    | `T0={01, addr[9:6]}`, `T1=addr[5:0]`                                                                   |
| `WRITE`      | `10`    | `T0={10, addr[9:6]}`, `T1=addr[5:0]`, `T2=data[15:10]`, `T3=data[9:4]`, `T4={data[3:0], 2'b0}`         |
| `CFGRD`      | `11`    | **config-read** (v1, added 2026-08-27): `T0={11, addr[9:6]}`, `T1=addr[5:0]` — identical framing to `READ`, answered from a **separate** address space. |

- **Address** is a 10-bit **word** address (0..1023): 512 complex points, interleaved as
  word `2i` = re[i], word `2i+1` = im[i]. (A 512-point complex buffer is 1024 words.)
- **Write-data** is 16 bits (one Q1.15 real or imag component).
- Padding bits (the `2'b0` in T4) are driven `0` and ignored by the slave.

## Transaction timing

### WRITE (5 cycles, no response)

```
cycle:   0     1     2     3     4     5 ...
uio:     T0    T1    T2    T3    T4    NOP
         └── master drives 5 consecutive transfers; slave stores on T4 ──┘
```

### READ (2 command cycles + handshked response)

```
cycle:   0     1     2 .......... k    k+1   k+2 ...
uio:     T0    T1    NOP  ...    NOP   NOP   NOP      (master waits, driving NOP)
ui_in:   -     -     -    ...    HI    LO    -        (MCU drives the 2 data bytes)
resp_v:  0     0     0    ...    1     1     0        (each byte gated by resp_valid)
```

- After T1 the master drives `NOP` and watches `resp_valid`.
- The MCU takes an arbitrary number of cycles `k` (its fetch latency) then presents the
  **high** byte with `resp_valid=1`, then the **low** byte with `resp_valid=1`. The two
  valid bytes need not be consecutive — the master simply captures on each `resp_valid=1`
  and assembles `{HI, LO}` after the 2nd.
- Because the master waits for `resp_valid`, any MCU latency is safe. A stuck MCU stalls
  the chip (visible) rather than returning silently-wrong data.

### Pipelined (burst) reads

The command lane (`uio`) and response lane (`ui_in`) are physically separate, so the
master may issue the **next** read's command while the **previous** read's data is still
streaming back. It issues up to `MAX_OUTSTANDING = 4` reads back-to-back and captures the
responses **in order** (i-th word ↔ i-th command). One butterfly's 4 reads
(`A_re, A_im, B_re, B_im`) are exactly such a burst.

```
uio:   R0a R0b R1a R1b R2a R2b R3a R3b NOP NOP ...   (4 read commands, back-to-back)
ui_in:  -   -   -   -  H0  L0  H1  L1  H2  L2  H3 L3  (responses stream, in order)
resp_v: 0   0   0   0  1   1   1   1   1   1   1  1
```

- The fetch latency `L` is paid **once** at fill, not per read; once flowing, one 16-bit
  word streams every **2 cycles** (the HI/LO pair). A 4-read burst ≈ `8 + L` cycles vs
  `4·(4+L)` serial.
- Responses are **strictly in order** — no tags, no reorder buffer. The master routes the
  n-th returned word to the n-th outstanding read.
- The slave must buffer up to `MAX_OUTSTANDING` fetched words.
- **Writes are never interleaved with outstanding reads:** the master drains all pending
  read responses before issuing a `WRITE`. This removes read-after-write hazards and keeps
  ordering trivial. (v1 rule; relaxable later.)
- A single read is just a burst of length 1, so the non-pipelined description above still
  holds.

## Framing & reset

- Transfers are **sequence-framed**: the slave's FSM knows whether it is expecting an
  opcode (IDLE) or a specific payload transfer, so payload bytes are never mistaken for
  opcodes.
- `rst_n` (active low) returns both master and slave to IDLE, re-aligning the framing.
- The master must drive `NOP` (`000000`) on every idle cycle so the slave in IDLE never
  latches a stale command.

### The idle NOP is load-bearing — do not optimise it away

There is **always at least one NOP cycle before every transaction**: `wr_accept`/`rd_accept`
require `state == S_IDLE`, and `S_IDLE` drives NOP combinationally, so the earliest a new
transaction can begin is the cycle *after* an idle cycle. Back-to-back writes are therefore
`W0 W1 W2 W3 W4 NOP W0 …`, never `…W4 W0…`.

That gap gives the protocol a **self-resynchronising** property, which is the only thing
standing between a dropped transfer and permanent framing loss:

> If the MCU ever fails to sample one cycle, its FSM is one transfer behind: it has consumed
> 4 of the master's 5 write transfers and is still waiting for its last payload. It consumes
> the following NOP as that payload, completes a **corrupted** transaction, and returns to
> IDLE **realigned with the master**. Damage is bounded to one wrong word — a single garbled
> frame at 60 Hz — instead of every subsequent transfer being misparsed until reset.

Squeezing the NOP out to gain ~17% write bandwidth would trade a transient glitch for a
permanent failure. The bus is nowhere near bandwidth-limited (~70k of 663k cycles per frame),
so there is nothing to gain and a great deal to lose.

## Flow control: what is and isn't handshaked

| Direction | Backpressure |
| --- | --- |
| MCU→chip (`ui_in` + `resp_valid`) | **Full handshake.** The master waits indefinitely; no fixed-latency assumption. |
| chip→MCU (`uio[5:0]`: commands *and* write data) | **None.** The master drives T0…T4 on consecutive cycles and assumes each was sampled. |

**There is no write acknowledgement, deliberately.** The MCU *is* the memory: once a write
reaches the slave FSM, storing it is a local operation with no medium in between that can
fail. The only way to lose a write is to miss a sample, and because both ends share one
clock with no CDC and the slave is a fixed FSM, that is a **timing-budget** property, not a
reliability one — either the firmware sustains the rate on every cycle or it fails on
essentially all of them. There is no stochastic middle ground in which a per-write ack would
tell you anything a bring-up test would not.

**Two caveats the firmware must respect (see `PART3_INTEGRATION_PLAN.md`):**

1. **The clock cannot be used as flow control.** It is tempting to read "the MCU generates
   the chip clock, so both ends are in lockstep" (top of this file) as "the MCU can always
   stall the chip by withholding edges". Once Part 2's VGA shares that single clock, it
   **cannot** — the pixel clock must be a stable 40 MHz or the monitor loses sync. The MCU
   must therefore service the bus in hard real time. Escape hatches if it cannot: lower the
   clock, and fall back to 640×480 @ 25.175 MHz.
2. **Verify the command stream instead of acking it.** The chip's access sequence is fully
   deterministic and known in advance (2304 butterflies in a fixed order), so the firmware
   can check that each incoming address is the one it expected next. That catches a dropped
   transfer *and* a framing desync, costs no pins and no gates, and is strictly stronger
   than a per-write ack.

## Config-read (`CFGRD`, opcode `11`) — IMPLEMENTED

Used once per frame by the on-chip `visual_state` refresh.

**Why it needs its own opcode rather than a reserved address range:** a 512-point complex
buffer is 1024 words, and the address is 10 bits — so `READ`/`WRITE` already address *every*
word. There is no spare address space to carve out. `CFGRD` therefore selects a **second,
separate address space** on the MCU side. Address 7 under `CFGRD` and address 7 under `READ`
are different locations.

**Framing is identical to `READ`** — 2 transfers, same address split, same handshaked
response on `ui_in`/`resp_valid`, same in-order pipelining and the same
`MAX_OUTSTANDING = 4`. Only the opcode nibble differs, so the MCU-side PIO can share one
capture datapath and simply branch on the opcode when servicing.

**The config region layout (v1)** — 20 words, one value per word, low bits only:

| CFG addr | Meaning | Bits used |
| --- | --- | --- |
| `0` … `15` | `visual_state` band 0…15 (0 = lowest frequency) | `[4:0]` |
| `16` | global kick-flash level | `[4:0]` |
| `17` | **look config**: `{dim[1:0], palette[1:0], bw}` | `[4:0]` |
| `18` | **breathing amplitude**, in 2-pixel units (0 = off, 31 = 62 px) | `[4:0]` |
| `19` | **fade config**: `{--, fade_sh[1:0], fade_en}` (0 = hard bar tips) | `[2:0]` |
| `20`+ | reserved (the chip ignores them today) | — |

**Config word 19** enables the soft fade + 4×4 ordered dither on each bar's tip.
`fade_en` turns it on; `fade_sh` selects the ramp width, `16 << fade_sh` pixels, so
16 / 32 / 64 / 128. Bits `[4:3]` are reserved and must be written 0.

**Config words 17 and 19 are designed so that ALL-ZERO means "classic look"** — full colour, palette
0, full brightness. That is a safety property, not a convenience: an unwritten config region
reads back 0, so firmware that only publishes bands still gets a normal picture. It is why
brightness is encoded as a *dim* amount rather than a *cap* (a cap of 0 would blank the
screen on any firmware that forgot to set it).

The chip reads all 20 back-to-back at the start of vertical blanking and latches them. The
upper bits of each returned word are ignored, so firmware may pack extra information there
later without a silicon change.

**There is deliberately still no chip→MCU "config write":** the chip cannot receive an
unsolicited write, only data it asked for. Everything flows MCU→chip as a response.

**Ordering rule:** the chip is a single bus master (`fft_ctrl`) and a refresh is only ever
started from its idle state, so a refresh never interleaves with FFT traffic. This matters
because responses carry no tags — two interleaved readers would mis-route each other's data.
If a transform is still running when vblank arrives the refresh is **skipped** for that
frame and the visuals hold their previous values, which is imperceptible at 60 Hz.
