"""Dependency-free reader for binary and ASCII STL files.

Extracts the geometry facts a review tool needs without pulling in a mesh
library: file format, triangle count, bounding box and (for binary files)
the 80-byte header text. Watertightness is out of scope; establishing it
properly needs real half-edge topology, which is more than a bounding-box
reader should attempt.

Both formats are read streaming rather than loaded whole: a binary file is
read as an 84-byte prologue followed by fixed 50-byte triangle records
consumed in bounded batches, and an ASCII file is read line by line. Either
way, a triangle's vertices fold into a running bounding box and are then
discarded, so a multi-hundred-megabyte STL does not become a matching heap
of Python objects.

Format detection does not rely on the conventional "starts with the token
`solid`" heuristic, because that token is only a convention for ASCII files;
a binary file's 80-byte header is free text and may itself start with
`solid`, which makes the token alone ambiguous. The authoritative check is
arithmetic: a binary file's header carries a 4-byte little-endian triangle
count, and a genuine binary file's size is ``84 + count * 50`` bytes, plus
up to (but not including) one further triangle record's worth of surplus,
which is enough slack for an exporter that pads or newline-terminates the
file without also being enough to hide an undercounted or fabricated
declared count. If the file's size falls in that narrow window, the file
is read as binary regardless of what its header text says; otherwise it is
read as ASCII, carrying the declared count and the size it implies along so
a genuine size/count mismatch is reported with them.

The ASCII reader requires positive structural evidence, not just a leading
`solid` line: every non-blank line's first token must be a keyword from the
STL grammar, each `outer loop` must close with exactly three vertices, each
`facet` must be closed by `endfacet`, and each `solid` must be closed by its
own `endsolid` before another `solid` opens or before EOF. This is what
tells a legitimately empty `solid X` / `endsolid X` pair apart from a
truncated write, an unrelated text file that happens to start with "solid",
or binary triangle data that reached the ASCII reader because its declared
count disagreed with the file's size; it also catches a solid left open
when a later solid in the same file, or EOF, arrives before its `endsolid`.
The raw bytes are also sniffed for C0 control characters, which real STL
text never contains and binary triangle records almost always do, before
any line is interpreted as content; TAB, vertical tab and form feed are
exempt from that sniff, because ASCII STL is a whitespace-delimited grammar
and a real exporter may separate a keyword from its coordinates with a tab
rather than a space. This lets a non-ASCII solid name (UTF-8 or Latin-1)
decode normally while a binary payload is still rejected as binary, with
the declared/expected/actual byte counts in the error when they are known.

The ASCII path never materialises more than one line at a time: the raw
handle is read in fixed-size chunks and split on line feed, with a leading
UTF-8 byte-order mark dropped first so a BOM-writing editor's output is not
mistaken for not-STL content. A "line" that grows past a generous cap with
no line feed in sight is refused outright rather than accumulated without
bound, which is what a large truncated binary STL that reached this path
looks like: grid-aligned float32 data contains long runs with no 0x0A byte.

Every parsed vertex coordinate is checked for finiteness; a NaN or infinite
coordinate raises rather than silently poisoning or being dropped from the
running bounding box, because both of those outcomes would let a
non-finite value cross into a JSON tool result as invalid JSON.

Opening or reading the path can itself raise OSError (for example
FileNotFoundError or IsADirectoryError); this module does not catch or
convert that, so it propagates to the caller unchanged.
"""

import math
import os
import struct
import sys
from pathlib import Path

HEADER_SIZE = 80
COUNT_FIELD_SIZE = 4
BINARY_PROLOGUE_SIZE = HEADER_SIZE + COUNT_FIELD_SIZE  # 84
TRIANGLE_RECORD_SIZE = 50  # 12 little-endian float32 (48 B) + uint16 attribute byte count
_RECORD_STRUCT = struct.Struct("<12fH")

READ_CHUNK_TRIANGLES = 4096  # triangle records per streamed read

# First token of a line the ASCII grammar recognises. Any non-blank line
# whose first token is not one of these is rejected rather than ignored.
_ASCII_KEYWORDS = frozenset(
    {"solid", "facet", "outer", "vertex", "endloop", "endfacet", "endsolid"})


class StlError(Exception):
    """Raised for a truncated, malformed, or unrecognisable STL file."""


