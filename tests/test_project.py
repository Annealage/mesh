"""Tests for project scaffolding and the git decision tree in ``project.py``.

Every git-shaped case is driven through a fake ``run`` and a fake ``which``,
never a real git binary or a real repository: ``_FakeGit`` below answers the
exact argv shapes ``project.py`` issues and records every call it saw, so a
test can assert not only the outcome but which git commands actually ran
(most usefully, that a commit was never attempted once the init itself was
skipped or refused).
"""

import pytest

from annealage_mesh import project


class _Completed:
    """Just enough of ``subprocess.CompletedProcess`` for a stubbed ``run``."""

    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


class _FakeGit:
    """A canned ``run`` for the git subprocess calls ``project.py`` issues.

    ``state`` picks which of the three ``_probe_git_state`` outcomes the
    probe reports: ``"clear"`` (no repository claims the path, so ``init``
    proceeds), ``"root"`` (the path is itself a repository's top level) or
    ``"nested"`` (the path sits inside a repository rooted elsewhere).
    ``toplevel`` is what ``git rev-parse --show-toplevel`` reports for
    ``"root"``/``"nested"``; a project's own directory for ``"root"``, and
    some other path for ``"nested"``.

    ``user_email`` stands in for whatever ``git config user.email`` would
    report; ``None`` reproduces an unset value. ``init_ok`` and ``commit_ok``
    let a test make either step fail without touching every other one.

    Every call is recorded in ``calls`` as a plain ``argv`` tuple, so a test
    can assert on the sequence of git subcommands actually issued, not only
    on the ``ScaffoldResult`` that came out the other end. An argv this fake
    does not recognise raises rather than returning a plausible default,
    since a command ``project.py`` was not expected to issue is itself the
    finding a test needs to see.
    """

    def __init__(
        self,
        *,
        state="clear",
        user_email="dev@example.com",
        init_ok=True,
        commit_ok=True,
        toplevel=None,
    ):
        self.state = state
        self.user_email = user_email
        self.init_ok = init_ok
        self.commit_ok = commit_ok
        self.toplevel = toplevel
        self.calls = []

    def __call__(self, argv, *, cwd, capture_output, text, timeout, check):
        assert capture_output and text and not check
        self.calls.append(tuple(argv))
        assert argv[0] == "git"
        sub = tuple(argv[1:])

        if sub[:2] == ("rev-parse", "--is-inside-work-tree"):
            if self.state == "clear":
                return _Completed(returncode=1)
            return _Completed(stdout="true\n", returncode=0)
        if sub[:2] == ("rev-parse", "--show-toplevel"):
            assert self.state in ("root", "nested")
            return _Completed(stdout=str(self.toplevel) + "\n", returncode=0)
        if sub[:1] == ("init",):
            if self.init_ok:
                return _Completed(returncode=0)
            return _Completed(returncode=1, stderr="init failed")
        if sub[:2] == ("config", "user.email"):
            if self.user_email:
                return _Completed(stdout=self.user_email + "\n", returncode=0)
            return _Completed(returncode=1)
        if sub[:1] == ("add",):
            return _Completed(returncode=0)
        if sub[:1] == ("commit",):
            if self.commit_ok:
                return _Completed(returncode=0)
            return _Completed(returncode=1, stderr="commit failed")
        raise AssertionError("unstubbed git command: %r" % (argv,))


def _which_git_present(name):
    return "/usr/bin/git" if name == "git" else None


def _which_git_absent(name):
    return None


def _run_that_must_not_be_called(argv, **kwargs):
    raise AssertionError(
        "run() was invoked with %r but the code path being tested must not "
        "shell out at all" % (argv,)
    )


# --- fresh scaffold ---------------------------------------------------------


def test_ensure_project_scaffolds_an_empty_directory(tmp_path):
    """A fresh call over an empty directory creates both scaffold
    directories and both generated files, and reports every one of them
    through ``created`` rather than ``kept``."""
    git = _FakeGit(state="clear", toplevel=tmp_path)
    result = project.ensure_project(tmp_path, run=git, which=_which_git_present)

    assert set(result.created) == {"models", "images", ".gitignore", "CLAUDE.md"}
    assert result.kept == ()
    assert result.regenerated == ()
    assert (tmp_path / "models").is_dir()
    assert (tmp_path / "images").is_dir()
    assert (tmp_path / ".gitignore").is_file()
    assert (tmp_path / "CLAUDE.md").is_file()


