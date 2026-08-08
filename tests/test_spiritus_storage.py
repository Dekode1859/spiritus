"""Storage primitives: containment, round trips, and the absence of folder semantics."""
from __future__ import annotations

import pytest

from spiritus import storage


@pytest.fixture
def root(tmp_path):
    ws = tmp_path / "workspace"
    ws.mkdir()
    return ws


class TestPathContainment:
    def test_plain_parent_traversal_is_blocked(self, root):
        with pytest.raises(ValueError, match="escapes the storage root"):
            storage._safe(root, "../../etc/passwd")

    def test_sibling_with_shared_name_prefix_is_blocked(self, root):
        """Regression: a string-prefix containment check accepted this.

        Root ``…/workspace`` and sibling ``…/workspace-evil`` share a prefix, so
        ``str(resolved).startswith(str(root))`` was true for a path plainly
        outside the root.
        """
        (root.parent / "workspace-evil").mkdir()
        with pytest.raises(ValueError, match="escapes the storage root"):
            storage._safe(root, "../workspace-evil/secret.md")

    def test_absolute_path_escape_is_blocked(self, root, tmp_path):
        outside = tmp_path.parent / "elsewhere.md"
        with pytest.raises(ValueError, match="escapes the storage root"):
            storage._safe(root, str(outside))

    def test_nested_relative_paths_are_allowed(self, root):
        assert storage._safe(root, "a/b/c.md") == (root / "a" / "b" / "c.md").resolve()

    def test_traversal_that_lands_back_inside_is_allowed(self, root):
        assert storage._safe(root, "a/../b.md") == (root / "b.md").resolve()

    @pytest.mark.parametrize("op", ["read", "delete"])
    def test_public_api_enforces_containment_too(self, root, op):
        (root.parent / "workspace-evil").mkdir(exist_ok=True)
        with pytest.raises(ValueError):
            getattr(storage, op)(root, "../workspace-evil/secret.md")

    def test_write_enforces_containment(self, root):
        (root.parent / "workspace-evil").mkdir(exist_ok=True)
        with pytest.raises(ValueError):
            storage.write(root, "../workspace-evil/planted.md", "x")
        assert not (root.parent / "workspace-evil" / "planted.md").exists()


class TestRoundTrip:
    def test_write_then_read_returns_the_content(self, root):
        written = storage.write(root, "notes/hello.md", "# hi")
        assert written["ok"] is True
        assert written["path"] == "notes/hello.md"

        got = storage.read(root, "notes/hello.md")
        assert got["content"] == "# hi"
        assert got["path"] == "notes/hello.md"
        assert isinstance(got["modified"], int)

    def test_write_creates_missing_parent_directories(self, root):
        storage.write(root, "deep/deeper/file.md", "x")
        assert (root / "deep" / "deeper" / "file.md").is_file()

    def test_write_overwrites_rather_than_appends(self, root):
        storage.write(root, "f.md", "first")
        storage.write(root, "f.md", "second")
        assert storage.read(root, "f.md")["content"] == "second"

    def test_unicode_survives_the_round_trip(self, root):
        storage.write(root, "u.md", "em—dash ± α")
        assert storage.read(root, "u.md")["content"] == "em—dash ± α"

    def test_reading_a_missing_file_returns_an_error_dict(self, root):
        assert storage.read(root, "nope.md") == {"error": "File not found: nope.md"}

    def test_deleting_a_missing_file_returns_an_error_dict(self, root):
        assert storage.delete(root, "nope.md") == {"error": "File not found: nope.md"}

    def test_delete_removes_the_file(self, root):
        storage.write(root, "gone.md", "x")
        assert storage.delete(root, "gone.md") == {"ok": True, "path": "gone.md"}
        assert not (root / "gone.md").exists()