def read_stl_facts(path):
    """Return a dict of geometry facts for the STL file at path.

    Keys: ``format`` ("binary" or "ascii"), ``triangles`` (int),
    ``bbox_min`` and ``bbox_max`` (3-tuples of float, or both ``None`` if
    the file has zero triangles and therefore no vertices), ``size_bytes``
    (int), and ``header`` (the binary header text, or ``None`` for ASCII
    files).

    Raises StlError if the file is truncated, declares a triangle count
    that disagrees with its own size, contains a non-finite coordinate, or
    is not recognisable as STL at all. A zero-triangle file is legal STL
    and is returned normally with ``triangles=0`` and both bbox values
    ``None``. OSError from opening or reading the path propagates
    unchanged.
    """
    path = Path(path)
    with path.open("rb") as fh:
        size_bytes = os.fstat(fh.fileno()).st_size
        prologue = fh.read(BINARY_PROLOGUE_SIZE)
        mismatch = None
        if len(prologue) == BINARY_PROLOGUE_SIZE:
            header = prologue[:HEADER_SIZE]
            declared = struct.unpack(
                "<I", prologue[HEADER_SIZE:BINARY_PROLOGUE_SIZE])[0]
            expected_size = BINARY_PROLOGUE_SIZE + declared * TRIANGLE_RECORD_SIZE
            surplus = size_bytes - expected_size
            if 0 <= surplus < TRIANGLE_RECORD_SIZE:
                return _read_binary(fh, declared, header, size_bytes)
            mismatch = (declared, expected_size)
        # Not binary by size (or shorter than the 84-byte prologue). Fall
        # through to the ASCII reader, carrying the declared/expected numbers
        # along so a genuine size/count mismatch is reported with them
        # instead of surfacing only as "this isn't ASCII".
        return _read_ascii(fh, path, size_bytes, mismatch)


class _BBoxTracker:
    """Running bounding box over a stream of vertices, fed one at a time.

    Once the first vertex has seeded both ``min`` and ``max``, a value
    below the running minimum on an axis cannot also be above the running
    maximum on that axis, so a plain if/elif per axis is sufficient; this
    is the invariant the elif below relies on. A non-finite coordinate
    raises immediately rather than seeding or updating the box, so the
    bbox this class reports is always JSON-serialisable.
    """

    def __init__(self):
        self._min = None
        self._max = None

    def update(self, vx, vy, vz):
        if not (math.isfinite(vx) and math.isfinite(vy) and math.isfinite(vz)):
            raise StlError(
                "non-finite vertex coordinate: (%r, %r, %r)" % (vx, vy, vz))
        if self._min is None:
            self._min = [vx, vy, vz]
            self._max = [vx, vy, vz]
            return
        mn, mx = self._min, self._max
        for i, v in enumerate((vx, vy, vz)):
            if v < mn[i]:
                mn[i] = v
            elif v > mx[i]:
                mx[i] = v

    @property
    def bbox_min(self):
        return None if self._min is None else tuple(self._min)

    @property
    def bbox_max(self):
        return None if self._max is None else tuple(self._max)


def _read_binary(fh, declared, header, size_bytes):
    """Stream declared triangle records from a handle positioned after the prologue.

    The caller has already confirmed the file's size falls within one
    triangle record's width of 84 + declared * 50 bytes; any bytes beyond
    that are surplus and are left unread. A short read here can only
    happen if another process truncates the file after its size was
    measured and before these bytes were read; this is a defensive check
    against that race, not the path a well-formed file takes.
    """
    tracker = _BBoxTracker()
    remaining = declared
    processed = 0
    while remaining:
        batch = min(remaining, READ_CHUNK_TRIANGLES)
        chunk = fh.read(batch * TRIANGLE_RECORD_SIZE)
        if len(chunk) != batch * TRIANGLE_RECORD_SIZE:
            present = processed + len(chunk) // TRIANGLE_RECORD_SIZE
            raise StlError(
                "truncated binary STL: header declares %d triangles, only "
                "%d fully present before the data ran out"
                % (declared, present))
        for record in _RECORD_STRUCT.iter_unpack(chunk):
            # record[0:3] is the facet normal, record[3:12] the three
            # vertices; the normal doesn't affect the bounding box.
            for vx, vy, vz in (record[3:6], record[6:9], record[9:12]):
                tracker.update(vx, vy, vz)
            processed += 1
        remaining -= batch

    return {
        "format": "binary",
        "triangles": declared,
        "bbox_min": tracker.bbox_min,
        "bbox_max": tracker.bbox_max,
        "size_bytes": size_bytes,
        "header": _decode_header(header),
    }