def test_ensure_project_does_not_scaffold_a_review_directory(tmp_path):
    """``review/`` is created lazily by the first transcript export, not by
    scaffolding, so a fresh call must never bring it into existence."""
    project.ensure_project(tmp_path, run=_FakeGit(toplevel=tmp_path), which=_which_git_present)
    assert not (tmp_path / "review").exists()


def test_gitignore_body_ignores_mesh_dir_but_tracks_the_scaffold_dirs():
    body = project.gitignore_body()
    pattern_lines = [line for line in body.splitlines() if line and not line.startswith("#")]
    assert ".mesh/" in pattern_lines
    assert "!.mesh/config.toml" in pattern_lines
    assert "models/" not in pattern_lines
    assert "images/" not in pattern_lines
    assert "git-lfs" in body


def test_claude_md_body_names_every_exchange_file(tmp_path):
    from annealage_mesh import paths

    body = project.claude_md_body(tmp_path)
    assert paths.COMMENTS_JSON_NAME in body
    assert paths.COMMENTS_LOG_NAME in body
    assert paths.CALLOUTS_JSON_NAME in body
    assert "models/" in body
    assert paths.IMAGES_DIRNAME in body


# --- idempotency and force --------------------------------------------------


def test_ensure_project_is_idempotent_on_a_second_call(tmp_path):
    """A second call over a folder the first call already scaffolded writes
    nothing new: every generated file and directory comes back through
    ``kept``, and the git side reports the folder as already a repository
    rather than attempting a second ``init``."""
    first = project.ensure_project(
        tmp_path, run=_FakeGit(state="clear", toplevel=tmp_path), which=_which_git_present
    )
    assert first.git.initialised is True

    second_git = _FakeGit(state="root", toplevel=tmp_path)
    second = project.ensure_project(tmp_path, run=second_git, which=_which_git_present)

    assert second.created == ()
    assert set(second.kept) == {"models", "images", ".gitignore", "CLAUDE.md"}
    assert second.regenerated == ()
    assert second.git == project.GitResult(False, False, "already a git repository")
    assert ("git", "init") not in [c[:2] for c in second_git.calls]
    assert ("git", "commit") not in [c[:2] for c in second_git.calls]


def test_force_regenerates_gitignore_and_claude_md_but_never_the_directories(tmp_path):
    """``force=True`` rewrites the two generated files back to their
    canonical content even when something else has since been written into
    them, but a scaffold directory that already exists is always ``kept``:
    there is no content of a plain directory for ``force`` to reconcile."""
    project.ensure_project(tmp_path, run=_FakeGit(toplevel=tmp_path), which=_which_git_present)
    (tmp_path / ".gitignore").write_text("hand-edited\n")
    (tmp_path / "CLAUDE.md").write_text("hand-edited\n")

    result = project.ensure_project(tmp_path, force=True, git=False, which=_which_git_absent)

    assert result.created == ()
    assert result.kept == ("models", "images")
    assert set(result.regenerated) == {".gitignore", "CLAUDE.md"}
    assert (tmp_path / ".gitignore").read_text() == project.gitignore_body()
    assert (tmp_path / "CLAUDE.md").read_text() == project.claude_md_body(tmp_path)


def test_a_human_authored_claude_md_is_left_byte_identical(tmp_path):
    """``CLAUDE.md`` is only ever generated when absent; a human's own
    content there survives a scaffold call untouched, down to the byte,
    because ``force`` was not asked for."""
    project.ensure_project(tmp_path, run=_FakeGit(toplevel=tmp_path), which=_which_git_present)
    custom = b"# My own notes\r\nSome trailing whitespace \nand no final newline"
    (tmp_path / "CLAUDE.md").write_bytes(custom)

    result = project.ensure_project(tmp_path, git=False, which=_which_git_absent)

    assert (tmp_path / "CLAUDE.md").read_bytes() == custom
    assert "CLAUDE.md" in result.kept
    assert "CLAUDE.md" not in result.created
    assert "CLAUDE.md" not in result.regenerated


# --- the git decision tree --------------------------------------------------


