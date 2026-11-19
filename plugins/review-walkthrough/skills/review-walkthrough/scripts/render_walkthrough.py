#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["pygments>=2.19"]
# ///
"""Validate a walkthrough against a captured Git patch and render standalone HTML."""

import argparse
import html
import json
import re
import subprocess
import tempfile
from collections import Counter
from pathlib import Path

from pygments.lexer import RegexLexer, words
from pygments.lexers import get_lexer_by_name, get_lexer_for_filename
from pygments.token import (
    Comment,
    Keyword,
    Name,
    Number,
    Operator,
    Punctuation,
    String,
    Text,
    Whitespace,
)
from pygments.util import ClassNotFound

HUNK = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")
TEMPLATE = Path(__file__).resolve().parent.parent / "template.html"

# Pygments token types mapped to the page's GitHub dark color classes. Order matters: the first
# matching ancestor wins, so the specific types (constants, decorators) precede their parents.
TOKEN_CLASSES = (
    (Comment, "com"),
    (String, "str"),
    (Number, "num"),
    (Keyword.Constant, "num"),
    (Keyword, "kw"),
    (Operator.Word, "kw"),
    (Name.Decorator, "ann"),
    (Name.Tag, "tag"),
    (Name.Attribute, "attr"),
    (Name.Function, "fn"),
    (Name.Class, "type"),
    (Name.Builtin, "num"),
    (Name.Constant, "num"),
    (Name.Entity, "attr"),
)

# File extensions that Pygments does not map, or maps less usefully, by filename.
LEXER_ALIASES = {
    ".jsonc": "json5",
    ".g4": "antlr",
    ".ddl": "sql",
    ".fk": "sql",
    ".idx": "sql",
}


class KlassLexer(RegexLexer):
    """Lexer for the Klass modeling language, which Pygments does not know."""

    name = "Klass"
    aliases = ["klass"]
    filenames = ["*.klass"]
    tokens = {
        "root": [
            (r"\s+", Whitespace),
            (r"//.*?$", Comment.Single),
            (r"/\*", Comment.Multiline, "comment"),
            (r'"(\\\\|\\"|[^"])*"', String.Double),
            (r"'(\\\\|\\'|[^'])*'", String.Single),
            (r"\d+(\.\d+)?", Number),
            (words(("true", "false", "null"), suffix=r"\b"), Keyword.Constant),
            (
                words(
                    (
                        "package",
                        "class",
                        "interface",
                        "enumeration",
                        "association",
                        "projection",
                        "service",
                        "on",
                        "relationship",
                        "multiplicity",
                        "criteria",
                        "orderBy",
                        "ascending",
                        "descending",
                        "authorize",
                        "validate",
                        "conflict",
                        "version",
                        "GET",
                        "POST",
                        "PUT",
                        "PATCH",
                        "DELETE",
                        "key",
                        "id",
                        "userId",
                        "audit",
                        "valid",
                        "system",
                        "temporal",
                        "temporalRange",
                        "temporalInstant",
                        "transient",
                        "abstract",
                        "inherits",
                        "implements",
                        "extends",
                        "final",
                        "private",
                        "owned",
                        "derived",
                        "native",
                        "this",
                        "createdBy",
                        "createdOn",
                        "lastUpdatedBy",
                        "systemFrom",
                        "systemTo",
                        "validFrom",
                        "validTo",
                        "optimisticallyLocked",
                        "digest",
                        "digested",
                        "objectStorage",
                        "count",
                        "one",
                        "many",
                    ),
                    suffix=r"\b",
                ),
                Keyword,
            ),
            (r"[A-Z]\w*", Name.Class),
            (r"[a-zA-Z_]\w*", Name),
            (r"[{}()\[\];:,.]", Punctuation),
            (r"[=<>!&|+\-*/%?]+", Operator),
            (r".", Text),
        ],
        "comment": [
            (r"[^*]+", Comment.Multiline),
            (r"\*/", Comment.Multiline, "#pop"),
            (r"\*", Comment.Multiline),
        ],
    }


