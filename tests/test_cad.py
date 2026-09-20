"""Tests for the CAD subpackage and CAD MCP tools.

The helper scripts in ``cad/`` have CadQuery/trimesh as optional deps.
Tests that need those deps skip themselves when they're not available.
Tests that exercise pure-stdlib logic run unconditionally.
"""

import json

import pytest

from annealage_mesh import project
from annealage_mesh.cad import HELPER_SCRIPTS, scaffold, script_source

# ── scaffold template tests ──────────────────────────────────────


class TestScaffoldTemplates:
    """The scaffold templates generate valid, self-consistent content."""

    def test_dimensions_json_body_is_valid_json(self):
        body = scaffold.dimensions_json_body()
        parsed = json.loads(body)
        assert isinstance(parsed, dict)
        assert "_units" in parsed
        assert parsed["_units"] == "mm"

    def test_dimensions_json_has_placeholder_values(self):
        parsed = json.loads(scaffold.dimensions_json_body())
        # Must have at least one non-meta key for the scaffold model.py to read
        assert "width" in parsed
        assert "depth" in parsed
        assert "height" in parsed

    def test_model_py_body_is_syntactically_valid(self):
        """The template must parse as Python without a SyntaxError."""
        body = scaffold.model_py_body()
        compile(body, "model.py", "exec")

    def test_model_py_has_pep723_deps(self):
        body = scaffold.model_py_body()
        assert "# /// script" in body
        assert "cadquery" in body

    def test_model_py_reads_dimensions_json(self):
        body = scaffold.model_py_body()
        assert "dimensions.json" in body

    def test_model_py_exports_to_models_dir(self):
        body = scaffold.model_py_body()
        assert 'pathlib.Path("models")' in body

    def test_claude_md_cad_section_covers_all_stages(self):
        section = scaffold.claude_md_cad_section()
        assert "dimensions.json" in section
        assert "model.py" in section
        assert "robust_solids" in section
        assert "section_probe" in section
        assert "export_watertight" in section
        assert "pin_to_model" in section


# ── helper script bundling ───────────────────────────────────────


class TestHelperScripts:
    """The bundled helper scripts are present and loadable."""

    @pytest.mark.parametrize("name", HELPER_SCRIPTS)
    def test_script_source_returns_nonempty_string(self, name):
        src = script_source(name)
        assert isinstance(src, str)
        assert len(src) > 100

    @pytest.mark.parametrize("name", HELPER_SCRIPTS)
    def test_script_source_is_syntactically_valid_python(self, name):
        src = script_source(name)
        compile(src, name, "exec")

    def test_robust_solids_has_pep723_block(self):
        src = script_source("robust_solids.py")
        assert "# /// script" in src

    def test_section_probe_has_pep723_block(self):
        src = script_source("section_probe.py")
        assert "# /// script" in src

    def test_export_watertight_has_pep723_block(self):
        src = script_source("export_watertight.py")
        assert "# /// script" in src


# ── project scaffold creates CAD files ───────────────────────────


class TestProjectScaffoldCAD:
    """``ensure_project`` creates the CAD scaffold files."""

    def test_scaffold_creates_dimensions_json(self, tmp_path):
        project.ensure_project(tmp_path, git=False)
        assert (tmp_path / "dimensions.json").is_file()
        parsed = json.loads((tmp_path / "dimensions.json").read_text())
        assert "width" in parsed

    def test_scaffold_creates_model_py(self, tmp_path):
        project.ensure_project(tmp_path, git=False)
        assert (tmp_path / "model.py").is_file()
        body = (tmp_path / "model.py").read_text()
        assert "cadquery" in body

    def test_scaffold_creates_cad_directory_with_helpers(self, tmp_path):
        project.ensure_project(tmp_path, git=False)
        assert (tmp_path / "cad").is_dir()
        for name in HELPER_SCRIPTS:
            assert (tmp_path / "cad" / name).is_file(), "missing cad/%s" % name

    def test_scaffold_helpers_match_bundled_source(self, tmp_path):
        """The scaffolded copies must be byte-identical to the package."""
        project.ensure_project(tmp_path, git=False)
        for name in HELPER_SCRIPTS:
            scaffolded = (tmp_path / "cad" / name).read_text()
            bundled = script_source(name)
            assert scaffolded == bundled, "cad/%s differs from bundled" % name

    def test_scaffold_does_not_overwrite_existing_dimensions_json(self, tmp_path):
        custom = '{"my_value": 42}\n'
        (tmp_path / "dimensions.json").write_text(custom)
        project.ensure_project(tmp_path, git=False)
        assert (tmp_path / "dimensions.json").read_text() == custom

    def test_scaffold_does_not_overwrite_existing_model_py(self, tmp_path):
        custom = "# my custom model\n"
        (tmp_path / "model.py").write_text(custom)
        project.ensure_project(tmp_path, git=False)
        assert (tmp_path / "model.py").read_text() == custom

    def test_scaffold_does_not_overwrite_existing_cad_scripts(self, tmp_path):
        (tmp_path / "cad").mkdir()
        custom = "# my custom robust_solids\n"
        (tmp_path / "cad" / "robust_solids.py").write_text(custom)
        project.ensure_project(tmp_path, git=False)
        assert (tmp_path / "cad" / "robust_solids.py").read_text() == custom

    def test_scaffold_result_reports_cad_files_in_created(self, tmp_path):
        result = project.ensure_project(tmp_path, git=False)
        assert "dimensions.json" in result.created
        assert "model.py" in result.created
        assert "cad" in result.created
        assert any("cad/" in name for name in result.created)

    def test_scaffold_idempotent_reports_cad_files_in_kept(self, tmp_path):
        project.ensure_project(tmp_path, git=False)
        result = project.ensure_project(tmp_path, git=False)
        assert "dimensions.json" in result.kept
        assert "model.py" in result.kept
        assert "cad" in result.kept

    def test_force_regenerates_dimensions_json(self, tmp_path):
        project.ensure_project(tmp_path, git=False)
        (tmp_path / "dimensions.json").write_text('{"old": true}')
        result = project.ensure_project(tmp_path, git=False, force=True)
        assert "dimensions.json" in result.regenerated
        parsed = json.loads((tmp_path / "dimensions.json").read_text())
        assert "width" in parsed  # regenerated to template

    def test_force_does_not_regenerate_cad_helper_scripts(self, tmp_path):
        """Helper scripts are never overwritten by force — they are
        scaffolded once and the user may have modified them."""
        project.ensure_project(tmp_path, git=False)
        custom = "# modified\n"
        (tmp_path / "cad" / "robust_solids.py").write_text(custom)
        project.ensure_project(tmp_path, git=False, force=True)
        assert (tmp_path / "cad" / "robust_solids.py").read_text() == custom

    def test_claude_md_includes_cad_workflow(self, tmp_path):
        project.ensure_project(tmp_path, git=False)
        body = (tmp_path / "CLAUDE.md").read_text()
        assert "dimensions.json" in body
        assert "model.py" in body
        assert "robust_solids" in body
        assert "CadQuery" in body

    def test_gitignore_includes_build_directory(self, tmp_path):
        project.ensure_project(tmp_path, git=False)
        body = (tmp_path / ".gitignore").read_text()
        assert "build/" in body


