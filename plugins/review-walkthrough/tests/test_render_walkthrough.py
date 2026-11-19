"""Check artifact construction against real Git patches and malformed review data."""

import copy
import importlib.util
import json
import subprocess
import sys
from html.parser import HTMLParser
from pathlib import Path

import pytest

SCRIPT = (
    Path(__file__).resolve().parents[1] / "skills/review-walkthrough/scripts/render_walkthrough.py"
)
SPEC = importlib.util.spec_from_file_location("walkthrough_renderer", SCRIPT)
renderer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(renderer)


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args])


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / "repo"
    path.mkdir()
    git(path, "init", "-q")
    git(path, "config", "user.name", "Walkthrough Fixture")
    git(path, "config", "user.email", "fixture@example.invalid")
    git(path, "config", "commit.gpgsign", "false")
    git(path, "config", "diff.renames", "true")
    (path / "a.txt").write_text("old\ncontext\n", encoding="utf-8")
    (path / "deleted.txt").write_text("deleted\n", encoding="utf-8")
    (path / "image.bin").write_bytes(b"\x00old")
    (path / "rename.txt").write_text("rename only\n", encoding="utf-8")
    git(path, "add", ".")
    git(path, "commit", "-qm", "Initial fixture")
    return path


def patch_data(repo):
    patch = git(repo, "diff", "--no-ext-diff", "--no-textconv", "--no-color", "HEAD", "--").decode()
    blocks = renderer.split_diff(patch)
    paths = renderer.patch_paths(patch, len(blocks))
    data = {
        "title": "Fixture review",
        "steps": [
            {"sha": f"step-{i + 1}", "message": path, "files": [path], "diff": block}
            for i, (path, block) in enumerate(zip(paths, blocks, strict=True))
        ],
        "explanations": ["<p>Explain this change.</p>" for _ in blocks],
        "reviews": [[] for _ in blocks],
        "pr_meta": None,
    }
    return patch, data


@pytest.fixture
def changed(repo):
    (repo / "a.txt").write_text("new\ncontext\n", encoding="utf-8")
    return patch_data(repo)


def finding(**anchor):
    return {"severity": "medium", "title": "Finding", "body": "<p>Impact.</p>", **anchor}


def test_real_patch_covers_deletion_rename_binary_and_quoted_paths(repo):
    (repo / "deleted.txt").unlink()
    (repo / "image.bin").write_bytes(b"\x00new")
    git(repo, "mv", "rename.txt", "renamed.txt")
    odd_path = 'quote" tab\t newline\n snow-雪.txt'
    (repo / odd_path).write_bytes(b"first\r\nsecond\r\n")
    git(repo, "add", "-N", odd_path)
    patch, data = patch_data(repo)
    result = renderer.normalize_data(data, patch)
    paths = [step["files"][0] for step in result["steps"]]
    assert set(paths) == {"deleted.txt", "image.bin", "renamed.txt", odd_path}
    assert "Binary files" in patch
    assert "rename to renamed.txt" in patch
    assert "\r\n" in patch
    assert len(result["steps"]) == 4


def test_step_order_can_change_without_changing_any_diff(repo):
    (repo / "a.txt").write_text("new\n", encoding="utf-8")
    (repo / "deleted.txt").unlink()
    patch, data = patch_data(repo)
    data["steps"].reverse()
    result = renderer.normalize_data(data, patch)
    assert result["steps"][0]["files"] == ["deleted.txt"]
    assert [step["sha"] for step in result["steps"]] == ["step-1", "step-2"]