def read_text(path):
    # Preserve CRLF inside patch content; universal-newline conversion changes the evidence.
    return path.read_bytes().decode("utf-8")


def text_field(value, key, context):
    result = value.get(key)
    if not isinstance(result, str) or not result.strip():
        raise ValueError(f"{context}.{key} must be a nonempty string")
    return result


def lexer_for(path):
    """Pick a Pygments lexer for a file, or None when the file should stay plain text."""
    # Velocity templates are named after the file they generate, e.g. pom.xml.vm.
    name = path.rsplit("/", 1)[-1].removesuffix(".vm")
    suffix = name[name.rfind(".") :] if "." in name else ""
    options = {"stripnl": False, "stripall": False, "ensurenl": False, "tabsize": 0}
    if suffix == ".klass":
        return KlassLexer(**options)
    try:
        if suffix in LEXER_ALIASES:
            return get_lexer_by_name(LEXER_ALIASES[suffix], **options)
        return get_lexer_for_filename(name, **options)
    except ClassNotFound:
        return None


def token_class(token_type):
    for parent, css_class in TOKEN_CLASSES:
        if token_type in parent:
            return css_class
    return None


def lex_lines(lexer, lines):
    """Lex lines as one block so multi-line constructs keep their state, then split per line."""
    rows = [[]]
    for token_type, value in lexer.get_tokens("\n".join(lines) + "\n"):
        css_class = token_class(token_type)
        for index, piece in enumerate(value.split("\n")):
            if index:
                rows.append([])
            if not piece:
                continue
            row = rows[-1]
            if row and row[-1][0] == css_class:
                row[-1][1] += piece
            else:
                row.append([css_class, piece])
    return rows[: len(lines)]


def highlight_rows(block, path):
    """Return syntax tokens for each displayed line of one file's diff, in display order.

    Each hunk is lexed twice: its old side (context and deleted lines) and its new side (context
    and added lines), so comments and strings that span lines are colored the way they appear in
    each version of the file. Returns None when no lexer fits the file.
    """
    lexer = lexer_for(path)
    if lexer is None:
        return None
    hunks = []
    for line in block.split("\n"):
        if HUNK.match(line):
            hunks.append([])
        elif hunks and line[:1] in ("+", "-", " "):
            hunks[-1].append((line[0], line[1:]))
    rows = []
    for hunk in hunks:
        old = lex_lines(lexer, [text for sign, text in hunk if sign != "+"])
        new = lex_lines(lexer, [text for sign, text in hunk if sign != "-"])
        old_index = new_index = 0
        for sign, text in hunk:
            if sign == "-":
                tokens = old[old_index]
                old_index += 1
            else:
                tokens = new[new_index]
                new_index += 1
                if sign == " ":
                    old_index += 1
            # Lexers that rewrite text would misalign the display; fall back to plain text.
            if "".join(piece for _, piece in tokens) != text:
                tokens = [[None, text]] if text else []
            rows.append(tokens)
    return rows


def split_diff(patch):
    if not isinstance(patch, str) or not patch.startswith("diff --git "):
        raise ValueError("Expected a nonempty Git unified diff starting with 'diff --git '")
    return re.split(r"(?=^diff --git )", patch, flags=re.MULTILINE)[1:]


def patch_paths(patch, count):
    # --numstat parses paths using Git's own quoting rules without applying the patch.
    result = subprocess.run(
        ["git", "apply", "--numstat", "-z", "--"],
        input=patch.encode("utf-8"),
        capture_output=True,
        cwd=tempfile.gettempdir(),
        check=False,
    )
    if result.returncode:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise ValueError(f"Git could not parse the captured patch: {detail}")
    records = result.stdout.decode("utf-8").split("\0")
    if records[-1] != "" or len(records) - 1 != count:
        raise ValueError("Git did not return one path per diff block; recapture the complete patch")
    paths = [record.split("\t", 2)[2] for record in records[:-1]]
    if len(set(paths)) != len(paths):
        raise ValueError("The captured patch contains duplicate file paths")
    return paths


