"""
test_mcu_bus_model.py -- self-checks for the bus golden model.

    python model/test_mcu_bus_model.py     # standalone, prints a report
    pytest model/                          # as unit tests

Checks the exact transfer bit-packing, that a write->read round-trip through the
cycle-stepped MCUSlave returns the same data for any address / value / MCU latency, and
that PIPELINED burst reads return the right words in order (proving the full-duplex
overlap of command issue and response streaming).
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mcu_bus_model as bus   # noqa: E402


def _pattern(addr):
    """Deterministic 16-bit value per address (no RNG needed)."""
    return (addr * 40503 + 0x1234) & 0xFFFF


def test_encode_read_bits():
    # addr=165=0b00_1010_0101 (10b) -> addr[9:6]=0b0010=2, addr[5:0]=0b100101=37
    assert bus.encode_read(165) == [0x12, 37], bus.encode_read(165)


def test_encode_write_bits():
    # addr=165 -> T0={10,0010}=0x22, T1=addr[5:0]=37; data=0xBEEF -> T2..T4
    assert bus.encode_write(165, 0xBEEF) == [0x22, 37, 47, 46, 60], \
        bus.encode_write(165, 0xBEEF)


def test_encode_decode_consistent():
    """Feeding encode_write's own transfers into the slave must store `data`."""
    for addr in (0, 1, 511, 512, 1000, 1023):
        for data in (0x0000, 0xFFFF, 0x8000, 0x7FFF, 0xA5A5):
            s = bus.MCUSlave(latency=0)
            bus.drive_write(s, addr, data)
            assert s.sram[addr] == data, (addr, data, s.sram.get(addr))


def test_roundtrip_all_latencies():
    """Write a whole pattern, read it back (single reads) -- for several MCU latencies."""
    for lat in (0, 1, 2, 5, 13):
        s = bus.MCUSlave(latency=lat)
        addrs = list(range(0, 1024, 13)) + [0, 1, 1022, 1023]
        for a in addrs:
            bus.drive_write(s, a, _pattern(a))
        for a in addrs:
            got = bus.drive_read(s, a)
            assert got == _pattern(a), \
                "lat={} addr={}: got {:#06x} exp {:#06x}".format(lat, a, got, _pattern(a))


def test_burst_read_inorder():
    """Pipelined burst returns the requested words in order, for several latencies."""
    for lat in (0, 1, 2, 4, 9):
        s = bus.MCUSlave(latency=lat)
        for a in range(1024):
            bus.drive_write(s, a, _pattern(a))
        # walk the address space in bursts of MAX_OUTSTANDING
        k = bus.MAX_OUTSTANDING
        for base in range(0, 1024, k):
            addrs = list(range(base, min(base + k, 1024)))
            got = bus.drive_burst_read(s, addrs)
            exp = [_pattern(a) for a in addrs]
            assert got == exp, "lat={} base={}: got {} exp {}".format(lat, base, got, exp)


def test_burst_matches_single():
    """A burst of the SAME addresses gives identical data to reading them one-by-one."""
    s = bus.MCUSlave(latency=3)
    addrs = [1000, 3, 1023, 128]               # arbitrary order, 4 = MAX_OUTSTANDING
    for a in addrs:
        bus.drive_write(s, a, _pattern(a ^ 0x55))
    single = [bus.drive_read(s, a) for a in addrs]
    burst = bus.drive_burst_read(s, addrs)
    assert burst == single, (burst, single)


def test_burst_butterfly_shape():
    """The real access pattern: 4 reads (A_re,A_im,B_re,B_im) as one pipelined burst."""
    s = bus.MCUSlave(latency=2)
    a_re, a_im, b_re, b_im = 10, 11, 266, 267   # A at k, B at k+stride
    vals = {a_re: 0x1111, a_im: 0x2222, b_re: 0x3333, b_im: 0x4444}
    for addr, v in vals.items():
        bus.drive_write(s, addr, v)
    got = bus.drive_burst_read(s, [a_re, a_im, b_re, b_im])
    assert got == [0x1111, 0x2222, 0x3333, 0x4444], got


def test_overwrite():
    s = bus.MCUSlave(latency=2)
    bus.drive_write(s, 300, 0x1111)
    bus.drive_write(s, 300, 0x2222)
    assert bus.drive_read(s, 300) == 0x2222