def _decode_header(raw_header):
    """Return the free-form 80-byte header field as a safe display string.

    The field is cut at the first NUL, the conventional terminator
    exporters use, and decoded as UTF-8 with substitution rather than
    strict ASCII, so a non-ASCII author name or comment survives instead
    of turning into replacement-character noise. Any C0 control character
    or DEL is then collapsed to a single space: a hostile header could
    otherwise carry terminal escape sequences or embedded newlines into
    text this module writes to stdout or hands to a model as a tool
    result.
    """
    text = raw_header.split(b"\x00", 1)[0].decode("utf-8", "replace")
    sanitized = []
    space_pending = False
    for ch in text:
        if ord(ch) < 0x20 or ord(ch) == 0x7F:
            space_pending = True
            continue
        if space_pending:
            if sanitized and sanitized[-1] != " ":
                sanitized.append(" ")
            space_pending = False
        sanitized.append(ch)
    return "".join(sanitized).strip()


def _binary_payload_message(path, size_bytes, mismatch):
    """Return the error text for ASCII content rejected as binary payload.

    When ``mismatch`` is known (the file had a full 84-byte prologue whose
    declared count did not match the file's actual size), the numbers that
    let a user act, declared count, the size it implies, and the actual
    size, are reported directly rather than thrown away.
    """
    if mismatch is None:
        return (
            "%s is not valid ASCII STL text (contains control bytes) and "
            "is not sized as a binary STL either" % path)
    declared, expected_size = mismatch
    return (
        "%s declares %d triangles, which implies %d bytes, but the file "
        "is %d bytes; truncated or corrupt binary STL"
        % (path, declared, expected_size, size_bytes))


def _has_control_byte(text):
    """Return True if text contains a byte real STL text never carries.

    TAB, vertical tab and form feed are exempt: ASCII STL is a
    whitespace-delimited grammar, so a tab between a keyword and its
    coordinates is legitimate whitespace, not evidence of a binary
    payload. CR and LF never reach this check because the line has
    already been split on LF and stripped of any trailing CR.
    """
    return any(
        (ord(ch) < 0x20 and ch not in "\t\v\f") or ord(ch) == 0x7F
        for ch in text)


_ASCII_READ_CHUNK_BYTES = 65536
_MAX_ASCII_LINE_BYTES = 1 << 20  # no real STL text line is remotely this long
_UTF8_BOM = b"\xef\xbb\xbf"


def _iter_ascii_lines(fh, path):
    """Yield successive newline-delimited lines read from fh in bounded chunks.

    fh is read _ASCII_READ_CHUNK_BYTES bytes at a time rather than handed to
    a line-buffering text wrapper, so a file with no line feed anywhere
    (the shape a large truncated binary STL takes, since grid-aligned
    float32 data rarely contains a 0x0A byte) cannot be materialised whole
    before it is rejected. A line that grows past _MAX_ASCII_LINE_BYTES
    with still no line feed found is refused directly instead of being
    accumulated without bound. Each yielded line is decoded as Latin-1,
    which maps every byte to a character and never fails, so a non-ASCII
    solid name (UTF-8 or Latin-1 encoded) decodes without special-casing;
    a trailing CR from a CRLF file survives into the yielded line and is
    removed later by the caller's own `.strip()`.
    """
    pending = b""
    while True:
        chunk = fh.read(_ASCII_READ_CHUNK_BYTES)
        if not chunk:
            break
        pending += chunk
        start = 0
        while True:
            index = pending.find(b"\n", start)
            if index == -1:
                break
            yield pending[start:index].decode("latin-1")
            start = index + 1
        pending = pending[start:]
        if len(pending) > _MAX_ASCII_LINE_BYTES:
            raise StlError(
                "not ASCII STL: %s has a line longer than %d bytes with no "
                "line feed in sight; likely truncated binary STL data that "
                "reached the ASCII reader" % (path, _MAX_ASCII_LINE_BYTES))
    if pending:
        yield pending.decode("latin-1")