class TestListing:
    def test_lists_only_text_suffixes(self, root):
        for name in ("a.md", "b.txt", "c.pdf", "d.json"):
            (root / name).write_text("x", encoding="utf-8")
        assert [e["name"] for e in storage.list_dir(root)] == ["a.md", "b.txt"]

    def test_entries_are_sorted_and_carry_relative_paths(self, root):
        sub = root / "notes"
        sub.mkdir()
        (sub / "z.md").write_text("zz", encoding="utf-8")
        (sub / "a.md").write_text("a", encoding="utf-8")

        entries = storage.list_dir(root, "notes")
        assert [e["name"] for e in entries] == ["a.md", "z.md"]
        assert entries[0]["path"].replace("\\", "/") == "notes/a.md"
        assert entries[1]["size"] == 2

    def test_listing_is_not_recursive(self, root):
        (root / "top.md").write_text("x", encoding="utf-8")
        (root / "sub").mkdir()
        (root / "sub" / "nested.md").write_text("x", encoding="utf-8")
        assert [e["name"] for e in storage.list_dir(root)] == ["top.md"]

    def test_missing_directory_lists_empty_rather_than_raising(self, root):
        assert storage.list_dir(root, "does-not-exist") == []

    def test_a_root_spelled_differently_from_its_resolved_form_still_lists(self, root):
        """Regression: `relative_to` raised when the two spellings differed.

        _safe() resolves the paths it returns, but the root was used as given.
        Anywhere those differ — a Windows 8.3 short name (RUNNER~1), the macOS
        /var → /private/var symlink, or a path containing `..` — listing a
        subfolder blew up with "is not in the subpath of". Local runs never saw
        it because their temp paths happen to already be canonical.
        """
        (root / "notes").mkdir()
        (root / "notes" / "a.md").write_text("hi", encoding="utf-8")
        (root.parent / "detour").mkdir(exist_ok=True)

        indirect = root.parent / "detour" / ".." / root.name
        assert indirect.resolve() == root.resolve()
        assert indirect != root                      # same place, different spelling

        entries = storage.list_dir(indirect, "notes")
        assert [e["name"] for e in entries] == ["a.md"]
        assert entries[0]["path"].replace("\\", "/") == "notes/a.md"

    def test_count_dir_counts_only_text_files(self, root):
        (root / "sub").mkdir()
        for name in ("a.md", "b.txt", "c.bin"):
            (root / "sub" / name).write_text("x", encoding="utf-8")
        assert storage.count_dir(root, "sub") == 2

    def test_count_dir_on_missing_directory_is_zero(self, root):
        assert storage.count_dir(root, "nope") == 0


class TestNoFolderSemantics:
    """Spiritus must not invent, require, or privilege any folder name."""

    def test_ensure_dirs_creates_exactly_what_the_app_asked_for(self, root):
        storage.ensure_dirs(root, ["alpha", "beta"])
        assert sorted(p.name for p in root.iterdir()) == ["alpha", "beta"]

    def test_ensure_dirs_with_no_names_creates_only_the_root(self, tmp_path):
        target = tmp_path / "fresh"
        storage.ensure_dirs(target, [])
        assert target.is_dir()
        assert list(target.iterdir()) == []

    def test_ensure_dirs_is_idempotent(self, root):
        storage.ensure_dirs(root, ["alpha"])
        (root / "alpha" / "keep.md").write_text("x", encoding="utf-8")
        storage.ensure_dirs(root, ["alpha"])
        assert (root / "alpha" / "keep.md").exists()

    def test_timestamped_name_uses_the_folder_it_is_given(self):
        assert storage.timestamped_name("anything").startswith("anything/")

    def test_timestamped_name_slugifies_and_truncates_the_title(self):
        name = storage.timestamped_name("f", "A Very Long Title " * 5)
        slug = name.split("-", 3)[-1].removesuffix(".md")
        assert " " not in slug
        assert len(slug) <= 40

    def test_timestamped_name_falls_back_to_note(self):
        assert storage.timestamped_name("f").endswith("-note.md")
