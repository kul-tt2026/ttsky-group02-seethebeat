# SPDX-FileCopyrightText: © 2026 Jonas Creyns, Giel Swenters
# SPDX-License-Identifier: Apache-2.0
"""
Golden model of the SeeTheBeat chip<->MCU bus (see docs/bus_protocol.md).

Pieces, all authoritative:
  * encode_read / encode_write -- the MASTER side: turn a transaction into the exact
    sequence of 6-bit `uio[5:0]` transfers the chip drives. `src/mcu_bus.v` must emit
    byte-for-byte these values.
  * MCUSlave -- a cycle-stepped model of the MCU memory slave. It is PIPELINE-CAPABLE:
    it decodes commands continuously and streams read responses IN ORDER on `ui_in`, so
    the master may have several reads outstanding at once (full-duplex burst reads).
  * drive_read / drive_write / drive_burst_read -- reference master behaviours used by
    the self-test and mirrored by the RTL master.

Pure stdlib so it runs anywhere (host + CI), same as cordic.py / butterfly.py.
"""

# ---- opcodes (top 2 bits of transfer T0) ----
OP_NOP   = 0b00
OP_READ  = 0b01
OP_WRITE = 0b10
OP_CFGRD = 0b11          # config-read: same 2-transfer shape as READ, but the
                         # slave serves it from a SEPARATE region (see cfg below),
                         # because the FFT buffer already fills all 1024 words.

NOP = 0b000000           # idle word driven on uio[5:0]

ADDR_BITS = 10           # word address 0..1023 (512 complex points x 2 words)
DATA_BITS = 16
MAX_OUTSTANDING = 4      # pipelined reads in flight (one butterfly's 4 reads)


# ============================================================================
#  Master side: transaction -> list of 6-bit transfers
# ============================================================================
def encode_read(addr):
    """READ(addr) -> [T0, T1]  (2 transfers)."""
    addr &= (1 << ADDR_BITS) - 1
    t0 = (OP_READ << 4) | ((addr >> 6) & 0xF)   # {01, addr[9:6]}
    t1 = addr & 0x3F                             # addr[5:0]
    return [t0, t1]


def encode_cfgread(addr):
    """CFGRD(addr) -> [T0, T1]. Identical framing to READ; only the opcode differs, which
    is what tells the MCU to answer from the config region instead of the FFT buffer."""
    addr &= (1 << ADDR_BITS) - 1
    return [(OP_CFGRD << 4) | ((addr >> 6) & 0xF), addr & 0x3F]


def encode_write(addr, data):
    """WRITE(addr, data16) -> [T0..T4]  (5 transfers)."""
    addr &= (1 << ADDR_BITS) - 1
    data &= (1 << DATA_BITS) - 1
    t0 = (OP_WRITE << 4) | ((addr >> 6) & 0xF)   # {10, addr[9:6]}
    t1 = addr & 0x3F                              # addr[5:0]
    t2 = (data >> 10) & 0x3F                      # data[15:10]
    t3 = (data >> 4) & 0x3F                       # data[9:4]
    t4 = (data & 0xF) << 2                        # {data[3:0], 00}
    return [t0, t1, t2, t3, t4]


# ============================================================================
#  Slave side: cycle-stepped, pipeline-capable MCU memory model
# ============================================================================
class MCUSlave:
    """
    One `step(cmd6)` call == one clock. `cmd6` is the master's uio[5:0] this cycle;
    the return is (resp_valid, read_byte) the MCU drives this cycle.

    The command decoder runs every cycle, so reads may be issued back-to-back while
    earlier responses are still streaming (pipelining). Fetched words queue and are
    emitted IN ORDER as HI then LO bytes; once flowing, one word streams every 2 cycles.
    `latency` = fetch delay from a read's T1 to its HI byte, used to prove the master
    waits on resp_valid for any latency.
    """

    def __init__(self, latency=2):
        self.latency = latency
        self.sram = {}      # the FFT working buffer, addressed by READ/WRITE
        self.cfg = {}       # the config region, addressed by CFGRD -- a SEPARATE space
        self.reset()

    def reset(self):
        self.dec = "IDLE"           # command-decoder state
        self._addr_hi = 0
        self._addr = 0
        self._d1510 = 0
        self._d94 = 0
        self._respq = []            # queue of (ready_cycle, word) fetched, awaiting emit
        self._emit_word = 0         # word currently being emitted
        self._lo_pending = False    # LO byte owed next cycle
        self._next_free = 0         # earliest cycle the emitter may start a new word
        self._cyc = 0

    def step(self, cmd):
        cmd &= 0x3F
        resp_valid, rd_byte = 0, 0

        # ---- 1) response emitter (runs before the decoder, so a word fetched this
        #         cycle waits at least one cycle before its HI byte) ----
        if self._lo_pending:
            resp_valid, rd_byte = 1, self._emit_word & 0xFF          # LO
            self._lo_pending = False
            self._next_free = self._cyc + 1
        elif self._respq and self._cyc >= self._respq[0][0] and self._cyc >= self._next_free:
            _, word = self._respq.pop(0)
            resp_valid, rd_byte = 1, (word >> 8) & 0xFF              # HI
            self._emit_word = word
            self._lo_pending = True

        # ---- 2) command decoder (continuous, enables pipelining) ----
        d = self.dec
        if d == "IDLE":
            op = cmd >> 4
            if op == OP_READ:
                self._addr_hi = cmd & 0xF
                self.dec = "R1"
            elif op == OP_WRITE:
                self._addr_hi = cmd & 0xF
                self.dec = "W1"
            elif op == OP_CFGRD:
                self._addr_hi = cmd & 0xF
                self.dec = "C1"
            # NOP -> stay IDLE
        elif d == "R1":
            self._addr = (self._addr_hi << 6) | (cmd & 0x3F)
            word = self.sram.get(self._addr, 0) & 0xFFFF
            self._respq.append((self._cyc + self.latency, word))
            self.dec = "IDLE"
        elif d == "C1":
            self._addr = (self._addr_hi << 6) | (cmd & 0x3F)
            word = self.cfg.get(self._addr, 0) & 0xFFFF
            self._respq.append((self._cyc + self.latency, word))
            self.dec = "IDLE"
        elif d == "W1":
            self._addr = (self._addr_hi << 6) | (cmd & 0x3F)
            self.dec = "WD0"
        elif d == "WD0":
            self._d1510 = cmd & 0x3F
            self.dec = "WD1"
        elif d == "WD1":
            self._d94 = cmd & 0x3F
            self.dec = "WD2"
        elif d == "WD2":
            d30 = (cmd >> 2) & 0xF
            self.sram[self._addr] = (self._d1510 << 10) | (self._d94 << 4) | d30
            self.dec = "IDLE"

        self._cyc += 1
        return resp_valid, rd_byte


