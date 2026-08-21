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
| _(reserved)_ | `11`    | config-read (extension for later if possible); undefined in v1.                                        |

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

## Config-read (reserved)

Visual config (color/B&W, palette, brightness cap, …) flows MCU→chip. The chip therefore
**reads** config from a small reserved MCU region (opcode `11`, or normal READs to a
reserved address range — TBD in future extension maybe) a few times per frame and latches
on-chip config registers. There is deliberately **no** chip→MCU "config write": the chip
cannot receive an unsolicited write, only data it asked for.