def diff_lines(block):
    """Return the line numbers and hunk IDs exposed in the unified diff gutter."""
    lines = {"LEFT": {}, "RIGHT": {}}
    old = new = 0
    hunk = 0
    for line in block.split("\n"):
        match = HUNK.match(line)
        if match:
            old, new = map(int, match.groups())
            hunk += 1
        elif hunk and line.startswith("+"):
            lines["RIGHT"][new] = hunk
            new += 1
        elif hunk and line.startswith("-"):
            lines["LEFT"][old] = hunk
            old += 1
        elif hunk and line.startswith(" "):
            lines["LEFT"][old] = hunk
            lines["RIGHT"][new] = hunk
            old += 1
            new += 1
    return lines


def positive_integer(value):
    return type(value) is int and value > 0


def review_anchor(review, files, context):
    if "file" not in review:
        if any(key in review for key in ("line", "end_line", "side")):
            raise ValueError(f"{context}: an anchor needs a file and line")
        return {}
    path = text_field(review, "file", context)
    if path not in files:
        raise ValueError(f"{context}: {path!r} is not a file in this step")
    start = review.get("line")
    end = review.get("end_line", start)
    if not positive_integer(start) or not positive_integer(end) or end < start:
        raise ValueError(f"{context}: line and end_line must form a positive, ordered range")
    candidates = []
    for side, numbers in files[path].items():
        if start not in numbers or end not in numbers or numbers[start] != numbers[end]:
            continue
        if sum(start <= number <= end for number in numbers) == end - start + 1:
            candidates.append(side)
    side = review.get("side")
    if side is None and len(candidates) == 1:
        side = candidates[0]
    if side not in candidates:
        raise ValueError(
            f"{context}: anchor {path}:{start}-{end} is missing or ambiguous; "
            "choose LEFT for deletions or RIGHT for additions/context within one hunk"
        )
    result = {"file": path, "line": start, "side": side}
    if end != start:
        result["end_line"] = end
    return result


def normalize_reviews(reviews, files, context):
    if not isinstance(reviews, list):
        raise ValueError(f"{context} must be an array (use [] for no findings)")
    normalized = []
    for index, review in enumerate(reviews):
        location = f"{context}[{index}]"
        if not isinstance(review, dict):
            raise ValueError(f"{location} must be an object")
        severity = review.get("severity")
        if severity not in ("high", "medium", "low"):
            raise ValueError(f"{location}.severity must be high, medium, or low")
        normalized.append(
            {
                "severity": severity,
                "title": text_field(review, "title", location),
                "body": text_field(review, "body", location),
                **review_anchor(review, files, location),
            }
        )
    return normalized


def normalize_pr(meta):
    if meta is None:
        return None
    if not isinstance(meta, dict):
        raise ValueError("pr_meta must be an object or null")
    for field in ("owner", "repo"):
        value = text_field(meta, field, "pr_meta")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", value):
            raise ValueError(f"pr_meta.{field} must contain only a GitHub owner or repository name")
    if not positive_integer(meta.get("pr_number")):
        raise ValueError("pr_meta.pr_number must be a positive integer")
    sha = text_field(meta, "head_sha", "pr_meta")
    if not re.fullmatch(r"[0-9a-fA-F]{40}", sha):
        raise ValueError("pr_meta.head_sha must be the full GitHub pull-request head SHA")
    return {key: meta[key] for key in ("owner", "repo", "pr_number", "head_sha")}


