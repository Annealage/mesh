"""Tests for the dependency-free STL reader."""

import io
import os
import struct
import subprocess
import sys

import pytest

from annealage_mesh.stl import (
    _ASCII_READ_CHUNK_BYTES,
    BINARY_PROLOGUE_SIZE,
    HEADER_SIZE,
    TRIANGLE_RECORD_SIZE,
    StlError,
    _iter_ascii_lines,
    _read_binary,
    read_stl_facts,
)


def _make_binary_stl(header_text, triangles):
    """Return the bytes of a valid binary STL: an 80-byte header (from header_text, NUL-padded), a little-endian triangle count, then one 50-byte record per entry in triangles (each a sequence of 12 floats: normal then three vertices)."""
    header_bytes = header_text.encode("ascii")[:HEADER_SIZE]
    header_bytes = header_bytes + b"\x00" * (HEADER_SIZE - len(header_bytes))

    data = header_bytes + struct.pack("<I", len(triangles))
    for triangle in triangles:
        data += struct.pack("<12fH", *triangle, 0)
    return data


def _make_ascii_stl(solid_name, facets):
    """Return the bytes of a valid ASCII STL with one solid named solid_name, containing one facet per entry in facets (each a sequence of three (x, y, z) vertices)."""
    lines = ["solid %s" % solid_name]
    for facet in facets:
        lines.append("  facet normal 0 0 1")
        lines.append("    outer loop")
        for vertex in facet:
            lines.append("      vertex %r %r %r" % vertex)
        lines.append("    endloop")
        lines.append("  endfacet")
    lines.append("endsolid %s" % solid_name)
    return "\n".join(lines).encode("ascii") + b"\n"


def test_valid_binary_stl_with_two_triangles(tmp_path):
    stl_file = tmp_path / "test.stl"

    # Triangle 1: normal (0,0,1), vertices at (0,0,0), (1,0,0), (0,1,0)
    # Triangle 2: normal (0,0,1), vertices at (1,1,0), (2,1,0), (1,2,0)
    triangles = [
        [0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 1, 0],
        [0, 0, 1, 1, 1, 0, 2, 1, 0, 1, 2, 0],
    ]
    stl_file.write_bytes(_make_binary_stl("test header", triangles))

    facts = read_stl_facts(stl_file)
    assert facts["format"] == "binary"
    assert facts["triangles"] == 2
    assert facts["bbox_min"] == (0.0, 0.0, 0.0)
    assert facts["bbox_max"] == (2.0, 2.0, 0.0)
    assert facts["size_bytes"] == BINARY_PROLOGUE_SIZE + 2 * TRIANGLE_RECORD_SIZE
    assert isinstance(facts["header"], str)
    assert "test header" in facts["header"]


def test_valid_ascii_stl_with_two_triangles(tmp_path):
    stl_file = tmp_path / "test.stl"

    facets = [
        [(0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0)],
        [(1.0, 1.0, 0.0), (2.0, 1.0, 0.0), (1.0, 2.0, 0.0)],
    ]
    stl_file.write_bytes(_make_ascii_stl("test", facets))

    facts = read_stl_facts(stl_file)
    assert facts["format"] == "ascii"
    assert facts["triangles"] == 2
    assert facts["bbox_min"] == (0.0, 0.0, 0.0)
    assert facts["bbox_max"] == (2.0, 2.0, 0.0)
    assert facts["header"] is None


def test_binary_stl_with_solid_header_detected_as_binary(tmp_path):
    """A header that happens to start with the ASCII token 'solid' must not be misread as ASCII content."""
    stl_file = tmp_path / "test.stl"

    triangles = [
        [0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 1, 0],
    ]
    stl_file.write_bytes(_make_binary_stl("solid widget model v1", triangles))

    facts = read_stl_facts(stl_file)
    assert facts["format"] == "binary"
    assert facts["triangles"] == 1
    assert facts["header"].startswith("solid")


def test_binary_stl_with_trailing_padding_is_still_binary(tmp_path):
    """Bytes appended after a complete binary STL (padding, a trailing newline) are surplus, not truncation."""
    stl_file = tmp_path / "test.stl"

    triangles = [
        [0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 1, 0],
    ]
    data = _make_binary_stl("test", triangles) + b"\n"
    stl_file.write_bytes(data)

    facts = read_stl_facts(stl_file)
    assert facts["format"] == "binary"
    assert facts["triangles"] == 1
    assert facts["bbox_max"] == (1.0, 1.0, 0.0)


def test_binary_stl_with_trailing_crlf_is_still_binary(tmp_path):
    """A trailing CRLF, as a Windows text editor might append, is still surplus within the narrow padding window, not truncation."""
    stl_file = tmp_path / "test.stl"

    triangles = [
        [0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 1, 0],
    ]
    data = _make_binary_stl("test", triangles) + b"\r\n"
    stl_file.write_bytes(data)

    facts = read_stl_facts(stl_file)
    assert facts["format"] == "binary"
    assert facts["triangles"] == 1


def test_binary_stl_with_a_single_trailing_nul_is_still_binary(tmp_path):
    """A single trailing NUL byte is still within the narrow padding window."""
    stl_file = tmp_path / "test.stl"

    triangles = [
        [0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 1, 0],
    ]
    data = _make_binary_stl("test", triangles) + b"\x00"
    stl_file.write_bytes(data)

    facts = read_stl_facts(stl_file)
    assert facts["format"] == "binary"
    assert facts["triangles"] == 1


def test_binary_stl_with_surplus_beyond_the_narrow_window_is_rejected(tmp_path):
    """Surplus bytes wider than a trailing newline are not read as an exporter's padding; a wide allowance would let an arbitrary non-STL file of about the right size pass as a valid, empty binary STL."""
    stl_file = tmp_path / "test.stl"

    header = b"h" * HEADER_SIZE
    # Declares zero triangles (expected size 84 bytes) plus 30 bytes of
    # unrelated surplus, well past the narrow padding window.
    data = header + struct.pack("<I", 0) + b"\xa5" * 30
    stl_file.write_bytes(data)

    with pytest.raises(StlError):
        read_stl_facts(stl_file)