# ============================================================================
#  Reference master behaviours (mirrored by the RTL / used by the self-test)
# ============================================================================
def drive_write(slave, addr, data):
    """Feed a WRITE to the slave, one transfer per cycle (no response)."""
    for t in encode_write(addr, data):
        slave.step(t)


def drive_read(slave, addr, timeout=64):
    """Single (non-pipelined) READ -> 16-bit value."""
    return drive_burst_read(slave, [addr], timeout)[0]


def drive_cfgread(slave, addr, timeout=64):
    """Single config-read: same shape as drive_read, opcode 11."""
    cmd_stream = encode_cfgread(addr)
    got, ci = [], 0
    for _ in range(timeout):
        cmd = cmd_stream[ci] if ci < len(cmd_stream) else NOP
        rv, b = slave.step(cmd)
        if ci < len(cmd_stream):
            ci += 1
        if rv:
            got.append(b)
            if len(got) == 2:
                return (got[0] << 8) | got[1]
    raise TimeoutError("cfgread of {} never returned".format(addr))


def drive_burst_cfgread(slave, addrs, timeout=8192):
    """
    Streamed config-read of arbitrarily many addresses, throttled to MAX_OUTSTANDING reads
    in flight -- exactly what the on-chip refresh path does for the 17 visual_state words.
    Responses are strictly in order, so the n-th word answers the n-th command.
    """
    got, issued, done, pend = [], 0, 0, []
    for _ in range(timeout):
        cmd = NOP
        if pend:
            cmd = pend.pop(0)
        elif issued < len(addrs) and (issued - done) < MAX_OUTSTANDING:
            t0, t1 = encode_cfgread(addrs[issued])
            cmd, pend = t0, [t1]
            issued += 1
        rv, b = slave.step(cmd)
        if rv:
            got.append(b)
            if len(got) % 2 == 0:
                done += 1
                if done == len(addrs):
                    return [(got[2 * i] << 8) | got[2 * i + 1] for i in range(len(addrs))]
    raise TimeoutError("burst cfgread incomplete: {}/{} words".format(done, len(addrs)))


def drive_burst_read(slave, addrs, timeout=4096):
    """
    Pipelined burst read: stream all read commands back-to-back on uio while
    concurrently capturing the in-order responses on ui_in. Returns the words in the
    order requested. Enforces MAX_OUTSTANDING (drop-in for the RTL master's flow control).
    """
    if len(addrs) > MAX_OUTSTANDING:
        raise ValueError("burst {} exceeds MAX_OUTSTANDING {}".format(len(addrs), MAX_OUTSTANDING))
    cmd_stream = []
    for a in addrs:
        cmd_stream += encode_read(a)          # [T0, T1] per read
    need = 2 * len(addrs)
    got = []
    ci = 0
    for _ in range(timeout):
        cmd = cmd_stream[ci] if ci < len(cmd_stream) else NOP
        rv, b = slave.step(cmd)
        if ci < len(cmd_stream):
            ci += 1
        if rv:
            got.append(b)
            if len(got) == need:
                break
    else:
        raise TimeoutError("burst read incomplete: {}/{} bytes".format(len(got), need))
    return [(got[2 * i] << 8) | got[2 * i + 1] for i in range(len(addrs))]