def normalize_data(data, patch):
    if not isinstance(data, dict):
        raise ValueError("Walkthrough input must be a JSON object")
    title = text_field(data, "title", "walkthrough")
    steps = data.get("steps")
    if not isinstance(steps, list) or not steps:
        raise ValueError("steps must be a nonempty array")
    for key in ("explanations", "reviews"):
        if not isinstance(data.get(key), list) or len(data[key]) != len(steps):
            raise ValueError(f"{key} must have exactly one entry per step")
    blocks = split_diff(patch)
    paths = patch_paths(patch, len(blocks))
    originals = dict(zip(blocks, paths, strict=True))
    if len(originals) != len(blocks):
        raise ValueError("The captured patch contains duplicate diff blocks")
    seen = []
    normalized_steps = []
    normalized_reviews = []
    for index, step in enumerate(steps):
        context = f"steps[{index}]"
        if not isinstance(step, dict):
            raise ValueError(f"{context} must be an object")
        diff = text_field(step, "diff", context)
        step_blocks = split_diff(diff)
        if any(block not in originals for block in step_blocks):
            raise ValueError(f"{context}: copy each complete file diff verbatim from the patch")
        step_paths = [originals[block] for block in step_blocks]
        if step.get("files") != step_paths:
            raise ValueError(f"{context}.files must match the paths and order in this step's diff")
        explanation = data["explanations"][index]
        if not isinstance(explanation, str) or not explanation.strip():
            raise ValueError(f"explanations[{index}] must be a nonempty HTML string")
        normalized_steps.append(
            {
                "sha": f"step-{index + 1}",
                "message": text_field(step, "message", context),
                "files": step_paths,
                "diff": diff,
                "highlights": {
                    path: highlight_rows(block, path)
                    for path, block in zip(step_paths, step_blocks, strict=True)
                },
            }
        )
        files = {originals[block]: diff_lines(block) for block in step_blocks}
        normalized_reviews.append(
            normalize_reviews(data["reviews"][index], files, f"reviews[{index}]")
        )
        seen.extend(step_blocks)
    if Counter(seen) != Counter(blocks):
        raise ValueError(
            "Missing or repeated files: each captured file diff must appear in exactly one step"
        )
    return {
        "title": title,
        "steps": normalized_steps,
        "explanations": data["explanations"],
        "reviews": normalized_reviews,
        "pr_meta": normalize_pr(data.get("pr_meta")),
    }


def render(data, patch, template=None):
    data = normalize_data(data, patch)
    template = read_text(TEMPLATE) if template is None else template
    replacements = {"TITLE_PLACEHOLDER": html.escape(data["title"])}
    for key in ("steps", "explanations", "reviews", "pr_meta"):
        replacements[f"{key.upper()}_PLACEHOLDER"] = json.dumps(data[key]).replace("<", r"\u003c")
    for placeholder in replacements:
        expected = 2 if placeholder == "TITLE_PLACEHOLDER" else 1
        if template.count(placeholder) != expected:
            raise ValueError(f"Template must contain {expected} occurrence(s) of {placeholder}")
    # One pass prevents placeholder-like source text from being substituted recursively.
    pattern = "|".join(replacements)
    return re.sub(pattern, lambda match: replacements[match[0]], template)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path, help="Walkthrough JSON")
    parser.add_argument("--diff", required=True, type=Path, help="Complete captured Git patch")
    parser.add_argument("--output", required=True, type=Path, help="Standalone HTML output")
    args = parser.parse_args()
    try:
        if args.output.resolve() in (args.input.resolve(), args.diff.resolve(), TEMPLATE):
            raise ValueError("Choose an output path different from the input, patch, and template")
        data = json.loads(read_text(args.input))
        page = render(data, read_text(args.diff))
        args.output.write_text(page, encoding="utf-8")
    except (OSError, UnicodeError, ValueError) as exc:
        parser.exit(1, f"Cannot render walkthrough: {exc}\n")
    print(args.output.resolve())


if __name__ == "__main__":
    main()
