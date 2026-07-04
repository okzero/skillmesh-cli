"""Test CAS blob storage: content hash determinism, idempotent writes, materialization.

Covers F3.1, F3.3 (CAS blob, materialization).
"""
import json
import os
import shutil
from pathlib import Path

import pytest

from skillmesh import cas


def _make_skill(tmp_path, name="test-skill", content="# hello", fmt="SKILL.md"):
    skill_dir = tmp_path / name
    skill_dir.mkdir(parents=True)
    (skill_dir / fmt).write_text(content)
    return skill_dir


def test_content_hash_deterministic(tmp_path):
    """Same content produces same hash."""
    skill1 = _make_skill(tmp_path, "skill1", "# hello")
    sub = tmp_path / "sub"
    sub.mkdir()
    skill2 = _make_skill(sub, "skill1", "# hello")  # same name + content
    h1 = cas.compute_content_hash(skill1, "skill1", "skill-md", "")
    h2 = cas.compute_content_hash(skill2, "skill1", "skill-md", "")
    assert h1 == h2  # same name + content = same hash

    # Different name with same content = different hash
    skill3 = _make_skill(tmp_path, "skill2", "# hello")
    h3 = cas.compute_content_hash(skill3, "skill2", "skill-md", "")
    assert h1 != h3


def test_content_hash_excludes_junk_files(tmp_path):
    """Junk files (.DS_Store, __pycache__, .pyc) excluded from hash."""
    skill1 = _make_skill(tmp_path, "s1", "# x")
    (skill1 / ".DS_Store").write_text("junk")
    (skill1 / "Thumbs.db").write_text("junk")

    sub = tmp_path / "sub"
    sub.mkdir()
    skill2 = _make_skill(sub, "s1", "# x")

    h1 = cas.compute_content_hash(skill1, "s1", "skill-md", "")
    h2 = cas.compute_content_hash(skill2, "s1", "skill-md", "")
    assert h1 == h2  # junk excluded


def test_content_hash_normalizes_line_endings(tmp_path):
    """CRLF and LF produce same hash."""
    skill1 = _make_skill(tmp_path, "s1", "line1\nline2\n")
    sub = tmp_path / "sub"
    sub.mkdir()
    skill2 = _make_skill(sub, "s1", "line1\r\nline2\r\n")
    h1 = cas.compute_content_hash(skill1, "s1", "skill-md", "")
    h2 = cas.compute_content_hash(skill2, "s1", "skill-md", "")
    assert h1 == h2


def test_write_blob_idempotent(tmp_path):
    """T13: writing same content twice produces one blob."""
    skill = _make_skill(tmp_path, "my-skill", "# content")
    blobs_dir = tmp_path / "blobs"
    blobs_dir.mkdir()

    h1 = cas.write_blob(skill, "my-skill", "skill-md", "1.0.0", blobs_dir, "host-1")
    h2 = cas.write_blob(skill, "my-skill", "skill-md", "1.0.0", blobs_dir, "host-1")
    assert h1 == h2
    # Only one blob dir
    blob_dirs = [d for d in blobs_dir.iterdir() if d.is_dir() and not d.name.startswith(".")]
    assert len(blob_dirs) == 1


def test_write_blob_creates_meta(tmp_path):
    skill = _make_skill(tmp_path, "my-skill", "# content")
    blobs_dir = tmp_path / "blobs"
    blobs_dir.mkdir()

    content_hash = cas.write_blob(skill, "my-skill", "skill-md", "1.0.0", blobs_dir, "host-xyz")
    meta = cas.read_meta(content_hash, blobs_dir)
    assert meta is not None
    assert meta.skill_name == "my-skill"
    assert meta.format == "skill-md"
    assert meta.version == "1.0.0"
    assert meta.created_by_host == "host-xyz"
    assert meta.content_hash == content_hash
    assert meta.blob_hash.startswith("sha256:")


