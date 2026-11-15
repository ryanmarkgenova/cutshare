"""FCPXML rational time helpers.

FCPXML stores time as rational seconds, e.g. "1001/30000s" or "5s".
We keep everything as fractions.Fraction so nothing drifts off frame.
"""
from fractions import Fraction


def parse_time(value):
    if value is None or value == "":
        return Fraction(0)
    s = value.strip()
    if s.endswith("s"):
        s = s[:-1]
    if "/" in s:
        num, den = s.split("/", 1)
        return Fraction(int(num), int(den))
    return Fraction(s)


def fmt_time(value, base_den=None):
    """Format a Fraction as FCPXML time. If base_den is given (the sequence
    timebase, e.g. 30000 for 29.97), keep that denominator like FCP does."""
    f = Fraction(value)
    if f == 0:
        return "0s"
    if base_den:
        scaled = f * base_den
        if scaled.denominator == 1:
            if scaled.numerator % base_den == 0:
                return f"{scaled.numerator // base_den}s"
            return f"{scaled.numerator}/{base_den}s"
    if f.denominator == 1:
        return f"{f.numerator}s"
    return f"{f.numerator}/{f.denominator}s"


def human(value):
    """Seconds -> 'm:ss.s' for people."""
    f = float(value)
    sign = "-" if f < 0 else ""
    f = abs(f)
    m, s = divmod(f, 60)
    return f"{sign}{int(m)}:{s:04.1f}"


def parse_clock(text):
    """'1:05.5' or '65.5' or '0:01:05' -> Fraction seconds (for --at arguments)."""
    parts = text.strip().split(":")
    total = Fraction(0)
    for p in parts:
        total = total * 60 + Fraction(p)
    return total