def test_truncated_binary_file_raises_error_with_the_byte_counts(tmp_path):
    """A declared count that disagrees with the file's actual size is reported with the declared, implied and actual byte counts."""
    stl_file = tmp_path / "test.stl"

    header = b"test header" + b"\x00" * (HEADER_SIZE - 11)
    count = 10
    data = header + struct.pack("<I", count)
    # Only one triangle record is written, though the header declares 10.
    data += struct.pack("<12fH", *[0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 1, 0], 0)
    stl_file.write_bytes(data)

    with pytest.raises(StlError, match="truncated or corrupt binary STL") as excinfo:
        read_stl_facts(stl_file)
    message = str(excinfo.value)
    assert "declares 10 triangles" in message
    assert str(BINARY_PROLOGUE_SIZE + 10 * TRIANGLE_RECORD_SIZE) in message
    assert str(len(data)) in message


def test_binary_payload_diagnostics_survive_a_space_padded_printable_header(tmp_path):
    """A header padded with spaces is fully printable text, but that must not suppress the declared/expected/actual byte counts for the genuinely binary, truncated data that follows it."""
    stl_file = tmp_path / "test.stl"

    header_text = "Exported by SomeCAD 2026.1 widget bracket rev3 units mm operator ajl"
    header_bytes = header_text.encode("ascii")
    header_bytes = header_bytes + b" " * (HEADER_SIZE - len(header_bytes))
    count = 1000
    record = struct.pack("<12fH", *[0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 1, 0], 0)
    data = header_bytes + struct.pack("<I", count) + record * 3
    stl_file.write_bytes(data)

    with pytest.raises(StlError, match="truncated or corrupt binary STL") as excinfo:
        read_stl_facts(stl_file)
    message = str(excinfo.value)
    assert "declares 1000 triangles" in message
    assert str(BINARY_PROLOGUE_SIZE + count * TRIANGLE_RECORD_SIZE) in message
    assert str(len(data)) in message


def test_binary_payload_diagnostics_are_the_same_with_a_nul_padded_header(tmp_path):
    """The same truncated file with a NUL-padded rather than space-padded header reports the identical counts; the header's padding style must not change the diagnosis."""
    stl_file = tmp_path / "test.stl"

    header_text = "Exported by SomeCAD 2026.1 widget bracket rev3 units mm operator ajl"
    header_bytes = header_text.encode("ascii")
    header_bytes = header_bytes + b"\x00" * (HEADER_SIZE - len(header_bytes))
    count = 1000
    record = struct.pack("<12fH", *[0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 1, 0], 0)
    data = header_bytes + struct.pack("<I", count) + record * 3
    stl_file.write_bytes(data)

    with pytest.raises(StlError, match="truncated or corrupt binary STL") as excinfo:
        read_stl_facts(stl_file)
    message = str(excinfo.value)
    assert "declares 1000 triangles" in message
    assert str(BINARY_PROLOGUE_SIZE + count * TRIANGLE_RECORD_SIZE) in message
    assert str(len(data)) in message


@pytest.mark.parametrize("padding", [b"\x00", b" "])
def test_exactly_84_bytes_is_rejected_without_the_byte_counts(tmp_path, padding):
    """A file that ends exactly at the 84-byte prologue carries no record to corroborate its declared count, so it is rejected without those numbers being repeated back.

    Reporting them for this case would mean reporting them for every
    84-byte file whose bytes 80-83 are not all NUL, since nothing else
    distinguishes one, and that fabricates a triangle count for small text
    notes and for unrelated binary formats. The header's padding style is
    irrelevant either way.
    """
    stl_file = tmp_path / "test.stl"

    header_bytes = b"model" + padding * (HEADER_SIZE - 5)
    data = header_bytes + struct.pack("<I", 10)
    assert len(data) == BINARY_PROLOGUE_SIZE
    stl_file.write_bytes(data)

    with pytest.raises(StlError) as excinfo:
        read_stl_facts(stl_file)
    assert "declares" not in str(excinfo.value)


def test_utf16_note_of_exactly_84_bytes_is_not_told_it_declares_triangles(tmp_path):
    """A UTF-16 text note trimmed to exactly 84 bytes must not be reported as a truncated binary STL. Its bytes 80-83 are ordinary text, so a rule that trusts an 84-byte file's count field fabricates a triangle count for it."""
    stl_file = tmp_path / "note.txt"

    data = ("A tiny note in UTF-16LE!" * 3).encode("utf-16-le")[:BINARY_PROLOGUE_SIZE]
    assert len(data) == BINARY_PROLOGUE_SIZE
    stl_file.write_bytes(data)

    with pytest.raises(StlError) as excinfo:
        read_stl_facts(stl_file)
    assert "declares" not in str(excinfo.value)


def test_binary_stl_with_positive_octant_float_data_reports_the_byte_counts(tmp_path):
    """A realistic truncated binary STL, with unit normals and vertex coordinates confined to a small positive range, sits under a naive byte-ratio threshold for non-printable data; the declared/expected/actual counts must still be reported rather than the file being misdiagnosed as bad ASCII text."""
    stl_file = tmp_path / "test.stl"

    header_bytes = b"model" + b"\x00" * (HEADER_SIZE - 5)
    declared = 20000
    triangles = []
    for i in range(300):
        v0 = (10 + (i % 50) * 0.1, 10 + ((i * 7) % 50) * 0.1, 10 + ((i * 13) % 50) * 0.1)
        v1 = (10 + ((i * 3 + 1) % 50) * 0.1, 10 + ((i * 11 + 2) % 50) * 0.1, 10 + ((i * 17 + 3) % 50) * 0.1)
        v2 = (10 + ((i * 5 + 4) % 50) * 0.1, 10 + ((i * 19 + 5) % 50) * 0.1, 10 + ((i * 23 + 6) % 50) * 0.1)
        triangles.append([0.0, 0.0, 1.0] + list(v0) + list(v1) + list(v2))
    data = header_bytes + struct.pack("<I", declared)
    for triangle in triangles:
        data += struct.pack("<12fH", *triangle, 0)
    stl_file.write_bytes(data)

    with pytest.raises(StlError, match="truncated or corrupt binary STL") as excinfo:
        read_stl_facts(stl_file)
    message = str(excinfo.value)
    assert "declares 20000 triangles" in message
    assert str(BINARY_PROLOGUE_SIZE + declared * TRIANGLE_RECORD_SIZE) in message
    assert str(len(data)) in message


