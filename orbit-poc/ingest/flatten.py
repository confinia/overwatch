"""Flatten a decoded kaitai telemetry object into {field_path: number} (#171).

Its own module so it can be tested without importing the whole ingest service
(which needs a database at import time) and without satnogs-decoders installed.

The depth bound exists to stop runaway recursion, not to bound real payloads —
and it used to do the second thing by accident. AX.25-wrapped decoders nest
their telemetry in per-subsystem structs, one level below where the old cutoff
of 4 stopped: SAL-E's cp16 frames yielded 7 fields instead of 121, and ingest
logged "19/25 frames decoded+stored" while storing almost nothing. Measured on
a live frame, cp16 saturates at depth 6; 8 keeps headroom for deeper decoders
while still bounding the walk, and a visited set makes cycles impossible
regardless of the number.
"""

MAX_DEPTH = 8

# kaitai objects carry _parent/_root back-references — skipped by the leading
# underscore rule below, but the visited set means a decoder exposing a public
# back-reference cannot loop either.
_DECODER_MODULE = "satnogsdecoders"


def flatten_decoded(obj, max_depth: int = MAX_DEPTH, module: str = _DECODER_MODULE):
    """Walk a decoded frame and collect every numeric leaf, keyed by its path.

    `module` is the package prefix that marks an object as worth descending
    into; it is a parameter only so tests can walk stand-ins.
    """
    out: dict[str, float] = {}
    seen: set[int] = set()

    def walk(o, prefix: str = "", depth: int = 0) -> None:
        if depth > max_depth or id(o) in seen:
            return
        seen.add(id(o))
        for name in dir(o):
            if name.startswith("_"):
                continue
            try:
                value = getattr(o, name)
            except Exception:
                continue                      # kaitai lazy props can raise
            if isinstance(value, bool):
                continue                      # bools are ints; not telemetry
            if isinstance(value, (int, float)):
                out[prefix + name] = value
            elif getattr(value.__class__, "__module__", "").startswith(module):
                walk(value, prefix + name + "_", depth + 1)

    walk(obj)
    return out