def test_git_not_installed_is_reported_and_never_shells_out(tmp_path):
    """When ``which`` cannot find git, ``ensure_project`` must not call
    ``run`` at all: there is nothing a git subprocess call could answer once
    the binary itself is confirmed absent."""
    result = project.ensure_project(
        tmp_path, run=_run_that_must_not_be_called, which=_which_git_absent
    )
    assert result.git == project.GitResult(False, False, "git is not installed")


def test_already_a_git_repository_is_told_apart_from_nested(tmp_path):
    """A folder that is itself a repository's top level is reported
    distinctly from one merely sitting inside a repository rooted higher up,
    and neither one runs ``git init``."""
    git = _FakeGit(state="root", toplevel=tmp_path)
    result = project.ensure_project(tmp_path, run=git, which=_which_git_present)

    assert result.git == project.GitResult(False, False, "already a git repository")
    assert not any(c[1] == "init" for c in git.calls)


def test_a_folder_inside_a_larger_repository_is_not_reinitialised(tmp_path):
    """A folder nested inside an ancestor's repository gets its own distinct
    reason, and, like the folder-is-itself-a-repository case, no ``git
    init`` is attempted: a second repository nested inside the first is not
    a fix this function can usefully make."""
    parent = tmp_path.parent
    git = _FakeGit(state="nested", toplevel=parent)
    result = project.ensure_project(tmp_path, run=git, which=_which_git_present)

    assert result.git == project.GitResult(False, False, "already inside a git repository")
    assert not any(c[1] == "init" for c in git.calls)


def test_user_email_unset_leaves_the_init_standing_but_skips_the_commit(tmp_path):
    """``git init`` still runs when ``user.email`` is unset, since the
    repository itself is harmless with nothing committed to it, but no
    commit is attempted: ``add`` and ``commit`` must never be called once
    the identity check has already failed."""
    git = _FakeGit(state="clear", user_email=None, toplevel=tmp_path)
    result = project.ensure_project(tmp_path, run=git, which=_which_git_present)

    assert result.git == project.GitResult(
        True, False, "git user.email is unset, so nothing was committed"
    )
    assert any(c[1] == "init" for c in git.calls)
    assert not any(c[1] in ("add", "commit") for c in git.calls)
    assert (tmp_path / ".git").exists() is False  # the init itself is faked, not real


def test_clean_init_and_commit_reports_both_true_with_no_reason(tmp_path):
    git = _FakeGit(state="clear", user_email="dev@example.com", toplevel=tmp_path)
    result = project.ensure_project(tmp_path, run=git, which=_which_git_present)

    assert result.git == project.GitResult(True, True, None)
    calls = [c[1] for c in git.calls]
    assert calls.index("init") < calls.index("add") < calls.index("commit")


def test_git_false_skips_the_git_side_entirely(tmp_path):
    """``git=False`` (``--no-git``) must not probe git at all: ``result.git``
    is ``None``, distinct from every outcome a real attempt can produce."""
    result = project.ensure_project(
        tmp_path, git=False, run=_run_that_must_not_be_called, which=_which_git_absent
    )
    assert result.git is None


def test_git_init_failure_is_raised_rather_than_swallowed(tmp_path):
    """An unanticipated ``git init`` failure (permissions, a corrupted
    ``.git`` left by something else) is not one of the four outcomes this
    module models, so it is raised loudly instead of being folded into a
    ``skipped_reason`` a caller might mistake for one of the ordinary ones."""
    git = _FakeGit(state="clear", init_ok=False, toplevel=tmp_path)
    with pytest.raises(RuntimeError):
        project.ensure_project(tmp_path, run=git, which=_which_git_present)


def test_git_commit_failure_is_raised_rather_than_swallowed(tmp_path):
    git = _FakeGit(state="clear", commit_ok=False, toplevel=tmp_path)
    with pytest.raises(RuntimeError):
        project.ensure_project(tmp_path, run=git, which=_which_git_present)


def test_ensure_project_creates_the_project_directory_itself_if_absent(tmp_path):
    """``project_dir`` need not already exist: a directory named on the
    command line for ``init`` that has not been ``mkdir``'d yet is created
    along with everything under it."""
    target = tmp_path / "not-yet-created"
    result = project.ensure_project(target, git=False, which=_which_git_absent)
    assert target.is_dir()
    assert set(result.created) >= {"models", "images"}