def test_unwritten_reads_zero():
    s = bus.MCUSlave(latency=1)
    assert bus.drive_read(s, 42) == 0x0000


def test_max_outstanding_guard():
    s = bus.MCUSlave(latency=1)
    try:
        bus.drive_burst_read(s, list(range(bus.MAX_OUTSTANDING + 1)))
    except ValueError:
        return
    raise AssertionError("burst over MAX_OUTSTANDING should raise")


def test_nop_between_transactions():
    """Idle NOPs must not corrupt framing between back-to-back transactions."""
    s = bus.MCUSlave(latency=2)
    bus.drive_write(s, 7, 0xCAFE)
    for _ in range(5):
        s.step(bus.NOP)
    bus.drive_write(s, 8, 0xF00D)
    for _ in range(3):
        s.step(bus.NOP)
    assert bus.drive_read(s, 7) == 0xCAFE
    assert bus.drive_read(s, 8) == 0xF00D


def test_cfgread_encoding_differs_only_in_the_opcode():
    """CFGRD must be framed exactly like READ -- same 2 transfers, same address split --
    so the RTL and the PIO slave share one datapath. Only the opcode changes."""
    for addr in (0, 1, 16, 63, 64, 512, 1023):
        r = bus.encode_read(addr)
        c = bus.encode_cfgread(addr)
        assert len(c) == 2, c
        assert c[1] == r[1], "T1 (addr[5:0]) must match READ at addr {}".format(addr)
        assert (c[0] & 0x0F) == (r[0] & 0x0F), "T0 address nibble must match READ"
        assert (c[0] >> 4) == bus.OP_CFGRD, "T0 opcode must be 11"
        assert (r[0] >> 4) == bus.OP_READ


def test_cfgread_uses_a_separate_address_space():
    """The FFT buffer already fills all 1024 words, which is the whole reason config-read
    exists. A CFGRD to address N must NOT return sram[N]."""
    sl = bus.MCUSlave(latency=1)
    sl.sram[7] = 0xBEEF
    sl.cfg[7] = 0x0015
    got = bus.drive_cfgread(sl, 7)
    assert got == 0x0015, "cfgread returned {:#06x}, expected the CFG value".format(got)
    sl2 = bus.MCUSlave(latency=1)
    sl2.sram[7] = 0xBEEF                      # cfg left empty
    assert bus.drive_cfgread(sl2, 7) == 0, "unwritten config must read 0, not the FFT buffer"


def test_cfgread_does_not_disturb_the_fft_buffer():
    sl = bus.MCUSlave(latency=2)
    for a in range(8):
        sl.sram[a] = 0x1000 + a
        sl.cfg[a] = a
    before = dict(sl.sram)
    for a in range(8):
        assert bus.drive_cfgread(sl, a) == a
    assert sl.sram == before, "a config-read must never touch the FFT buffer"


def test_cfgread_burst_returns_in_order():
    """The refresh path issues 17 back-to-back config-reads; they must come back in issue
    order, exactly like a normal read burst."""
    sl = bus.MCUSlave(latency=3)
    for a in range(17):
        sl.cfg[a] = (a * 3 + 1) & 0xFFFF
    got = bus.drive_burst_cfgread(sl, list(range(17)))
    assert got == [(a * 3 + 1) & 0xFFFF for a in range(17)], got


def _main():
    checks = [test_cfgread_encoding_differs_only_in_the_opcode,
              test_cfgread_uses_a_separate_address_space,
              test_cfgread_does_not_disturb_the_fft_buffer,
              test_cfgread_burst_returns_in_order,
              test_encode_read_bits, test_encode_write_bits,
              test_encode_decode_consistent, test_roundtrip_all_latencies,
              test_burst_read_inorder, test_burst_matches_single,
              test_burst_butterfly_shape, test_overwrite, test_unwritten_reads_zero,
              test_max_outstanding_guard, test_nop_between_transactions]
    print("SeeTheBeat MCU-bus golden-model self-check")
    print("-" * 48)
    ok = 0
    for c in checks:
        try:
            c()
            print("  PASS  {}".format(c.__name__))
            ok += 1
        except AssertionError as e:
            print("  FAIL  {}  --> {}".format(c.__name__, e))
    print("-" * 48)
    print("{}/{} checks passed".format(ok, len(checks)))
    return 0 if ok == len(checks) else 1


if __name__ == "__main__":
    raise SystemExit(_main())