def test_materialize_restores_skill(tmp_path):
    """F3.3: blob can be materialized to skills/<name>/."""
    skill = _make_skill(tmp_path, "my-skill", "# content here")
    blobs_dir = tmp_path / "blobs"
    skills_dir = tmp_path / "skills"
    blobs_dir.mkdir()
    skills_dir.mkdir()

    content_hash = cas.write_blob(skill, "my-skill", "skill-md", "1.0.0", blobs_dir, "host-1")
    target = cas.materialize(content_hash, "my-skill", skills_dir, blobs_dir)

    assert target.exists()
    assert (target / "SKILL.md").read_text() == "# content here"
    # .meta.json should NOT be in materialized dir
    assert not (target / ".meta.json").exists()


def test_materialize_overwrites_existing_symlink(tmp_path):
    """materialize replaces existing symlink (skillmesh-owned)."""
    skill = _make_skill(tmp_path, "my-skill", "# new")
    blobs_dir = tmp_path / "blobs"
    skills_dir = tmp_path / "skills"
    blobs_dir.mkdir()
    skills_dir.mkdir()

    content_hash = cas.write_blob(skill, "my-skill", "skill-md", "1.0.0", blobs_dir, "host-1")
    target = skills_dir / "my-skill"
    # Pre-existing symlink (skillmesh-owned, safe to replace)
    os.symlink(tmp_path / "old-target", target)
    (tmp_path / "old-target").mkdir()

    cas.materialize(content_hash, "my-skill", skills_dir, blobs_dir)
    assert (target / "SKILL.md").exists()


def test_materialize_refuses_nonempty_user_dir(tmp_path):
    """B5: materialize refuses to delete non-empty dir without marker (user content)."""
    skill = _make_skill(tmp_path, "my-skill", "# new")
    blobs_dir = tmp_path / "blobs"
    skills_dir = tmp_path / "skills"
    blobs_dir.mkdir()
    skills_dir.mkdir()

    content_hash = cas.write_blob(skill, "my-skill", "skill-md", "1.0.0", blobs_dir, "host-1")
    target = skills_dir / "my-skill"
    target.mkdir()  # pre-existing user dir
    (target / "user-content.txt").write_text("user data")

    with pytest.raises(RuntimeError, match="refusing to delete"):
        cas.materialize(content_hash, "my-skill", skills_dir, blobs_dir)
    # User content preserved
    assert (target / "user-content.txt").exists()


def test_materialize_overwrites_skillmesh_owned_dir(tmp_path):
    """materialize replaces dir with .skillmesh-owned marker."""
    skill = _make_skill(tmp_path, "my-skill", "# new")
    blobs_dir = tmp_path / "blobs"
    skills_dir = tmp_path / "skills"
    blobs_dir.mkdir()
    skills_dir.mkdir()

    content_hash = cas.write_blob(skill, "my-skill", "skill-md", "1.0.0", blobs_dir, "host-1")
    target = skills_dir / "my-skill"
    target.mkdir()
    (target / ".skillmesh-owned").write_text("skillmesh\n")
    (target / "old.txt").write_text("old")

    cas.materialize(content_hash, "my-skill", skills_dir, blobs_dir)
    assert (target / "SKILL.md").exists()
    assert not (target / "old.txt").exists()  # old content replaced


def test_different_content_different_hash(tmp_path):
    """Different content produces different hash."""
    skill1 = _make_skill(tmp_path, "s", "# content A")
    sub = tmp_path / "sub"
    sub.mkdir()
    skill2 = _make_skill(sub, "s", "# content B")
    h1 = cas.compute_content_hash(skill1, "s", "skill-md", "")
    h2 = cas.compute_content_hash(skill2, "s", "skill-md", "")
    assert h1 != h2


def test_version_affects_hash(tmp_path):
    """Same content + different version = different hash (version is part of canonical form)."""
    skill = _make_skill(tmp_path, "s", "# content")
    h1 = cas.compute_content_hash(skill, "s", "skill-md", "1.0.0")
    h2 = cas.compute_content_hash(skill, "s", "skill-md", "2.0.0")
    assert h1 != h2