def test_ascii_text_with_stray_control_byte_is_reported_without_fabricated_counts(tmp_path):
    """An otherwise ordinary text file with a single stray control byte is corrupted text, not misrouted binary payload; the declared/expected/actual counts must not be attached to it."""
    stl_file = tmp_path / "test.stl"

    text = (
        "solid widget\n"
        "  facet normal 0 0 1\n"
        "    outer loop\n"
        "      vertex 0 0 0 \x07 stray bell character in an otherwise ordinary line\n"
        "      vertex 1 0 0\n"
        "      vertex 0 1 0\n"
        "    endloop\n"
        "  endfacet\n"
        "endsolid widget\n"
    )
    data = text.encode("ascii")
    assert len(data) > BINARY_PROLOGUE_SIZE
    stl_file.write_bytes(data)

    with pytest.raises(StlError) as excinfo:
        read_stl_facts(stl_file)
    message = str(excinfo.value)
    assert "contains control bytes" in message
    assert "declares" not in message


def test_utf16_text_document_is_not_misread_as_a_truncated_binary_stl(tmp_path):
    """UTF-16LE-encoded ASCII text has a NUL byte in exactly half its positions, the same ratio a decoded triangle record happens to sit at under a lax threshold; it must not be reported as a truncated binary STL with fabricated triangle counts."""
    stl_file = tmp_path / "test.stl"

    text = (
        "solid state physics lecture notes\n"
        + "no triangles here, just prose about crystallography and phonons.\n" * 20
    )
    stl_file.write_bytes(text.encode("utf-16-le"))

    with pytest.raises(StlError) as excinfo:
        read_stl_facts(stl_file)
    message = str(excinfo.value)
    assert "contains control bytes" in message
    assert "declares" not in message





def test_binary_stl_with_zero_triangles(tmp_path):
    """A zero-triangle binary STL is legal STL; bbox is None."""
    stl_file = tmp_path / "test.stl"

    stl_file.write_bytes(_make_binary_stl("empty", []))

    facts = read_stl_facts(stl_file)
    assert facts["format"] == "binary"
    assert facts["triangles"] == 0
    assert facts["bbox_min"] is None
    assert facts["bbox_max"] is None


def test_ascii_stl_with_zero_triangles(tmp_path):
    """A zero-triangle ASCII STL is legal STL; bbox is None."""
    stl_file = tmp_path / "test.stl"

    stl_file.write_bytes(_make_ascii_stl("empty", []))

    facts = read_stl_facts(stl_file)
    assert facts["format"] == "ascii"
    assert facts["triangles"] == 0
    assert facts["bbox_min"] is None
    assert facts["bbox_max"] is None


def test_ascii_with_no_solid_header_raises_error(tmp_path):
    """A text file whose first line does not start with the 'solid' token is rejected outright."""
    stl_file = tmp_path / "test.stl"

    stl_file.write_text("not a real STL file\nfacet normal 0 0 1\n")

    with pytest.raises(StlError, match="not a recognisable STL file"):
        read_stl_facts(stl_file)


def test_ascii_first_line_must_be_the_solid_token_exactly(tmp_path):
    """A line starting with a word that merely begins with 'solid' (e.g. 'solidworks') is not the STL keyword and is rejected."""
    stl_file = tmp_path / "test.stl"

    stl_file.write_text("solidworks release notes\nnothing here at all\n")

    with pytest.raises(StlError, match="not a recognisable STL file"):
        read_stl_facts(stl_file)


def test_ascii_stl_with_unrecognised_line_raises_error(tmp_path):
    """A 'solid'-prefixed document that is not STL is caught by its second line failing the keyword grammar."""
    stl_file = tmp_path / "test.stl"

    stl_file.write_text("solid state physics notes\nnothing to see here\n")

    with pytest.raises(StlError, match="unrecognised line"):
        read_stl_facts(stl_file)


def test_ascii_stl_loop_with_wrong_vertex_count_raises_error(tmp_path):
    """An 'outer loop' that closes with other than exactly three vertices is malformed, not a valid triangle."""
    stl_file = tmp_path / "test.stl"

    text = (
        "solid t\n"
        "  facet normal 0 0 1\n"
        "    outer loop\n"
        "      vertex 0 0 0\n"
        "      vertex 1 0 0\n"
        "      vertex 0 1 0\n"
        "      vertex 1 1 0\n"
        "      vertex 2 1 0\n"
        "      vertex 1 2 0\n"
        "    endloop\n"
        "  endfacet\n"
        "endsolid t\n"
    )
    stl_file.write_bytes(text.encode("ascii"))

    with pytest.raises(StlError, match="expected 3"):
        read_stl_facts(stl_file)


def test_ascii_stl_vertex_with_underscore_digit_separator_raises_error(tmp_path):
    """A coordinate token containing a PEP 515 underscore digit separator (e.g. "1_0") must be rejected outright rather than silently parsed by float() to a different number ("1_0" -> 10.0)."""
    stl_file = tmp_path / "test.stl"

    text = (
        "solid t\n"
        "  facet normal 0 0 1\n"
        "    outer loop\n"
        "      vertex 1_0 2_0_0 0\n"
        "      vertex 1 0 0\n"
        "      vertex 0 1 0\n"
        "    endloop\n"
        "  endfacet\n"
        "endsolid t\n"
    )
    stl_file.write_bytes(text.encode("ascii"))

    with pytest.raises(StlError, match="non-numeric vertex"):
        read_stl_facts(stl_file)


def test_ascii_stl_vertex_with_extra_token_raises_error(tmp_path):
    """A vertex line with more than three coordinate tokens is malformed, not a triangle with the surplus token silently ignored."""
    stl_file = tmp_path / "test.stl"

    text = (
        "solid t\n"
        "  facet normal 0 0 1\n"
        "    outer loop\n"
        "      vertex 0 0 0 9 9\n"
        "      vertex 1 0 0\n"
        "      vertex 0 1 0\n"
        "    endloop\n"
        "  endfacet\n"
        "endsolid t\n"
    )
    stl_file.write_bytes(text.encode("ascii"))

    with pytest.raises(StlError, match="exactly 3"):
        read_stl_facts(stl_file)


def test_ascii_stl_bare_outer_without_loop_raises_error(tmp_path):
    """An 'outer' line whose second token is not 'loop' does not open a loop; it is malformed rather than accepted as equivalent to 'outer loop'."""
    stl_file = tmp_path / "test.stl"

    text = (
        "solid t\n"
        "  facet normal 0 0 1\n"
        "    outer\n"
        "      vertex 0 0 0\n"
        "      vertex 1 0 0\n"
        "      vertex 0 1 0\n"
        "    endloop\n"
        "  endfacet\n"
        "endsolid t\n"
    )
    stl_file.write_bytes(text.encode("ascii"))

    with pytest.raises(StlError, match="'outer' not followed by 'loop'"):
        read_stl_facts(stl_file)