@pytest.mark.parametrize(
    "mutation", ["missing", "duplicate", "invented", "truncated", "wrong_file"]
)
def test_rejects_incomplete_or_fabricated_diff(repo, mutation):
    (repo / "a.txt").write_text("new\n", encoding="utf-8")
    (repo / "deleted.txt").unlink()
    patch, data = patch_data(repo)
    if mutation == "missing":
        for key in ("steps", "explanations", "reviews"):
            data[key].pop()
    elif mutation == "duplicate":
        data["steps"][1] = copy.deepcopy(data["steps"][0])
    elif mutation == "invented":
        data["steps"][0]["diff"] = data["steps"][0]["diff"].replace("+new", "+invented")
    elif mutation == "truncated":
        data["steps"][0]["diff"] = data["steps"][0]["diff"].rstrip("\n")
    else:
        data["steps"][0]["files"] = ["wrong.txt"]
    with pytest.raises(ValueError):
        renderer.render(data, patch)


@pytest.mark.parametrize("side", ["LEFT", "RIGHT"])
def test_deleted_and_added_line_anchors_preserve_side(changed, side):
    patch, data = changed
    data["reviews"][0] = [finding(file="a.txt", line=1, side=side)]
    result = renderer.normalize_data(data, patch)
    assert result["reviews"][0][0]["side"] == side


@pytest.mark.parametrize("side", ["LEFT", "RIGHT"])
def test_context_anchor_preserves_requested_side(changed, side):
    patch, data = changed
    data["reviews"][0] = [finding(file="a.txt", line=2, side=side)]
    assert renderer.normalize_data(data, patch)["reviews"][0][0]["side"] == side


@pytest.mark.parametrize(
    "anchor",
    [
        {"file": "a.txt", "line": 1},  # Ambiguous old/new coordinate.
        {"file": "a.txt", "line": 10, "side": "RIGHT"},
        {"file": "other.txt", "line": 1, "side": "RIGHT"},
        {"file": "a.txt", "line": True, "side": "RIGHT"},
        {"file": "a.txt", "line": 2, "end_line": 1, "side": "RIGHT"},
        {"file": "a.txt", "line": 1, "side": "WRONG"},
        {"file": "a.txt"},
        {"line": 1},
    ],
)
def test_rejects_invalid_or_ambiguous_anchors(changed, anchor):
    patch, data = changed
    data["reviews"][0] = [finding(**anchor)]
    with pytest.raises(ValueError):
        renderer.normalize_data(data, patch)


