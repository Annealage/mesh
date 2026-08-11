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
up to two bytes of surplus, enough slack for an exporter that
newline- or CRLF-terminates the file, but not enough to hide an
undercounted or fabricated declared count or to mistake an arbitrary
non-STL file of about the right size for one. If the file's size falls
in that narrow window, the file is read as binary regardless of what its
header text says; otherwise it is read as ASCII, carrying the declared
count and the size it implies along so a genuine size/count mismatch is
reported with them.

The ASCII reader requires positive structural evidence, not just a leading
`solid` line: every non-blank line's first token must be a keyword from the
STL grammar, every `facet` must open inside an already-open `solid`, each
`outer loop` must close with exactly three vertices, each `facet` must
contain exactly one such loop and be closed by `endfacet`, and each `solid`
must be closed by its own `endsolid` before another `solid` opens or before
EOF. This is what tells a legitimately empty `solid X` / `endsolid X` pair
apart from a truncated write, an unrelated text file that happens to start
with "solid", or binary triangle data that reached the ASCII reader because
its declared count disagreed with the file's size; it also catches a solid
left open when a later solid in the same file, or EOF, arrives before its
`endsolid`, a facet whose loop body was lost mid-write, and geometry that
appears after its enclosing solid has already closed.
The raw bytes are also sniffed for C0 control bytes (0x00-0x1F and DEL),
which real STL text never contains and binary triangle records almost
always do, before any line is interpreted as content; TAB is exempt from
that sniff, because ASCII STL is a whitespace-delimited grammar and a real
exporter may separate a keyword from its coordinates with a tab rather
than a space. Bytes at or above 0x80 are never treated as evidence of a
binary payload: lines are decoded Latin-1, so a C1 control byte is
indistinguishable from a UTF-8 continuation byte, and many ordinary
characters encode with one, for example U+0142 as C5 82 and U+4E00 as
E4 B8 80. This lets a non-ASCII solid name (UTF-8 or
Latin-1) decode normally while a binary payload is still rejected as
binary.

The declared/expected/actual byte counts are attached to that rejection on
one condition only: the 50 bytes following the prologue unpack as a
triangle record, meaning a zero attribute count and twelve finite float32
values whose nonzero magnitudes fall inside a plausible coordinate window.
Nothing else votes. Neither the header nor the 4-byte count field is
evidence, both being free text as far as this decision goes, and no
statistical test over the surrounding bytes is used, because every such
test classifies some text encoding as binary: Latin-1 accents and UTF-8
continuation bytes are not printable ASCII, and UTF-16 and UTF-32 pad
every character with NULs, so a byte-ratio threshold tuned to catch
float32 data catches those too. A text file told it declares 158 million
triangles is a worse outcome than one told only that it is not STL, so
when the evidence is absent the message says just that.

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

# Surplus beyond the exact 84 + declared * 50 byte size that is still
# treated as trailing padding rather than a mismatch. 2 bytes is exactly
# enough to cover a trailing "\n" or "\r\n". Every additional byte of
# allowance would widen, linearly, the range of file sizes within which an
# arbitrary non-STL file's random 32-bit count field could coincidentally
# land, without making that count field any more likely to be genuine.
_BINARY_SURPLUS_MAX = 2

