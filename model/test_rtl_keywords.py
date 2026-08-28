# SPDX-FileCopyrightText: © 2026 Jonas Creyns, Giel Swenters
# SPDX-License-Identifier: Apache-2.0
"""
Guard: no RTL signal, parameter or module may be named after a Verilog reserved word.

Why this exists (2026-08-27): `src/pixel_gen.v` declared `wire [4:0] tri`, and `tri` is a
Verilog net type. Both Verilator and Icarus rejected the file outright. It cost a full CI
round-trip to find something a text scan catches in milliseconds.

The real gap it plugs: there is NO Verilog compiler on the Windows host, so nothing catches
a syntax error before CI. This runs in the fast `golden-model` job -- and, more usefully,
can be run locally before pushing. It is not a substitute for lint; it is a cheap pre-flight
for the one mistake that is invisible without a parser.
"""

import io
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "..", "src")

# IEEE 1364-2005 reserved words. Names people actually trip over are marked.
RESERVED = set("""
always and assign automatic begin buf bufif0 bufif1 case casex casez cell cmos config
deassign default defparam design disable edge else end endcase endconfig endfunction
endgenerate endmodule endprimitive endspecify endtable endtask event for force forever
fork function generate genvar highz0 highz1 if ifnone incdir include initial inout input
instance integer join large liblist library localparam macromodule medium module nand
negedge nmos nor noshowcancelled not notif0 notif1 or output parameter pmos posedge
primitive pull0 pull1 pulldown pullup pulsestyle_onevent pulsestyle_ondetect rcmos real
realtime reg release repeat rnmos rpmos rtran rtranif0 rtranif1 scalared showcancelled
signed small specify specparam strong0 strong1 supply0 supply1 table task time tran
tranif0 tranif1 tri tri0 tri1 triand trior trireg unsigned use uwire vectored wait wand
weak0 weak1 while wire wor xnor xor
""".split())

# Declarations we care about: nets, variables, parameters, ports, module names.
DECL = re.compile(
    r"\b(?:wire|reg|logic|integer|real|localparam|parameter|input|output|inout|genvar|module)\b"
    r"(?:\s+(?:signed|unsigned|integer|wire|reg))*"        # optional type/sign words
    r"(?:\s*\[[^\]]*\])*"                                   # optional packed dimensions
    r"\s+([A-Za-z_][A-Za-z0-9_$]*)")


def _strip(text):
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
    text = re.sub(r"//[^\n]*", " ", text)
    return re.sub(r'"[^"\n]*"', " ", text)


def _scan(path):
    """Return [(line_no, name)] for declarations whose identifier is reserved."""
    raw = io.open(path, encoding="utf-8").read()
    clean = _strip(raw)
    bad = []
    for m in DECL.finditer(clean):
        name = m.group(1)
        if name in RESERVED:
            line = clean[:m.start(1)].count("\n") + 1
            bad.append((line, name))
    return bad


def test_no_reserved_words_as_identifiers():
    files = sorted(
        [os.path.join(SRC, f) for f in os.listdir(SRC) if f.endswith(".v")] +
        [os.path.join(SRC, "attic", f)
         for f in os.listdir(os.path.join(SRC, "attic")) if f.endswith(".v")])
    problems = []
    for path in files:
        for line, name in _scan(path):
            problems.append("{}:{}: '{}' is a Verilog reserved word".format(
                os.path.relpath(path, os.path.join(HERE, "..")), line, name))
    assert not problems, "reserved words used as identifiers:\n  " + "\n  ".join(problems)


def test_the_guard_actually_catches_it():
    """A guard that cannot fail is worthless -- prove it fires on the real bug."""
    import tempfile
    src = "module m;\n  wire [4:0] tri = 1;\n  reg time;\nendmodule\n"
    fd, tmp = tempfile.mkstemp(suffix=".v")
    os.close(fd)
    try:
        io.open(tmp, "w", encoding="utf-8").write(src)
        found = {n for _, n in _scan(tmp)}
        assert "tri" in found, "guard missed `wire tri` -- the exact 2026-08-27 bug"
        assert "time" in found, "guard missed `reg time`"
    finally:
        os.unlink(tmp)


def test_guard_does_not_flag_legitimate_names():
    """It must not fire on ordinary signals, or it will just get waived away."""
    import tempfile
    src = ("module m #(parameter WIDTH = 8) (input wire clk, output reg [1:0] wire_count);\n"
           "  wire [4:0] tri_wave = 0;\n  localparam TRIANGLE = 3;\n  reg timer;\nendmodule\n")
    fd, tmp = tempfile.mkstemp(suffix=".v")
    os.close(fd)
    try:
        io.open(tmp, "w", encoding="utf-8").write(src)
        assert _scan(tmp) == [], "false positive on legitimate names: {}".format(_scan(tmp))
    finally:
        os.unlink(tmp)


def _main():
    checks = [test_no_reserved_words_as_identifiers,
              test_the_guard_actually_catches_it,
              test_guard_does_not_flag_legitimate_names]
    print("SeeTheBeat RTL reserved-word check")
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