def test_anchor_cannot_span_separate_hunks(repo):
    (repo / "a.txt").write_text("\n".join(str(i) for i in range(50)) + "\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "Long file")
    (repo / "a.txt").write_text("changed\n" + "\n".join(str(i) for i in range(1, 49)) + "\nend\n")
    patch, data = patch_data(repo)
    data["reviews"][0] = [finding(file="a.txt", line=1, end_line=50, side="RIGHT")]
    with pytest.raises(ValueError, match="within one hunk"):
        renderer.normalize_data(data, patch)


def test_safe_embedding_preserves_script_tags_and_placeholder_words(changed):
    patch, data = changed
    hostile = "</script><script>window.injected=true</script> TITLE_PLACEHOLDER"
    data["title"] = hostile
    data["explanations"][0] = hostile
    page = renderer.render(data, patch)
    assert "<script>window.injected" not in page
    assert "&lt;/script&gt;" in page
    assert r"\u003c/script>" in page
    assert "TITLE_PLACEHOLDER" in page  # Valid content, not an unfilled template slot.

    class ScriptCounter(HTMLParser):
        count = 0

        def handle_starttag(self, tag, attrs):
            if tag == "script":
                self.count += 1

    parser = ScriptCounter()
    parser.feed(page)
    assert parser.count == 1


@pytest.mark.parametrize("value", [None, [], {}, {"title": "Empty", "steps": []}])
def test_rejects_empty_or_wrong_input(changed, value):
    patch, _ = changed
    with pytest.raises(ValueError):
        renderer.render(value, patch)


@pytest.mark.parametrize(
    "field,value", [("owner", "$(id)"), ("repo", "a/b"), ("pr_number", 0), ("head_sha", "abc")]
)
def test_rejects_invalid_pr_metadata(changed, field, value):
    patch, data = changed
    data["pr_meta"] = {"owner": "acme", "repo": "project", "pr_number": 1, "head_sha": "a" * 40}
    data["pr_meta"][field] = value
    with pytest.raises(ValueError):
        renderer.render(data, patch)


def test_cli_writes_output_and_keeps_existing_output_on_bad_input(tmp_path, changed):
    patch, data = changed
    source = tmp_path / "input.json"
    diff = tmp_path / "change.patch"
    output = tmp_path / "walkthrough.html"
    source.write_text(json.dumps(data))
    diff.write_bytes(patch.encode())
    command = [
        sys.executable,
        str(SCRIPT),
        "--input",
        str(source),
        "--diff",
        str(diff),
        "--output",
        str(output),
    ]
    subprocess.run(command, check=True, capture_output=True)
    assert "Fixture review" in output.read_text()
    source.write_text('{"steps": []}')
    before = output.read_bytes()
    result = subprocess.run(command, capture_output=True)
    assert result.returncode != 0
    assert b"Cannot render walkthrough" in result.stderr
    assert output.read_bytes() == before


def test_rejects_malformed_patch_and_missing_template_slots(changed):
    patch, data = changed
    with pytest.raises(ValueError, match="parse"):
        renderer.patch_paths("diff --git a/x b/x\n@@ -1 +1 @@\n", 1)
    with pytest.raises(ValueError, match="nonempty Git unified diff"):
        renderer.split_diff("")
    with pytest.raises(ValueError, match="Template must contain"):
        renderer.render(data, patch, template="<html></html>")


JAVA_BLOCK = (
    "diff --git a/A.java b/A.java\n"
    "index 1111111..2222222 100644\n"
    "--- a/A.java\n"
    "+++ b/A.java\n"
    "@@ -1,4 +1,4 @@\n"
    " /* opens\n"
    "-   closes */ int count = 1;\n"
    "+   closes */ long count = 1;\n"
    ' @Override public String name() { return "a"; } // done\n'
)


def kinds(tokens, kind):
    return [text for token_kind, text in tokens if token_kind == kind]


def test_highlights_lex_each_hunk_side_as_a_block_in_github_color_classes():
    rows = renderer.highlight_rows(JAVA_BLOCK, "A.java")
    assert ["".join(text for _, text in row) for row in rows] == [
        "/* opens",
        "   closes */ int count = 1;",
        "   closes */ long count = 1;",
        ' @Override public String name() { return "a"; } // done'[1:],
    ]
    assert kinds(rows[1], "com") == ["   closes */"]
    assert kinds(rows[1], "kw") == ["int"]
    assert kinds(rows[2], "kw") == ["long"]
    assert kinds(rows[3], "ann") == ["@Override"]
    assert kinds(rows[3], "str") == ['"a"']
    assert kinds(rows[3], "fn") == ["name"]
    assert kinds(rows[3], "com") == ["// done"]


@pytest.mark.parametrize(
    ("path", "line", "kind", "text"),
    [
        ("model.klass", "class Question", "kw", "class"),
        ("schema.ddl", "create table FOO (ID bigint);", "kw", "create"),
        ("settings.jsonc", '{"key": 1} // note', "com", "// note"),
        ("pom.xml.vm", "<version>${version}</version>", "tag", "<version>"),
        ("Klass.g4", "grammar Klass;", "kw", "grammar"),
    ],
)
def test_highlights_cover_aliases_and_the_klass_language(path, line, kind, text):
    block = f"diff --git a/{path} b/{path}\n@@ -0,0 +1 @@\n+{line}\n"
    (row,) = renderer.highlight_rows(block, path)
    assert "".join(token_text for _, token_text in row) == line
    assert text in kinds(row, kind)


def test_files_without_a_lexer_have_no_highlights():
    block = "diff --git a/notes.unknown b/notes.unknown\n@@ -0,0 +1 @@\n+plain\n"
    assert renderer.highlight_rows(block, "notes.unknown") is None


def test_normalized_steps_carry_highlights_for_each_file(changed):
    patch, data = changed
    result = renderer.normalize_data(data, patch)
    for step in result["steps"]:
        assert set(step["highlights"]) == set(step["files"])