def test_ascii_stl_facet_without_any_loop_raises_error(tmp_path):
    """A facet/endfacet pair with no outer loop body has no vertices and must not be counted as a triangle with a None bounding box."""
    stl_file = tmp_path / "test.stl"

    text = "solid s\n" + "  facet normal 0 0 1\n  endfacet\n" * 1000 + "endsolid s\n"
    stl_file.write_bytes(text.encode("ascii"))

    with pytest.raises(StlError, match="outer loops"):
        read_stl_facts(stl_file)


def test_ascii_stl_facet_with_two_outer_loops_raises_error(tmp_path):
    """A facet containing two outer loop blocks before its endfacet is malformed, not two triangles folded into one."""
    stl_file = tmp_path / "test.stl"

    text = (
        "solid s\n"
        "  facet normal 0 0 1\n"
        "    outer loop\n"
        "      vertex 0 0 0\n"
        "      vertex 1 0 0\n"
        "      vertex 0 1 0\n"
        "    endloop\n"
        "    outer loop\n"
        "      vertex 5 5 5\n"
        "      vertex 6 5 5\n"
        "      vertex 5 6 5\n"
        "    endloop\n"
        "  endfacet\n"
        "endsolid s\n"
    )
    stl_file.write_bytes(text.encode("ascii"))

    with pytest.raises(StlError, match="outer loops"):
        read_stl_facts(stl_file)


def test_ascii_stl_facet_after_endsolid_raises_error(tmp_path):
    """A facet appearing after its enclosing solid has already closed is not folded into that solid's geometry."""
    stl_file = tmp_path / "test.stl"

    text = (
        "solid a\n"
        "  facet normal 0 0 1\n"
        "    outer loop\n"
        "      vertex 0 0 0\n"
        "      vertex 1 0 0\n"
        "      vertex 0 1 0\n"
        "    endloop\n"
        "  endfacet\n"
        "endsolid a\n"
        "  facet normal 0 0 1\n"
        "    outer loop\n"
        "      vertex 9 9 9\n"
        "      vertex 10 0 0\n"
        "      vertex 0 10 0\n"
        "    endloop\n"
        "  endfacet\n"
    )
    stl_file.write_bytes(text.encode("ascii"))

    with pytest.raises(StlError, match="outside any solid"):
        read_stl_facts(stl_file)


def test_ascii_stl_truncated_mid_facet_raises_error(tmp_path):
    """A file that stops partway through a facet, with no endfacet or endsolid, is truncated rather than a smaller valid model."""
    stl_file = tmp_path / "test.stl"

    text = (
        "solid t\n"
        "  facet normal 0 0 1\n"
        "    outer loop\n"
        "      vertex 0 0 0\n"
        "      vertex 1 0 0\n"
        "      vertex 0 1 0\n"
        "    endloop\n"
        "  endfacet\n"
        "  facet normal 0 0 1\n"
        "    outer loop\n"
        "      vertex 9 9 9\n"
    )
    stl_file.write_bytes(text.encode("ascii"))

    with pytest.raises(StlError, match="truncated"):
        read_stl_facts(stl_file)


def test_ascii_stl_missing_endsolid_raises_error(tmp_path):
    """A file with a complete facet but no closing 'endsolid' is truncated."""
    stl_file = tmp_path / "test.stl"

    text = (
        "solid t\n"
        "  facet normal 0 0 1\n"
        "    outer loop\n"
        "      vertex 0 0 0\n"
        "      vertex 1 0 0\n"
        "      vertex 0 1 0\n"
        "    endloop\n"
        "  endfacet\n"
    )
    stl_file.write_bytes(text.encode("ascii"))

    with pytest.raises(StlError, match="endsolid"):
        read_stl_facts(stl_file)


def test_ascii_stl_with_two_solids_crlf_and_tabs(tmp_path):
    """CRLF line endings and tabs, both for indentation and between a keyword and its coordinates, are just whitespace; two solids in one file combine into one triangle count and bounding box."""
    stl_file = tmp_path / "test.stl"

    lines = [
        "solid first",
        "\tfacet normal 0 0 1",
        "\t\touter loop",
        "\t\t\tvertex\t0\t0\t0",
        "\t\t\tvertex\t1\t0\t0",
        "\t\t\tvertex\t0\t1\t0",
        "\t\tendloop",
        "\tendfacet",
        "endsolid first",
        "solid second",
        "\tfacet normal 0 0 1",
        "\t\touter loop",
        "\t\t\tvertex\t1\t1\t5",
        "\t\t\tvertex\t2\t1\t5",
        "\t\t\tvertex\t1\t2\t5",
        "\t\tendloop",
        "\tendfacet",
        "endsolid second",
    ]
    stl_file.write_bytes("\r\n".join(lines).encode("ascii") + b"\r\n")

    facts = read_stl_facts(stl_file)
    assert facts["format"] == "ascii"
    assert facts["triangles"] == 2
    assert facts["bbox_min"] == (0.0, 0.0, 0.0)
    assert facts["bbox_max"] == (2.0, 2.0, 5.0)


def test_ascii_stl_with_interior_cr_is_rejected_as_a_control_byte(tmp_path):
    """A bare CR in the interior of a line, as a file using CR-only line endings would produce once split on LF alone, is caught as a control byte rather than silently accepted the way a trailing CRLF artifact is."""
    stl_file = tmp_path / "test.stl"

    text = "solid a\nfacet normal 0 0 1\nouter loop\nvertex 0\r0 0\nendloop\nendfacet\nendsolid a\n"
    stl_file.write_bytes(text.encode("ascii"))

    with pytest.raises(StlError, match="contains control bytes"):
        read_stl_facts(stl_file)


@pytest.mark.parametrize("code_point", [0x1C, 0x1D, 0x1E, 0x1F])
def test_ascii_stl_trailing_information_separator_is_not_hidden_by_strip(tmp_path, code_point):
    """A C0 control byte that Python's str.strip() treats as whitespace (the information separators 0x1C-0x1F) must still be caught when it sits at the very end of a line, rather than being silently absorbed by a full strip() before the control-byte check runs. NEL (0x85) is deliberately not in this list: under the Latin-1 decode it cannot be distinguished from a UTF-8 continuation byte, so treating it as a control byte would reject valid non-English solid names."""
    stl_file = tmp_path / "test.stl"

    text = (
        "solid a\n"
        "facet normal 0 0 1\n"
        "outer loop\n"
        "vertex 0 0 0" + chr(code_point) + "\n"
        "vertex 1 0 0\n"
        "vertex 0 1 0\n"
        "endloop\n"
        "endfacet\n"
        "endsolid a\n"
    )
    stl_file.write_bytes(text.encode("latin-1"))

    with pytest.raises(StlError, match="contains control bytes"):
        read_stl_facts(stl_file)


