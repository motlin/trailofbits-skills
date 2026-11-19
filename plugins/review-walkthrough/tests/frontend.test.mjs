import assert from 'node:assert/strict';
import { test } from 'node:test';
import { existsSync, mkdtempSync, readFileSync, rmSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
import { data, diff, files, loadFrontend, unusualPath } from './frontend-fixture.mjs';

const { context, evaluate } = loadFrontend();

test('every diff file has a separate container, including deletions and metadata-only changes', () => {
  const rendered = context.renderDiff(diff, files);
  assert.equal((rendered.match(/class="diff-file"/g) || []).length, files.length);
  assert.equal((rendered.match(/<div\b/g) || []).length, (rendered.match(/<\/div>/g) || []).length);
  for (const file of files) assert.ok(rendered.includes(context.escape(file)));
  assert.match(rendered, /Binary files a\/asset.bin and b\/asset.bin differ/);
  assert.match(rendered, /rename to new.txt/);
  assert.match(rendered, /new mode 100755/);
});

test('metadata, no-newline markers, and trailing separators are not commentable lines', () => {
  const rendered = context.renderDiff(diff, files);
  assert.equal((rendered.match(/class="diff-line /g) || []).length, 6);
  assert.match(rendered, /data-file="gone.txt" data-line="1" data-old-line="1" data-new-line=""/);
  assert.match(rendered, /data-line="2" data-old-line="2" data-new-line="2"/);
});

test('line lookup distinguishes both sides and supports shifted context', () => {
  assert.equal(context.lineOnSide({ dataset: { oldLine: '12', newLine: '15' } }, 'LEFT'), 12);
  assert.equal(context.lineOnSide({ dataset: { oldLine: '12', newLine: '15' } }, 'RIGHT'), 15);
  assert.equal(context.lineOnSide({ dataset: { oldLine: '', newLine: '15' } }, 'LEFT'), 0);
});

test('Markdown code blocks preserve whitespace, blank lines, HTML, and formatting markers', () => {
  const code = '  **bold** _em_ ~~strike~~\n\n<sup>x</sup> & `ticks`\n';
  const html = context.markdownToHtml('Before\n\n````js\n' + code + '```\n````\n\nAfter');
  assert.ok(html.includes('<pre><code>' + context.escape(code + '```') + '</code></pre>'));
  assert.ok(!html.includes('<strong>'));
  assert.ok(!html.includes('<sup>'));
  assert.match(html, /^<p>Before<\/p>/);
  assert.match(html, /<p>After<\/p>$/);
});

test('inline code spans preserve Markdown punctuation and variable backtick delimiters', () => {
  const html = context.markdownToHtml('A `` **bold** _em_ <sup>x</sup> `tick` `` and **outside**');
  assert.ok(html.includes('<code>**bold** _em_ &lt;sup&gt;x&lt;/sup&gt; `tick`</code>'));
  assert.ok(html.includes('<strong>outside</strong>'));
  assert.equal(context.markdownToHtml('unclosed `` span'), '<p>unclosed `` span</p>');
  assert.equal(context.markdownToHtml('lone ``'), '<p>lone ``</p>');
});

test('escaped punctuation stays literal within formatted prose', () => {
  const literal = '# title, 1. item, - item, a|b &copy;';
  const md = '**' + context.escapeMarkdown(literal) + '**';
  assert.equal(context.markdownToHtml(md), '<p><strong>' + context.escape(literal) + '</strong></p>');
  assert.equal(context.markdownToHtml('**before `a * b` after**'), '<p><strong>before <code>a * b</code> after</strong></p>');
  assert.equal(context.markdownInline('\u0000 **' + context.escapeMarkdown('# literal') + '**'), '\u0000 <strong># literal</strong>');
});

test('review payload preserves line side, ranges, and literal comment text', () => {
  const comment = {
    file: unusualPath, startLine: 3, endLine: 5, side: 'LEFT', startSide: 'LEFT',
    body: "quotes ' \" `ticks` $(false) \\n and actual\nnewline", stepIndex: 0,
  };
  context.comment = comment;
  evaluate('pendingComments = [comment]');
  assert.deepEqual(JSON.parse(JSON.stringify(context.buildReviewPayload())), {
    commit_id: data.PR_META.head_sha, event: 'COMMENT', body: 'A summary',
    comments: [{ path: comment.file, line: 5, side: 'LEFT', body: comment.body, start_line: 3, start_side: 'LEFT' }],
  });
});

for (const shell of ['/bin/sh', '/bin/bash', '/bin/zsh']) {
  test(`generated gh command passes exact JSON and arguments in ${shell}`, { skip: !existsSync(shell) }, () => {
    const temp = mkdtempSync(path.join(os.tmpdir(), 'walkthrough-command-'));
    try {
      const comment = { file: unusualPath, endLine: 1, side: 'RIGHT', body: "' \" `false` $(false) \\n \\t\n\u2028 ✓" };
      context.comment = comment;
      evaluate('pendingComments = [comment]');
      const command = context.buildGhCommand();
      const result = spawnSync(shell, ['-c', 'gh() { printf "%s\\0" "$@" > "$REVIEW_ARGS"; cat; };\n' + command], {
        encoding: 'utf8', env: { ...process.env, REVIEW_ARGS: path.join(temp, 'args') },
      });
      assert.equal(result.status, 0, result.stderr);
      assert.deepEqual(JSON.parse(result.stdout), JSON.parse(JSON.stringify(context.buildReviewPayload())));
      assert.deepEqual(readFileSync(path.join(temp, 'args'), 'utf8').split('\0'), [
        'api', 'repos/example/repo/pulls/306/reviews', '--method', 'POST', '--input', '-', '',
      ]);
    } finally {
      rmSync(temp, { recursive: true });
    }
  });
}

const javaDiff = [
  'diff --git a/A.java b/A.java',
  'index 1111111..2222222 100644',
  '--- a/A.java',
  '+++ b/A.java',
  '@@ -1,4 +1,4 @@',
  ' /* opens',
  '-   closes */ int count = 1;',
  '+   closes */ long count = 1;',
  ' @Override public String name() { return "a"; } // done',
  '',
].join('\n');

const javaHighlights = {
  'A.java': [
    [['com', '/* opens']],
    [['com', '   closes */'], [null, ' '], ['kw', 'int'], [null, ' count = '], ['num', '1'], [null, ';']],
    [['com', '   closes */'], [null, ' '], ['kw', 'long'], [null, ' count = '], ['num', '1'], [null, ';']],
    [['ann', '@Override'], [null, ' '], ['kw', 'public'], [null, ' String '], ['fn', 'name'], [null, '() { '],
      ['kw', 'return'], [null, ' '], ['str', '"a"'], [null, '; } '], ['com', '// done']],
  ],
};

test('rows render the renderer\'s syntax tokens in order', () => {
  const rendered = context.renderDiff(javaDiff, ['A.java'], javaHighlights);
  assert.match(rendered, /<span class="tok-com">\/\* opens<\/span>/);
  assert.match(rendered, /<span class="tok-ann">@Override<\/span> <span class="tok-kw">public<\/span> String <span class="tok-fn">name<\/span>/);
  assert.match(rendered, /<span class="tok-str">&quot;a&quot;<\/span>/);
  assert.match(rendered, /<span class="tok-com">\/\/ done<\/span>/);
});

test('files without tokens render as plain text, and unsafe token kinds are ignored', () => {
  assert.doesNotMatch(context.renderDiff(diff, files), /class="tok-/);
  const hostile = { 'A.java': [[['kw" onclick="x', '/* opens']]] };
  const rendered = context.renderDiff(javaDiff, ['A.java'], hostile);
  assert.doesNotMatch(rendered, /onclick/);
  assert.match(rendered, /<span class="line-content">\/\* opens<\/span>/);
});

test('paired changed lines highlight only the words that differ', () => {
  const rendered = context.renderDiff(javaDiff, ['A.java'], javaHighlights);
  assert.match(rendered, /<span class="diff-word"><span class="tok-kw">int<\/span><\/span>/);
  assert.match(rendered, /<span class="diff-word"><span class="tok-kw">long<\/span><\/span>/);
  assert.equal((rendered.match(/class="diff-word"/g) || []).length, 2);
});

test('wholly rewritten lines get no word highlight', () => {
  assert.deepEqual(JSON.parse(JSON.stringify(context.changedRange('abc', 'abd'))),
    { before: [0, 3], after: [0, 3] });
  assert.deepEqual(JSON.parse(JSON.stringify(context.changedRange('use(response);', 'use(postResponse);'))),
    { before: [4, 12], after: [4, 16] });
  const rendered = context.renderDiff(diff, files);
  assert.doesNotMatch(rendered, /class="diff-word"/);
});

test('file headers show change counts and keep mode and rename metadata but hide index and path lines', () => {
  const rendered = context.renderDiff(diff, files);
  assert.match(rendered, /both\.txt<\/span><span class="diff-stat"><span class="diff-stat-add">\+1<\/span> <span class="diff-stat-del">−1<\/span>/);
  assert.match(rendered, /<span class="diff-file-meta">deleted file mode 100644<\/span>/);
  assert.doesNotMatch(rendered, /index 1111111/);
  assert.doesNotMatch(rendered, /\+\+\+ b\/both\.txt/);
  assert.match(rendered, /<span class="line-num-old">1<\/span><span class="line-num">/);
});

test('review drafts are keyed by the review contents so only the same review restores them', () => {
  const key = context.reviewDraftKey(data.STEPS, data.PR_META);
  assert.match(key, /^review-walkthrough-draft:[0-9a-f]{8}$/);
  assert.equal(context.reviewDraftKey(data.STEPS, data.PR_META), key);
  assert.notEqual(context.reviewDraftKey(data.STEPS, { ...data.PR_META, head_sha: 'b'.repeat(40) }), key);
});

test('restored drafts drop comments that no longer fit the review', () => {
  const valid = { file: 'both.txt', startLine: null, endLine: 1, side: 'LEFT', startSide: 'LEFT', body: 'Keep', stepIndex: 0 };
  const restored = context.restorableComments([
    valid,
    { ...valid, file: 'missing.txt' },
    { ...valid, stepIndex: 3 },
    { ...valid, side: 'MIDDLE' },
    { ...valid, endLine: '1' },
    null,
  ], data.STEPS);
  assert.deepEqual(JSON.parse(JSON.stringify(restored)), [valid]);
  assert.deepEqual(JSON.parse(JSON.stringify(context.restorableComments('nope', data.STEPS))), []);
});

test('pull request metadata links to the pull request on GitHub', () => {
  assert.equal(context.pullRequestUrl(data.PR_META), 'https://github.com/example/repo/pull/306');
  assert.equal(context.pullRequestUrl(null), null);
});
