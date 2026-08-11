"""Tests for the dependency-free STL reader."""

import io
import struct
import subprocess
import sys

import pytest

from annealage_mesh.stl import (
    BINARY_PROLOGUE_SIZE,
    HEADER_SIZE,
    TRIANGLE_RECORD_SIZE,
    StlError,
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
    """A declared triangle count smaller than the records actually present in the file is a size mismatch, not a valid shorter file with the extra records silently dropped."""
    stl_file = tmp_path / "test.stl"

    header = b"h" * HEADER_SIZE
    record = struct.pack("<12fH", *([0.0] * 12), 0)
    data = header + struct.pack("<I", 1) + record * 11
    stl_file.write_bytes(data)

    with pytest.raises(StlError, match="truncated or corrupt binary STL") as excinfo:
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