@pytest.mark.parametrize(
    ("name", "encoding"),
    [
        ("Wspornik ł", "utf-8"),        # U+0142 encodes as C5 82
        ("一号支架", "utf-8"),   # U+4E00 encodes as E4 B8 80
        ("Brackets", "latin-1"),       # a single byte 0x92, as cp1252 text carries
    ],
)
def test_ascii_stl_solid_name_using_the_c1_byte_range_still_parses(tmp_path, name, encoding):
    """A solid name whose bytes fall in 0x80-0x9F parses normally.

    Under the Latin-1 decode those bytes are indistinguishable from UTF-8
    continuation bytes, which ordinary non-English text produces constantly,
    so they carry no information about whether the payload is binary. The
    existing Braecket case does not exercise this: U+00E4 encodes as C3 A4,
    both outside the range.
    """
    stl_file = tmp_path / "test.stl"

    text = (
        "solid %s\n"
        "  facet normal 0 0 1\n"
        "    outer loop\n"
        "      vertex 0 0 0\n"
        "      vertex 2 0 0\n"
        "      vertex 0 3 0\n"
        "    endloop\n"
        "  endfacet\n"
        "endsolid %s\n"
    ) % (name, name)
    stl_file.write_bytes(text.encode(encoding))

    facts = read_stl_facts(stl_file)
    assert facts["format"] == "ascii"
    assert facts["triangles"] == 1
    assert facts["bbox_min"] == (0.0, 0.0, 0.0)
    assert facts["bbox_max"] == (2.0, 3.0, 0.0)


def test_ascii_stl_with_non_ascii_solid_name_utf8(tmp_path):
    """A UTF-8-encoded non-ASCII solid name does not stop the geometry from parsing."""
    stl_file = tmp_path / "test.stl"

    text = (
        "solid Bräcket\n"
        "  facet normal 0 0 1\n"
        "    outer loop\n"
        "      vertex 0 0 0\n"
        "      vertex 1 0 0\n"
        "      vertex 0 1 0\n"
        "    endloop\n"
        "  endfacet\n"
        "endsolid Bräcket\n"
    )
    stl_file.write_bytes(text.encode("utf-8"))

    facts = read_stl_facts(stl_file)
    assert facts["format"] == "ascii"
    assert facts["triangles"] == 1


def test_ascii_stl_with_non_ascii_solid_name_latin1(tmp_path):
    """A Latin-1-encoded non-ASCII solid name does not stop the geometry from parsing."""
    stl_file = tmp_path / "test.stl"

    text = (
        "solid Bräcket\n"
        "  facet normal 0 0 1\n"
        "    outer loop\n"
        "      vertex 0 0 0\n"
        "      vertex 1 0 0\n"
        "      vertex 0 1 0\n"
        "    endloop\n"
        "  endfacet\n"
        "endsolid Bräcket\n"
    )
    stl_file.write_bytes(text.encode("latin-1"))

    facts = read_stl_facts(stl_file)
    assert facts["format"] == "ascii"
    assert facts["triangles"] == 1


def test_binary_stl_with_nan_vertex_raises_error(tmp_path):
    """A non-finite vertex coordinate is rejected rather than poisoning or being silently dropped from the bounding box."""
    stl_file = tmp_path / "test.stl"

    triangles = [
        [0, 0, 1, float("nan"), float("nan"), float("nan"), 1, 2, 3, 4, 5, 6],
    ]
    stl_file.write_bytes(_make_binary_stl("test", triangles))

    with pytest.raises(StlError, match="non-finite"):
        read_stl_facts(stl_file)


def test_ascii_stl_with_non_finite_vertex_raises_error(tmp_path):
    """A non-finite vertex coordinate is rejected in the ASCII reader too."""
    stl_file = tmp_path / "test.stl"

    text = (
        "solid t\n"
        "  facet normal 0 0 1\n"
        "    outer loop\n"
        "      vertex nan nan nan\n"
        "      vertex 1 2 3\n"
        "      vertex 4 5 6\n"
        "    endloop\n"
        "  endfacet\n"
        "endsolid t\n"
    )
    stl_file.write_bytes(text.encode("ascii"))

    with pytest.raises(StlError, match="non-finite"):
        read_stl_facts(stl_file)


def test_binary_header_with_non_ascii_utf8_decodes_correctly(tmp_path):
    """A UTF-8 header decodes as its original text rather than mangled replacement characters."""
    stl_file = tmp_path / "test.stl"

    header_text = "solid crème brûlée"
    header_bytes = header_text.encode("utf-8")
    header_bytes = header_bytes[:HEADER_SIZE] + b"\x00" * (HEADER_SIZE - len(header_bytes))
    data = header_bytes + struct.pack("<I", 0)
    stl_file.write_bytes(data)

    facts = read_stl_facts(stl_file)
    assert facts["header"] == header_text


def test_binary_header_control_bytes_are_sanitized(tmp_path):
    """Control bytes (e.g. a terminal escape sequence) in the header are collapsed to spaces, not passed through raw."""
    stl_file = tmp_path / "test.stl"

    header_bytes = b"header\x1b[2Jwith escape"
    header_bytes = header_bytes + b"\x00" * (HEADER_SIZE - len(header_bytes))
    data = header_bytes + struct.pack("<I", 0)
    stl_file.write_bytes(data)

    facts = read_stl_facts(stl_file)
    assert "\x1b" not in facts["header"]
    assert "header" in facts["header"]
    assert "with escape" in facts["header"]


def test_binary_header_utf8_encoded_c1_control_is_sanitized(tmp_path):
    """A UTF-8-encoded C1 control character (CSI, U+009B) in the header is collapsed to a space rather than surviving re-encoding to become a live terminal escape when this module writes the header to stdout."""
    stl_file = tmp_path / "test.stl"

    csi = chr(0x9B)
    header_text = "pre" + csi + "31;5mRED" + csi + "0m post"
    header_bytes = header_text.encode("utf-8")
    header_bytes = header_bytes[:HEADER_SIZE] + b"\x00" * (HEADER_SIZE - len(header_bytes))
    data = header_bytes + struct.pack("<I", 0)
    stl_file.write_bytes(data)

    facts = read_stl_facts(stl_file)
    assert csi not in facts["header"]
    assert "pre" in facts["header"]
    assert "RED" in facts["header"]
    assert "post" in facts["header"]