def _read_ascii(fh, path, size_bytes, mismatch):
    """Parse an ASCII STL by streaming decoded lines from the open handle.

    Structure is tracked while streaming rather than only counting `facet`
    lines, so a truncated write is caught rather than reported as a
    plausible but wrong triangle count: each `outer loop` must close with
    exactly three vertices, each `facet` must be closed by `endfacet`
    before another one opens, and each `solid` must be closed by its own
    `endsolid` before another `solid` opens or before EOF. Multiple
    `solid`/`endsolid` pairs in one file are legal STL and are folded into
    one combined triangle count and bounding box.
    """
    fh.seek(0)
    if fh.read(len(_UTF8_BOM)) != _UTF8_BOM:
        fh.seek(0)

    tracker = _BBoxTracker()
    triangles = 0
    first_line = None
    in_solid = False
    in_facet = False
    in_loop = False
    loop_vertices = 0

    for line in _iter_ascii_lines(fh, path):
        stripped = line.strip()
        if not stripped:
            continue
        if _has_control_byte(stripped):
            raise StlError(_binary_payload_message(path, size_bytes, mismatch))

        if first_line is None:
            first_line = stripped
            if first_line.split()[0].lower() != "solid":
                raise StlError(
                    "not a recognisable STL file: %s (%d bytes; does not "
                    "match a binary STL by size, and does not start with "
                    "the 'solid' keyword)" % (path, size_bytes))
            in_solid = True
            continue

        parts = stripped.split()
        keyword = parts[0].lower()
        if keyword not in _ASCII_KEYWORDS:
            raise StlError(
                "malformed ASCII STL: unrecognised line in %s: %r"
                % (path, stripped))

        if keyword == "solid":
            if in_facet or in_loop:
                raise StlError(
                    "malformed ASCII STL: 'solid' inside an open facet: %r"
                    % stripped)
            if in_solid:
                raise StlError(
                    "malformed ASCII STL: 'solid' opened before the "
                    "previous solid was closed with 'endsolid': %r"
                    % stripped)
            in_solid = True
        elif keyword == "facet":
            if in_facet:
                raise StlError(
                    "malformed ASCII STL: nested facet, missing endfacet: %r"
                    % stripped)
            in_facet = True
        elif keyword == "outer":
            if not in_facet or in_loop:
                raise StlError(
                    "malformed ASCII STL: 'outer loop' outside a facet: %r"
                    % stripped)
            in_loop = True
            loop_vertices = 0
        elif keyword == "vertex":
            if not in_loop:
                raise StlError(
                    "malformed ASCII STL: vertex outside outer loop: %r"
                    % stripped)
            if len(parts) < 4:
                raise StlError(
                    "malformed ASCII STL: vertex line missing coordinates: %r"
                    % stripped)
            try:
                vx, vy, vz = (float(parts[1]), float(parts[2]), float(parts[3]))
            except ValueError as exc:
                raise StlError(
                    "malformed ASCII STL: non-numeric vertex: %r"
                    % stripped) from exc
            tracker.update(vx, vy, vz)
            loop_vertices += 1
        elif keyword == "endloop":
            if not in_loop:
                raise StlError(
                    "malformed ASCII STL: endloop without outer loop: %r"
                    % stripped)
            if loop_vertices != 3:
                raise StlError(
                    "malformed ASCII STL: facet loop has %d vertices, "
                    "expected 3: %r" % (loop_vertices, stripped))
            in_loop = False
        elif keyword == "endfacet":
            if not in_facet or in_loop:
                raise StlError(
                    "malformed ASCII STL: endfacet without a closed loop: %r"
                    % stripped)
            in_facet = False
            triangles += 1
        elif keyword == "endsolid":
            if in_facet or in_loop:
                raise StlError(
                    "malformed ASCII STL: endsolid with a facet still "
                    "open: %r" % stripped)
            if not in_solid:
                raise StlError(
                    "malformed ASCII STL: 'endsolid' without a matching "
                    "'solid': %r" % stripped)
            in_solid = False

    if first_line is None:
        raise StlError("empty file, not a recognisable STL: %s" % path)
    if in_facet or in_loop:
        raise StlError(
            "truncated ASCII STL: %s ends with a facet still open" % path)
    if in_solid:
        raise StlError(
            "truncated ASCII STL: %s has no closing 'endsolid'" % path)

    return {
        "format": "ascii",
        "triangles": triangles,
        "bbox_min": tracker.bbox_min,
        "bbox_max": tracker.bbox_max,
        "size_bytes": size_bytes,
        "header": None,
    }


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        sys.stderr.write("usage: python -m annealage_mesh.stl <file.stl>\n")
        return 2

    try:
        facts = read_stl_facts(argv[0])
    except (StlError, OSError) as exc:
        sys.stderr.write("error: %s\n" % exc)
        return 1

    sys.stdout.write("format:     %s\n" % facts["format"])
    sys.stdout.write("triangles:  %d\n" % facts["triangles"])
    if facts["bbox_min"] is None:
        sys.stdout.write("bbox:       (no geometry, zero triangles)\n")
    else:
        sys.stdout.write("bbox_min:   (%.4f, %.4f, %.4f)\n" % facts["bbox_min"])
        sys.stdout.write("bbox_max:   (%.4f, %.4f, %.4f)\n" % facts["bbox_max"])
    sys.stdout.write("size_bytes: %d\n" % facts["size_bytes"])
    if facts["header"] is not None:
        sys.stdout.write("header:     %s\n" % facts["header"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