# ── pin_to_model (pure stdlib, always runs) ──────────────────────


class TestPinToModel:
    """pin_to_model helpers are pure math — no optional deps."""

    def test_center_drop_shift(self):
        from annealage_mesh.cad.pin_to_model import center_drop_shift

        class FakeBBox:
            xmin, xmax = -10, 30
            ymin, ymax = 0, 20
            zmin = 5

        shift = center_drop_shift(FakeBBox())
        assert shift == (-10.0, -10.0, -5.0)

    def test_invert_bottom_half_is_identity_minus_shift(self):
        from annealage_mesh.cad.pin_to_model import invert_bottom_half

        pin = (15, 10, 5)
        shift = (5, 3, 1)
        model = invert_bottom_half(pin, shift)
        assert model == (10, 7, 4)

    def test_invert_top_half_flips_yz(self):
        from annealage_mesh.cad.pin_to_model import invert_top_half

        pin = (10, 5, 3)
        shift = (0, 0, 0)
        model = invert_top_half(pin, shift)
        assert model == (10, -5, -3)

    def test_map_pin_chooses_half_by_x_sign(self):
        from annealage_mesh.cad.pin_to_model import map_pin

        Tsh = (1, 0, 0)
        Bsh = (2, 0, 0)
        # positive x → top half
        top_result = map_pin((5, 3, 2), Tsh, Bsh)
        assert top_result == (4, -3, -2)
        # negative x → bottom half
        bot_result = map_pin((-5, 3, 2), Tsh, Bsh)
        assert bot_result == (-7, 3, 2)


# ── cad_tools dimensions (pure stdlib, always runs) ──────────────


class TestDimensionsHelpers:
    """The dimensions CRUD helpers in cad_tools work without optional deps."""

    def test_read_dims_returns_none_for_missing_file(self, tmp_path):
        from annealage_mesh.tools.cad_tools import _read_dims

        assert _read_dims(tmp_path) is None

    def test_read_dims_returns_parsed_json(self, tmp_path):
        from annealage_mesh.tools.cad_tools import _read_dims

        (tmp_path / "dimensions.json").write_text('{"x": 10}')
        result = _read_dims(tmp_path)
        assert result == {"x": 10}

    def test_write_dims_creates_file(self, tmp_path):
        from annealage_mesh.tools.cad_tools import _write_dims

        _write_dims(tmp_path, {"y": 20})
        parsed = json.loads((tmp_path / "dimensions.json").read_text())
        assert parsed == {"y": 20}

    def test_get_path_walks_dotted_keys(self):
        from annealage_mesh.tools.cad_tools import _get_path

        d = {"a": {"b": {"c": 42}}}
        val, found = _get_path(d, "a.b.c")
        assert found
        assert val == 42

    def test_get_path_returns_not_found_for_missing(self):
        from annealage_mesh.tools.cad_tools import _get_path

        d = {"a": 1}
        val, found = _get_path(d, "b")
        assert not found

    def test_set_path_creates_intermediate_dicts(self):
        from annealage_mesh.tools.cad_tools import _set_path

        d = {}
        _set_path(d, "a.b.c", 99)
        assert d == {"a": {"b": {"c": 99}}}

    def test_list_leaves_skips_meta_keys(self):
        from annealage_mesh.tools.cad_tools import _list_leaves

        d = {"x": 1, "_units": "mm", "//x": "note", "nested": {"y": 2}}
        leaves = list(_list_leaves(d))
        keys = [k for k, v in leaves]
        assert "x" in keys
        assert "nested.y" in keys
        assert "_units" not in keys
        assert "//x" not in keys


# ── cad_tools MCP tool integration ───────────────────────────────


class TestMCPToolIntegration:
    """The MCP tool builders return valid tool definitions."""

    def test_cad_tools_build_returns_three_tools(self, tmp_path):
        from annealage_mesh.tools.cad_tools import build

        tools = build(tmp_path)
        assert len(tools) == 3
        names = {t.name for t in tools}
        assert "mesh_verify" in names
        assert "mesh_dimensions" in names
        assert "mesh_dimensions_set" in names