def test_binary_header_utf8_encoded_nel_is_sanitized(tmp_path):
    """A UTF-8-encoded NEL (U+0085) in the header is collapsed to a space, not an embedded line break on terminals that honour NEL."""
    stl_file = tmp_path / "test.stl"

    nel = chr(0x85)
    header_text = "line1" + nel + "line2"
    header_bytes = header_text.encode("utf-8")
    header_bytes = header_bytes[:HEADER_SIZE] + b"\x00" * (HEADER_SIZE - len(header_bytes))
    data = header_bytes + struct.pack("<I", 0)
    stl_file.write_bytes(data)

    facts = read_stl_facts(stl_file)
    assert nel not in facts["header"]
    assert facts["header"] == "line1 line2"


@pytest.mark.parametrize(
    "code_point",
    [0x202A, 0x202B, 0x202C, 0x202D, 0x202E, 0x2066, 0x2067, 0x2068, 0x2069],
)
def test_binary_header_bidirectional_text_controls_are_sanitized(tmp_path, code_point):
    """Every bidirectional text control that can reorder a header's visual display, the explicit embeddings and overrides U+202A-U+202E and the isolates U+2066-U+2069, is collapsed to a space, not just the right-to-left override."""
    stl_file = tmp_path / "test.stl"

    control = chr(code_point)
    header_text = "A" + control + "B"
    header_bytes = header_text.encode("utf-8")
    header_bytes = header_bytes[:HEADER_SIZE] + b"\x00" * (HEADER_SIZE - len(header_bytes))
    data = header_bytes + struct.pack("<I", 0)
    stl_file.write_bytes(data)

    facts = read_stl_facts(stl_file)
    assert control not in facts["header"]
    assert facts["header"] == "A B"


def test_empty_file_raises_error(tmp_path):
    stl_file = tmp_path / "test.stl"
    stl_file.write_bytes(b"")

    with pytest.raises(StlError, match="empty file"):
        read_stl_facts(stl_file)


def test_read_binary_raises_on_short_read():
    """_read_binary defensively rejects a handle that runs out of bytes before the declared count is satisfied."""
    header = b"x" * HEADER_SIZE
    one_record = struct.pack("<12fH", *([0.0] * 12), 0)
    fh = io.BytesIO(one_record)  # one record present; declared says two

    with pytest.raises(StlError, match="truncated binary STL"):
        _read_binary(fh, 2, header, size_bytes=999)


def test_read_binary_truncation_message_counts_records_in_partial_chunk():
    """The truncated-binary count includes records fully present in the short final chunk, not just previously completed 4096-record batches."""
    header = b"x" * HEADER_SIZE
    record = struct.pack("<12fH", *([0.0] * 12), 0)
    fh = io.BytesIO(record * 4200)  # 4200 records present; declared says 5000

    with pytest.raises(StlError, match="only 4200 fully present"):
        _read_binary(fh, 5000, header, size_bytes=999)


def test_binary_stl_with_undercounted_declared_count_raises_error(tmp_path):
    """A declared triangle count smaller than the records actually present in the file is a size mismatch, not a valid shorter file with the extra records silently dropped. The file is longer than its declared count implies, so the message describes unexpected trailing data rather than truncation."""
    stl_file = tmp_path / "test.stl"

    header = b"h" * HEADER_SIZE
    # Real geometry, not a run of zeros: an all-zero record describes a zero
    # normal and three coincident vertices, which is not evidence of a model.
    record = struct.pack(
        "<12fH", *([0.0, 0.0, 1.0] + [4.5, 6.25, 1.5] * 3), 0)
    data = header + struct.pack("<I", 1) + record * 11
    stl_file.write_bytes(data)

    with pytest.raises(StlError, match="more data than the declared triangle count") as excinfo:
        read_stl_facts(stl_file)
    message = str(excinfo.value)
    assert "declares 1 triangles" in message
    assert str(BINARY_PROLOGUE_SIZE + TRIANGLE_RECORD_SIZE) in message
    assert str(len(data)) in message


def test_non_stl_file_with_zero_bytes_at_count_offset_is_rejected(tmp_path):
    """A non-STL file that happens to have zero bytes at the header's count-field offset is not a valid zero-triangle binary STL merely because it is long enough to hold one; unbounded surplus must not be read as padding."""
    stl_file = tmp_path / "test.stl"
    stl_file.write_bytes(b"\x00" * 5000)

    with pytest.raises(StlError):
        read_stl_facts(stl_file)