# A coordinate window wide enough for any model a person authors, in any
# unit they would use, and narrow enough to exclude the magnitudes that
# text bytes produce when read as float32: prose gives about 1e-19 or
# 1e38, and a wide encoding's padding NULs give denormals about 1e-44.
_COORD_MIN_MAGNITUDE = 1e-6
_COORD_MAX_MAGNITUDE = 1e9

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
            count_field = prologue[HEADER_SIZE:BINARY_PROLOGUE_SIZE]
            declared = struct.unpack("<I", count_field)[0]
            expected_size = BINARY_PROLOGUE_SIZE + declared * TRIANGLE_RECORD_SIZE
            surplus = size_bytes - expected_size
            # A file declaring no triangles has no payload to corroborate
            # it, so the only thing making it an STL is four NUL bytes at
            # offset 80. Requiring an exact 84 bytes for that case keeps an
            # unrelated 85 or 86 byte file with NULs there from being
            # reported as a valid empty model. A real zero-triangle export
            # is exactly 84 bytes.
            padding_allowed = 0 if declared == 0 else _BINARY_SURPLUS_MAX
            if 0 <= surplus <= padding_allowed:
                return _read_binary(fh, declared, header, size_bytes)
            # One question decides whether the declared/expected numbers are
            # worth repeating back: does the first record actually look like
            # a triangle. Statistical tests over the surrounding bytes were
            # tried and abandoned, because every one of them classifies some
            # text encoding as binary (Latin-1 accents, UTF-8 continuation
            # bytes, UTF-16 and UTF-32 padding NULs all read as payload),
            # and a text file told it declares 158 million triangles is
            # worse than one told only that it is not STL.
            sample = fh.read(TRIANGLE_RECORD_SIZE)
            if _first_record_parses_as_a_triangle(sample):
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

    The caller has already confirmed the file's size is within
    _BINARY_SURPLUS_MAX bytes of 84 + declared * 50 bytes; any bytes
    beyond that are surplus and are left unread. A short read here can
    only happen if another process truncates the file after its size was
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
    of turning into replacement-character noise. Any C0 or C1 control
    character, DEL, line/paragraph separator, or bidirectional text
    control (the explicit embeddings and overrides U+202A-U+202E and the
    isolates U+2066-U+2069) is then collapsed to a single space: a hostile
    header could otherwise carry terminal escape sequences (CSI and OSC
    are single C1 characters, U+009B and U+009D, as well as the two-byte
    ESC forms), embedded newlines (including NEL, U+0085), or visually
    reordered text into output this module writes to stdout or hands to a
    model as a tool result.
    """
    text = raw_header.split(b"\x00", 1)[0].decode("utf-8", "replace")
    sanitized = []
    space_pending = False
    for ch in text:
        code = ord(ch)
        if (code < 0x20 or code == 0x7F or 0x80 <= code <= 0x9F
                or code in (0x2028, 0x2029)
                or 0x202A <= code <= 0x202E
                or 0x2066 <= code <= 0x2069):
            space_pending = True
            continue
        if space_pending:
            if sanitized and sanitized[-1] != " ":
                sanitized.append(" ")
            space_pending = False
        sanitized.append(ch)
    return "".join(sanitized).strip()


def _control_byte_rejection_message(path, size_bytes, mismatch):
    """Return the error text for content rejected because it contains control bytes.

    When ``mismatch`` is known (the file had a full 84-byte prologue whose
    declared count did not match the file's actual size, and the bytes
    after that prologue looked positively binary), the numbers that let a
    user act, declared count, the size it implies, and the actual size, are
    reported directly rather than thrown away; the wording distinguishes a
    file shorter than its declared size, which is truncation, from one
    longer than it, which is not. When ``mismatch`` is None, either the
    file was too short to carry a binary prologue at all, or it carried one
    the following bytes gave no reason to believe, in which case the counts
    would be an artefact of reading four bytes of prose as a little-endian
    integer, and the message reports only the control bytes.
    """
    if mismatch is None:
        return (
            "%s is not valid ASCII STL text (contains control bytes)"
            % path)
    declared, expected_size = mismatch
    if size_bytes > expected_size:
        # Deliberately does not claim the declared records are all present
        # and parseable, because nothing here has read them; the only
        # established facts are the declared count, the size it implies and
        # the size on disk.
        return (
            "%s declares %d triangles, which implies %d bytes, but the "
            "file is %d bytes; more data than the declared triangle count "
            "accounts for" % (path, declared, expected_size, size_bytes))
    return (
        "%s declares %d triangles, which implies %d bytes, but the file "
        "is %d bytes; truncated or corrupt binary STL"
        % (path, declared, expected_size, size_bytes))


def _has_control_byte(text):
    """Return True if text contains a byte real STL text never carries.

    Only the C0 range and DEL count. TAB is exempt: ASCII STL is a
    whitespace-delimited grammar, so a tab between a keyword and its
    coordinates is legitimate whitespace, not evidence of a binary
    payload.

    Bytes at or above 0x80 are deliberately not checked. The line reaching
    this function was decoded Latin-1, so a C1 control byte cannot be told
    apart from a UTF-8 continuation byte, and continuation bytes in the
    0x80-0x9F range occur in ordinary text: U+0142 encodes as C5 82 and
    U+4E00 as E4 B8 80. Treating that range as a binary marker would
    reject a valid ASCII STL whose solid name is simply not English. The
    cost of leaving it out is that a stray C1 byte inside otherwise valid
    text is tolerated rather than reported, which is the better failure to
    have.

    LF never reaches this check because the line has already been split on
    it; only a trailing CR, the artefact of a CRLF split, is stripped by
    the caller before this check runs, so a CR anywhere else in the line,
    for example one from a file that uses bare CR as its line ending, is
    caught here like any other control byte.
    """
    return any(
        (ord(ch) < 0x20 and ch != "\t") or ord(ch) == 0x7F
        for ch in text)


def _first_record_parses_as_a_triangle(sample):
    """Return True if sample opens with bytes shaped like a triangle record.

    A binary triangle record is twelve little-endian float32 values then a
    two-byte attribute count. Three things must hold, and together they are
    narrow enough to be the only evidence the caller needs.

    The attribute count must be zero, which real exporters write and which
    prose read at that offset fails, because those two bytes land on
    ordinary text characters.

    Every value must be finite, so a record carrying NaN or an infinity is
    not offered as evidence for a file this reader would refuse anyway.

    Every nonzero coordinate must fall between _COORD_MIN_MAGNITUDE and
    _COORD_MAX_MAGNITUDE. This clause does the real work, and it is a
    statement about physical geometry rather than about byte statistics: a
    model measured in any unit a person uses sits inside that window, while
    bytes that are really text decode far outside it. Latin-1 and UTF-8
    prose yields magnitudes around 1e-19 or 1e38, and a wide encoding's
    padding NULs yield denormals around 1e-44, since UTF-32 puts three NUL
    bytes in every four. Exact zero is admitted, because real models sit on
    their axes.

    At least one of the twelve must be nonzero, so a run of NUL bytes is not
    read as a triangle. All twelve zeros describes a zero normal and three
    identical vertices at the origin, which no real model opens with, and
    admitting it hands a fabricated triangle count to any file carrying a
    stretch of NULs at that offset, as several speech-synthesis data files
    on an ordinary Linux system do.

    This decides only which of two error messages a rejected file gets, and
    both of them raise, so a conservative answer is the right kind of wrong.
    An exporter that packs colour into the attribute field gets the less
    specific message, which is a better outcome than a text file being told
    it declares 158 million triangles.
    """
    if len(sample) < TRIANGLE_RECORD_SIZE:
        return False
    values = struct.unpack("<12fH", sample[:TRIANGLE_RECORD_SIZE])
    if values[12] != 0:
        return False
    nonzero = 0
    for value in values[:12]:
        if not math.isfinite(value):
            return False
        magnitude = abs(value)
        if magnitude == 0.0:
            continue
        if not (_COORD_MIN_MAGNITUDE <= magnitude <= _COORD_MAX_MAGNITUDE):
            return False
        nonzero += 1
    return nonzero > 0


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
    a trailing CR from a CRLF file survives into the yielded line, and it
    is the caller's job to trim it before treating the line as content.
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
    exactly three vertices, each `facet` must contain exactly one such
    loop and be closed by `endfacet`, each `facet` must itself be inside
    an open `solid`, and each `solid` must be closed by its own `endsolid`
    before another `solid` opens or before EOF. Multiple `solid`/`endsolid`
    pairs in one file are legal STL and are folded into one combined
    triangle count and bounding box.
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
    facet_loops = 0

    for line in _iter_ascii_lines(fh, path):
        # Only a trailing CR, the artefact of a CRLF split, is trimmed
        # before this check; a full `.strip()` would also silently absorb
        # a leading or trailing control byte that Python's Unicode
        # whitespace rules treat as space, for example U+001C to U+001F,
        # hiding it from `_has_control_byte`.
        control_check_line = line[:-1] if line.endswith("\r") else line
        if _has_control_byte(control_check_line):
            raise StlError(
                _control_byte_rejection_message(path, size_bytes, mismatch))
        stripped = line.strip()
        if not stripped:
            continue

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
            if not in_solid:
                raise StlError(
                    "malformed ASCII STL: 'facet' outside any solid: %r"
                    % stripped)
            if in_facet:
                raise StlError(
                    "malformed ASCII STL: nested facet, missing endfacet: %r"
                    % stripped)
            in_facet = True
            facet_loops = 0
        elif keyword == "outer":
            if not in_facet or in_loop:
                raise StlError(
                    "malformed ASCII STL: 'outer loop' outside a facet: %r"
                    % stripped)
            if len(parts) < 2 or parts[1].lower() != "loop":
                raise StlError(
                    "malformed ASCII STL: 'outer' not followed by 'loop': %r"
                    % stripped)
            in_loop = True
            loop_vertices = 0
        elif keyword == "vertex":
            if not in_loop:
                raise StlError(
                    "malformed ASCII STL: vertex outside outer loop: %r"
                    % stripped)
            if len(parts) != 4:
                raise StlError(
                    "malformed ASCII STL: vertex line must have exactly 3 "
                    "coordinates: %r" % stripped)
            # A bare float() call accepts PEP 515 underscore digit
            # separators ("1_0" -> 10.0), so a coordinate token containing
            # one is rejected outright rather than silently parsed to a
            # different number.
            if any("_" in token for token in parts[1:4]):
                raise StlError(
                    "malformed ASCII STL: non-numeric vertex: %r" % stripped)
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
            facet_loops += 1
        elif keyword == "endfacet":
            if not in_facet or in_loop:
                raise StlError(
                    "malformed ASCII STL: endfacet without a closed loop: %r"
                    % stripped)
            if facet_loops != 1:
                raise StlError(
                    "malformed ASCII STL: facet has %d outer loops, "
                    "expected exactly 1: %r" % (facet_loops, stripped))
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
        # _decode_header only removes control characters; the remaining
        # text may still contain characters stdout's own encoding cannot
        # represent (for example a non-ASCII author name under a strict
        # PYTHONIOENCODING). Replacing them here, rather than letting the
        # write raise, keeps a valid STL's report from failing partway
        # through over a display-only detail.
        encoding = sys.stdout.encoding or "utf-8"
        displayable_header = facts["header"].encode(encoding, "replace").decode(encoding)
        sys.stdout.write("header:     %s\n" % displayable_header)
    return 0


if __name__ == "__main__":
    sys.exit(main())