def test_non_stl_binary_blob_with_elf_magic_prefix_is_rejected(tmp_path):
    """A few hundred kilobytes of plausible non-STL binary data carrying an ELF-like magic prefix is rejected, without being misread as a valid STL of either format."""
    size = 300 * 1024

    # The count field at the binary-STL offset (bytes 80:84) is forced to
    # an unsatisfiable value so this blob cannot coincidentally pass the
    # binary size check; no byte in the blob is a line feed, so the ASCII
    # fallback sees the whole file as a single line and rejects it on its
    # control bytes.
    pattern = bytes(b for b in range(256) if b != 0x0A)
    body = (pattern * (size // len(pattern) + 1))[:size]
    elf_like = (
        b"\x7fELF\x02\x01\x01\x00" + body[8:80]
        + struct.pack("<I", 0xFFFFFFFF) + body[84:])
    elf_blob = tmp_path / "elf.stl"
    elf_blob.write_bytes(elf_like)
    with pytest.raises(StlError):
        read_stl_facts(elf_blob)


def test_ascii_stl_with_tabs_between_vertex_coordinates(tmp_path):
    """A tab used as the separator between the 'vertex' keyword and its coordinates is legitimate whitespace in the STL grammar, not evidence of a binary payload."""
    stl_file = tmp_path / "test.stl"

    text = (
        "solid obj\n"
        "  facet normal 0 0 1\n"
        "    outer loop\n"
        "      vertex\t0.0\t0.0\t0.0\n"
        "      vertex\t1.0\t0.0\t0.0\n"
        "      vertex\t0.0\t1.0\t0.0\n"
        "    endloop\n"
        "  endfacet\n"
        "endsolid obj\n"
    )
    stl_file.write_bytes(text.encode("ascii"))

    facts = read_stl_facts(stl_file)
    assert facts["format"] == "ascii"
    assert facts["triangles"] == 1
    assert facts["bbox_min"] == (0.0, 0.0, 0.0)
    assert facts["bbox_max"] == (1.0, 1.0, 0.0)


def test_ascii_stl_second_solid_truncated_without_endsolid_raises_error(tmp_path):
    """A second solid in a multi-solid file that never reaches its own 'endsolid' is a truncated write, even though the first solid in the file closed cleanly."""
    stl_file = tmp_path / "test.stl"

    text = (
        "solid one\n"
        "  facet normal 0 0 1\n"
        "    outer loop\n"
        "      vertex 0 0 0\n"
        "      vertex 1 0 0\n"
        "      vertex 0 1 0\n"
        "    endloop\n"
        "  endfacet\n"
        "endsolid one\n"
        "solid two\n"
        "  facet normal 0 0 1\n"
        "    outer loop\n"
        "      vertex 7 7 7\n"
        "      vertex 8 7 7\n"
        "      vertex 7 8 7\n"
        "    endloop\n"
        "  endfacet\n"
    )
    stl_file.write_bytes(text.encode("ascii"))

    with pytest.raises(StlError, match="no closing 'endsolid'"):
        read_stl_facts(stl_file)


def test_ascii_stl_second_solid_opened_before_first_closed_raises_error(tmp_path):
    """A 'solid' that opens while a previous solid in the same file is still open, with no intervening 'endsolid', is malformed."""
    stl_file = tmp_path / "test.stl"

    text = "solid one\nsolid two\nendsolid two\nendsolid one\n"
    stl_file.write_bytes(text.encode("ascii"))

    with pytest.raises(StlError, match="previous solid was closed"):
        read_stl_facts(stl_file)


def test_ascii_stl_endsolid_without_matching_solid_raises_error(tmp_path):
    """A second 'endsolid' with no 'solid' reopened since the first one closed has nothing to close."""
    stl_file = tmp_path / "test.stl"

    text = "solid one\nendsolid one\nendsolid one\n"
    stl_file.write_bytes(text.encode("ascii"))

    with pytest.raises(StlError, match="without a matching 'solid'"):
        read_stl_facts(stl_file)


def test_ascii_stl_with_utf8_bom_is_still_recognised(tmp_path):
    """A leading UTF-8 byte-order mark, as written by some Windows text editors, does not stop an otherwise valid ASCII STL from being recognised."""
    stl_file = tmp_path / "test.stl"

    body = _make_ascii_stl("test", [[(0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0)]])
    stl_file.write_bytes(b"\xef\xbb\xbf" + body)

    facts = read_stl_facts(stl_file)
    assert facts["format"] == "ascii"
    assert facts["triangles"] == 1
    assert facts["bbox_max"] == (1.0, 1.0, 0.0)


def test_ascii_fallback_rejects_line_exceeding_bounded_cap(tmp_path):
    """A file that reaches the ASCII reader with no line feed anywhere is refused once its unterminated line passes a bounded cap, rather than being read into memory whole first."""
    stl_file = tmp_path / "test.stl"

    # No 0x0A anywhere in two megabytes, so a line-buffered reader would
    # have to materialise the whole file before this content could be
    # rejected; the bounded reader must not.
    stl_file.write_bytes(b"x" * (2 * 1024 * 1024))

    with pytest.raises(StlError, match="line longer than"):
        read_stl_facts(stl_file)


class _MaxRequestSpyHandle:
    """A read-only handle that records the largest size ever passed to read()."""

    def __init__(self, data):
        self._buf = io.BytesIO(data)
        self.max_requested = 0

    def read(self, size=-1):
        self.max_requested = max(self.max_requested, size)
        return self._buf.read(size)


def test_iter_ascii_lines_never_requests_more_than_the_bounded_chunk_size():
    """_iter_ascii_lines must never ask its handle to read more than _ASCII_READ_CHUNK_BYTES at once, even across a file spanning many chunks, so a large file is read incrementally rather than materialised whole."""
    data = b"line\n" * 20000
    spy = _MaxRequestSpyHandle(data)

    lines = list(_iter_ascii_lines(spy, "irrelevant/path"))

    assert spy.max_requested == _ASCII_READ_CHUNK_BYTES
    assert lines == ["line"] * 20000


def test_ascii_parse_leaves_no_unclosed_resource_warning(tmp_path):
    """Parsing a valid ASCII STL under promoted ResourceWarnings raises none, because the reader never hands the raw handle to a text wrapper that could outlive it."""
    stl_file = tmp_path / "test.stl"

    facets = [[(0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0)]]
    stl_file.write_bytes(_make_ascii_stl("test", facets))

    result = subprocess.run(
        [sys.executable, "-W", "error::ResourceWarning",
         "-m", "annealage_mesh.stl", str(stl_file)],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert result.stderr == ""


def test_cli_with_valid_binary_stl(tmp_path):
    stl_file = tmp_path / "test.stl"

    triangles = [
        [0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 1, 0],
        [0, 0, 1, 1, 1, 0, 2, 1, 0, 1, 2, 0],
    ]
    stl_file.write_bytes(_make_binary_stl("test header", triangles))

    result = subprocess.run(
        [sys.executable, "-m", "annealage_mesh.stl", str(stl_file)],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert "format:     binary" in result.stdout
    assert "triangles:  2" in result.stdout
    assert "bbox_min:" in result.stdout
    assert "bbox_max:" in result.stdout
    assert "size_bytes:" in result.stdout
    assert "header:" in result.stdout


def test_cli_with_non_ascii_header_under_restrictive_stdout_encoding(tmp_path):
    """A non-ASCII header must not crash the CLI's report when stdout's own encoding cannot represent it; the report degrades the header display instead of tracebacking partway through."""
    stl_file = tmp_path / "test.stl"

    header_text = "café exporter v2"
    header_bytes = header_text.encode("utf-8")
    header_bytes = header_bytes[:HEADER_SIZE] + b"\x00" * (HEADER_SIZE - len(header_bytes))
    data = header_bytes + struct.pack("<I", 0)
    stl_file.write_bytes(data)

    env = dict(os.environ, PYTHONIOENCODING="ascii")
    result = subprocess.run(
        [sys.executable, "-m", "annealage_mesh.stl", str(stl_file)],
        capture_output=True,
        text=True,
        env=env,
    )

    assert result.returncode == 0
    assert result.stderr == ""
    assert "header:" in result.stdout
    assert "size_bytes:" in result.stdout


def test_cli_with_valid_ascii_stl(tmp_path):
    stl_file = tmp_path / "test.stl"

    facets = [
        [(0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0)],
        [(1.0, 1.0, 0.0), (2.0, 1.0, 0.0), (1.0, 2.0, 0.0)],
    ]
    stl_file.write_bytes(_make_ascii_stl("test", facets))

    result = subprocess.run(
        [sys.executable, "-m", "annealage_mesh.stl", str(stl_file)],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert "format:     ascii" in result.stdout
    assert "triangles:  2" in result.stdout
    assert "bbox_min:" in result.stdout
    assert "bbox_max:" in result.stdout


def test_cli_with_zero_triangle_file(tmp_path):
    stl_file = tmp_path / "test.stl"

    stl_file.write_bytes(_make_binary_stl("empty", []))

    result = subprocess.run(
        [sys.executable, "-m", "annealage_mesh.stl", str(stl_file)],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert "format:     binary" in result.stdout
    assert "triangles:  0" in result.stdout
    assert "(no geometry, zero triangles)" in result.stdout


def test_cli_with_nonexistent_file(tmp_path):
    nonexistent = tmp_path / "does_not_exist.stl"

    result = subprocess.run(
        [sys.executable, "-m", "annealage_mesh.stl", str(nonexistent)],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "error:" in result.stderr


def test_cli_with_invalid_args():
    result = subprocess.run(
        [sys.executable, "-m", "annealage_mesh.stl"],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert "usage:" in result.stderr


def test_cli_with_too_many_args(tmp_path):
    stl_file = tmp_path / "test.stl"
    stl_file.write_bytes(_make_binary_stl("test", []))

    result = subprocess.run(
        [sys.executable, "-m", "annealage_mesh.stl", str(stl_file), "extra_arg"],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert "usage:" in result.stderr


def test_utf8_prose_is_not_told_it_declares_triangles(tmp_path):
    """A UTF-8 text document must not be diagnosed as a binary STL. Its multi-byte lead and continuation bytes are not printable ASCII, so counting every non-printable byte as binary evidence makes ordinary non-English prose look like a truncated float32 payload."""
    stl_file = tmp_path / "notes.txt"

    text = "Unicode prose with accents éàçñ and enough length to run well past the prologue.\n"
    stl_file.write_bytes((text * 8).encode("utf-8"))

    with pytest.raises(StlError) as excinfo:
        read_stl_facts(stl_file)
    assert "declares" not in str(excinfo.value)


def test_cjk_prose_is_not_told_it_declares_triangles(tmp_path):
    """A CJK text document is almost entirely multi-byte UTF-8, the worst case for a byte-ratio test that treats high bytes as binary evidence."""
    stl_file = tmp_path / "notes.txt"

    text = "測試文件内容需要足夠長以超過標頭邊界並繼續下去。\n"
    stl_file.write_bytes((text * 8).encode("utf-8"))

    with pytest.raises(StlError) as excinfo:
        read_stl_facts(stl_file)
    assert "declares" not in str(excinfo.value)


def test_small_utf16_document_is_not_told_it_declares_triangles(tmp_path):
    """A UTF-16 document between 84 and 95 bytes leaves too few bytes after the prologue for the stride test to recognise UTF-16, so the byte-ratio test must decline to vote at that size rather than reporting a fabricated triangle count."""
    stl_file = tmp_path / "notes.txt"

    data = "solid tiny model here ok and some more text".encode("utf-16-le")
    assert BINARY_PROLOGUE_SIZE < len(data) < BINARY_PROLOGUE_SIZE + 16
    stl_file.write_bytes(data)

    with pytest.raises(StlError) as excinfo:
        read_stl_facts(stl_file)
    assert "declares" not in str(excinfo.value)


def test_zero_triangle_binary_stl_must_be_exactly_the_prologue(tmp_path):
    """A file declaring no triangles has no payload to corroborate it, so the only thing marking it as STL is four NUL bytes at offset 80. Surplus bytes are not allowed in that case, or an unrelated file with NULs there is reported as a valid empty model."""
    stl_file = tmp_path / "test.stl"

    stl_file.write_bytes(b"h" * HEADER_SIZE + struct.pack("<I", 0) + b"x")

    with pytest.raises(StlError):
        read_stl_facts(stl_file)


def test_zero_triangle_binary_stl_at_exactly_the_prologue_is_valid(tmp_path):
    """A real zero-triangle export is exactly 84 bytes, and is byte-identical to any other 84-byte file with NULs at offset 80, so it is accepted."""
    stl_file = tmp_path / "test.stl"

    stl_file.write_bytes(b"empty model" + b"\x00" * (HEADER_SIZE - 11) + struct.pack("<I", 0))

    facts = read_stl_facts(stl_file)
    assert facts["format"] == "binary"
    assert facts["triangles"] == 0
    assert facts["bbox_min"] is None
    assert facts["bbox_max"] is None


def test_all_zero_bytes_after_the_prologue_are_not_read_as_a_triangle(tmp_path):
    """A run of NUL bytes at the record offset unpacks as twelve zeros with a zero attribute count, but describes a zero normal and three identical vertices at the origin, which no real model opens with. Treating it as a triangle hands a fabricated count to any file with NULs there."""
    stl_file = tmp_path / "data.bin"

    data = b"d" * HEADER_SIZE + struct.pack("<I", 5000) + b"\x00" * 200
    stl_file.write_bytes(data)

    with pytest.raises(StlError) as excinfo:
        read_stl_facts(stl_file)
    assert "declares" not in str(excinfo.value)


def test_truncated_binary_with_non_round_coordinates_still_reports_the_counts(tmp_path):
    """Coordinates that are not round numbers produce almost no control bytes, so the counts must be established from the record's structure rather than from any ratio of non-printable bytes."""
    stl_file = tmp_path / "test.stl"

    record = struct.pack(
        "<12fH", *([0.0, 0.0, 1.0] + [10.1, 3.7, 8.15] * 3), 0)
    data = b"m" + b"\x00" * (HEADER_SIZE - 1) + struct.pack("<I", 1000) + record * 3
    stl_file.write_bytes(data)

    with pytest.raises(StlError, match="truncated or corrupt binary STL") as excinfo:
        read_stl_facts(stl_file)
    assert "declares 1000 triangles" in str(excinfo.value)
