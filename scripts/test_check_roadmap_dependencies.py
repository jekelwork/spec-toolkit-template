import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).with_name('check_roadmap_dependencies.py')


def entry(number, title='Example', status='planned', depends='none', notes=None):
    text = f'### {number} — {title} [status: {status}]\n\n- **Depends on:** {depends}\n'
    if notes is not None:
        text += f'- **Notes:** {notes}\n'
    return text


class DependencyCliTest(unittest.TestCase):
    def run_check(self, roadmap, number='001', *options, raw=False):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'roadmap.md'
            content = roadmap if raw else '## Planned Specs\n\n### Foundation\n\n' + roadmap
            path.write_text(content, encoding='utf-8')
            return subprocess.run(
                [sys.executable, str(SCRIPT), number, '--roadmap', str(path), *options],
                capture_output=True, text=True,
            )

    def json_check(self, roadmap, number='001', raw=False):
        process = self.run_check(roadmap, number, '--json', raw=raw)
        return process, json.loads(process.stdout)

    def test_no_dependencies_ready_and_requested_notes_complete(self):
        roadmap = entry('001', notes='First line\ncontinued line\n\n- note bullet\n  another paragraph')
        process, data = self.json_check(roadmap)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(data['result'], 'ready')
        self.assertEqual(data['entry']['id'], '001')
        self.assertEqual(data['entry']['title'], 'Example')
        self.assertEqual(data['entry']['status'], 'planned')
        self.assertIn('continued line\n\n- note bullet\n  another paragraph', data['entry']['notes'])
        self.assertEqual(data['dependencies'], [])

    def test_many_mixed_dependencies_and_prerequisite_notes(self):
        statuses = ['implemented', 'verified', 'planned', 'in-progress', 'specced',
                    'undecided', 'needs-info', 'deferred', 'abandoned']
        ids = [f'{i:03}' for i in range(2, 11)]
        roadmap = entry('001', 'Target', depends=', '.join(ids), notes='Qualification')
        for number, status in zip(ids, statuses):
            roadmap += entry(number, f'Prerequisite {number}', status, notes=f'Note {number}')
        process, data = self.json_check(roadmap)
        self.assertEqual(process.returncode, 1, process.stderr)
        self.assertEqual(data['result'], 'blocked')
        self.assertEqual([d['id'] for d in data['dependencies']], ids)
        self.assertEqual([d['satisfied'] for d in data['dependencies']],
                         [True, True] + [False] * 7)
        self.assertEqual(data['dependencies'][2]['notes'], 'Note 004')
        self.assertIn('implemented', data['dependencies'][2]['condition'])
        human = self.run_check(roadmap)
        self.assertEqual(human.returncode, 1)
        for number, status in zip(ids, statuses):
            self.assertIn(number, human.stdout)
            self.assertIn(status, human.stdout)
            self.assertIn(f'Prerequisite {number}', human.stdout)
        self.assertIn('baseline', human.stdout.lower())
        self.assertIn('milestone', human.stdout.lower())
        self.assertIn('stale', human.stdout.lower())
        self.assertIn('Qualification', human.stdout)
        self.assertIn('Note 004', human.stdout)

    def test_all_satisfied(self):
        roadmap = (entry('001', depends='002, 003') +
                   entry('002', status='implemented') + entry('003', status='verified'))
        process, data = self.json_check(roadmap)
        self.assertEqual(process.returncode, 0)
        self.assertEqual(data['result'], 'ready')
        self.assertTrue(all(d['satisfied'] for d in data['dependencies']))

    def assert_invalid(self, roadmap, diagnostic, number='001', raw=False):
        process, data = self.json_check(roadmap, number, raw=raw)
        self.assertEqual(process.returncode, 2, (process.stdout, process.stderr))
        self.assertEqual(data['result'], 'error')
        self.assertIn(diagnostic.lower(), ' '.join(data['diagnostics']).lower())

    def test_invalid_requested_and_missing_ids(self):
        self.assert_invalid(entry('001'), 'canonical', '1')
        self.assert_invalid(entry('001'), '002', '002')

    def test_duplicate_and_malformed_headings_and_statuses(self):
        cases = [
            (entry('001') + entry('001'), 'duplicate'),
            ('### 01 — Wrong [status: planned]\n- **Depends on:** none\n', 'heading'),
            ('### 001 Example [status: planned]\n- **Depends on:** none\n', 'heading'),
            ('### 001 — Example\n- **Depends on:** none\n', 'heading'),
            (entry('001', status='completed'), 'status'),
            (entry('001', status='Implemented'), 'status'),
        ]
        for roadmap, diagnostic in cases:
            with self.subTest(roadmap=roadmap):
                self.assert_invalid(roadmap, diagnostic)

    def test_missing_malformed_duplicate_and_unknown_dependencies(self):
        cases = [
            ('### 001 — Example [status: planned]\n', 'depends on'),
            (entry('001', depends=''), 'depends on'),
            (entry('001', depends='none, 002') + entry('002'), 'none'),
            (entry('001', depends='002, 002') + entry('002'), 'duplicate'),
            (entry('001', depends='2'), 'dependency'),
            (entry('001', depends='002,'), 'dependency'),
            (entry('001', depends='002'), '002'),
            (entry('001') + '- **Depends on:** none\n', 'duplicate'),
            (entry('001') + '- **Depends on**: none\n', 'depends on'),
            (entry('001') + '- **Depends On:** none\n', 'depends on'),
        ]
        for roadmap, diagnostic in cases:
            with self.subTest(roadmap=roadmap):
                self.assert_invalid(roadmap, diagnostic)

    def test_cycles_anywhere_and_long_cycles(self):
        self.assert_invalid(entry('001', depends='001'), 'cycle')
        self.assert_invalid(entry('001') + entry('002', depends='003') +
                            entry('003', depends='002'), 'cycle')
        roadmap = entry('001')
        for i in range(2, 1202):
            next_id = f'{i + 1:03}' if i < 999 else '002'
            if i > 999:
                break
            roadmap += entry(f'{i:03}', depends=next_id)
        self.assert_invalid(roadmap, 'cycle')

    def test_graph_spanning_all_canonical_ids_and_deep_cycle(self):
        roadmap = ''.join(
            entry(f'{i:03}', status='implemented',
                  depends=f'{i + 1:03}' if i < 999 else 'none')
            for i in range(1000)
        )
        process, data = self.json_check(roadmap, '000')
        self.assertEqual(process.returncode, 0, process.stdout)
        self.assertEqual(data['result'], 'ready')
        self.assert_invalid(roadmap.replace('- **Depends on:** none\n',
                                            '- **Depends on:** 000\n'), 'cycle', '000')

    def test_unreadable_file_and_invalid_cli_in_json_mode(self):
        for args in (['001', '--roadmap', '/nonexistent/roadmap.md', '--json'],
                     ['001', '--unknown', '--json']):
            process = subprocess.run([sys.executable, str(SCRIPT), *args],
                                     capture_output=True, text=True)
            self.assertEqual(process.returncode, 2)
            data = json.loads(process.stdout)
            self.assertEqual(data['result'], 'error')
            self.assertTrue(data['diagnostics'])

    def test_error_retains_known_request_and_available_metadata(self):
        process, data = self.json_check(entry('001', 'Target', depends='999'))
        self.assertEqual(process.returncode, 2)
        self.assertEqual(data['entry']['id'], '001')
        self.assertEqual(data['entry']['title'], 'Target')
        self.assertEqual(data['entry']['status'], 'planned')
        process, data = self.json_check(entry('001'), '002')
        self.assertEqual(process.returncode, 2)
        self.assertEqual(data['entry']['id'], '002')
        self.assertIsNone(data['entry']['title'])

    def test_invalid_utf8_and_non_entry_section(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'roadmap.md'
            path.write_bytes(b'\xff')
            process = subprocess.run([sys.executable, str(SCRIPT), '001', '--json',
                                      '--roadmap', str(path)], capture_output=True, text=True)
            self.assertEqual(process.returncode, 2)
            self.assertEqual(json.loads(process.stdout)['result'], 'error')
        roadmap = entry('001') + '\n## Other\n- **Depends on:** ignored\n'
        process, data = self.json_check(roadmap)
        self.assertEqual(process.returncode, 0, process.stdout)
        self.assertEqual(data['result'], 'ready')

    def test_hidden_and_outside_entries_do_not_resolve_dependencies(self):
        target = entry('001', depends='002')
        prerequisite = entry('002', status='implemented')
        for suffix in (
            '<!--\n' + prerequisite + '-->\n',
            '```markdown\n' + prerequisite + '```\n',
            '~~~\n' + prerequisite + '~~~\n',
        ):
            with self.subTest(suffix=suffix):
                self.assert_invalid(target + suffix, '002')
        roadmap = '## Archive\n' + prerequisite + '\n## Planned Specs\n' + target
        self.assert_invalid(roadmap, 'outside Planned Specs', raw=True)
        self.assert_invalid(target + '## Archive\n' + prerequisite, 'outside Planned Specs')

    def test_hidden_dependency_fields_do_not_count(self):
        head = '### 001 — Example [status: planned]\n'
        for hidden in (
            '<!--\n- **Depends on:** none\n-->\n',
            '<!-- - **Depends on:** none -->\n',
            '```\n- **Depends on:** none\n```\n',
            '~~~markdown\n- **Depends on:** none\n~~~\n',
        ):
            with self.subTest(hidden=hidden):
                self.assert_invalid(head + hidden, 'depends on')

    def test_hidden_delimiters_and_section_boundaries_fail_closed(self):
        cases = [
            ('# Roadmap\n' + entry('001'), 'planned specs'),
            ('## Planned Specs\n' + entry('001') + '\n## Planned Specs\n', 'planned specs'),
            ('## Planned Specs (archived)\n' + entry('001'), 'planned specs'),
            ('### Planned Specs\n' + entry('001'), 'planned specs'),
            ('## Planned Specs\n' + entry('001') + '\n<!-- unclosed\n', 'comment'),
            ('## Planned Specs\n' + entry('001') + '\n```\nunclosed\n', 'fence'),
            ('## Planned Specs\n' + entry('001') + '\n~~~\nunclosed\n', 'fence'),
            ('## Planned Specs\n' + entry('001') + '\n## Archive\n<!-- unclosed\n',
             'comment'),
            ('## Planned Specs\n' + entry('001') + '\n## Archive\n```\nunclosed\n',
             'fence'),
            ('## Planned Specs\n' + entry('001') + '\n-->\n', 'comment'),
            ('## Planned Specs\n' + entry('001') + '\n<!-- <!-- nested --> -->\n', 'comment'),
            ('<!-- ## Planned Specs -->\n' + entry('001'), 'planned specs'),
            ('## Planned Specs\n' + entry('001') + '\n##Archive\n' + entry('002'),
             'boundary'),
            ('## Planned Specs\n' + entry('001', depends='002', notes='Notes') +
             '\nArchive\n-------\n' + entry('002', status='implemented'), 'boundary'),
            ('## Planned Specs\n' + entry('001', notes='Notes') +
             '\nPlanned Specs\n-------------\n', 'planned specs'),
        ]
        for roadmap, diagnostic in cases:
            with self.subTest(roadmap=roadmap):
                self.assert_invalid(roadmap, diagnostic, raw=True)

    def test_closed_hidden_blocks_do_not_affect_visible_content(self):
        roadmap = ('## Planned Specs\n### Foundation\n'
                   '<!-- ignored -->\n```md\n### 999 — hidden [status: nope]\n```\n'
                   + entry('001', depends='002') + entry('002', status='implemented'))
        process, data = self.json_check(roadmap, raw=True)
        self.assertEqual(process.returncode, 0, process.stdout)
        self.assertEqual(data['result'], 'ready')
        self.assertEqual([d['id'] for d in data['dependencies']], ['002'])

    def test_fence_info_and_inline_comments_are_handled_as_markdown(self):
        roadmap = ('## Planned Specs\n```html <!-- not a comment\n'
                   '<!-- unclosed comment in code\n```\n'
                   + entry('001', depends='002') +
                   '<!-- heading comment -->\n' + entry('002', status='implemented'))
        process, data = self.json_check(roadmap, raw=True)
        self.assertEqual(process.returncode, 0, process.stdout)
        self.assertEqual(data['result'], 'ready')

    def test_raw_html_cannot_hide_entries_fields_or_sections(self):
        target = entry('001', depends='002', notes='Target notes')
        prerequisite = entry('002', status='implemented', notes='Hidden notes')
        head = '### 001 — Example [status: planned]\n'
        cases = [
            ('## Planned Specs\n' + target + '<pre>\n' + prerequisite + '</pre>\n'),
            ('## Planned Specs\n' + head + '<pre>\n- **Depends on:** none\n</pre>\n'),
            ('<pre>\n## Planned Specs\n' + entry('001') + '</pre>\n'),
            ('## Planned Specs\n' + entry('001') + '<div class="archive">\n'),
            ('## Planned Specs\n' + entry('001') + '</pre>\n'),
            ('## Planned Specs\n' + entry('001') + '<pre\n'),
            ('## Planned Specs\n' + entry('001') + '<!-- hidden --> <PRE>\n'),
            ('## Planned Specs\n' + entry('001') + '- <details open>\n'),
            ('## Planned Specs\n' + entry('001') + '<pre><code>sample</code></pre>\n'),
            ('## Planned Specs\n' + entry('001') + '<!DOCTYPE html>\n'),
            ('## Planned Specs\n' + entry('001') + '<?processing instruction?>\n'),
        ]
        for roadmap in cases:
            with self.subTest(roadmap=roadmap):
                self.assert_invalid(roadmap, 'html', raw=True)

    def test_html_examples_in_comments_and_fences_do_not_trigger_errors(self):
        roadmap = ('## Planned Specs\n<!-- <pre> -->\n```html\n<pre>\n'
                   '### 999 — hidden [status: implemented]\n</pre>\n```\n'
                   + entry('001', depends='002') + entry('002', status='implemented'))
        process, data = self.json_check(roadmap, raw=True)
        self.assertEqual(process.returncode, 0, process.stdout)
        self.assertEqual(data['result'], 'ready')

    def test_autolinks_and_same_line_code_spans_are_preserved(self):
        examples = [
            'Schema: <https://example.com/schema>.',
            'Contact: <team@example.com> or <mailto:team@example.com>.',
            'Placeholders: `specs/<roadmap-id>-<slug>`.',
            'Example: `<pre><!-- literal --></pre>`.',
            'Nested backticks: ``<pre> and `inner` ``.',
            r'Backslashes within code: ``<pre> \` ``.',
            r'Escaped delimiters: \`literal and \<pre>.',
            r'Two slashes followed by a span: \\`<pre>`.',
        ]
        for notes in examples:
            with self.subTest(notes=notes):
                process, data = self.json_check(entry('001', notes=notes))
                self.assertEqual(process.returncode, 0, process.stdout)
                self.assertEqual(data['result'], 'ready')
                self.assertEqual(data['entry']['notes'], notes)

    def test_unclosed_or_multiline_code_span_cannot_hide_entry(self):
        target = entry('001', depends='002', notes='Example `')
        hidden = entry('002', status='implemented', notes='Fake notes')
        for roadmap in (
            target + hidden + '`\n',
            entry('001', notes='unclosed `'),
            entry('001', notes='unclosed `` with a single ` marker'),
            entry('001', notes='unclosed ` with a double `` marker'),
        ):
            with self.subTest(roadmap=roadmap):
                self.assert_invalid(roadmap, 'code span')

    def test_inline_code_or_autolink_does_not_hide_adjacent_raw_html(self):
        for notes in ('`safe` <pre>', '<https://example.com> <pre>',
                      r'\`escaped backtick followed by <pre>'):
            with self.subTest(notes=notes):
                self.assert_invalid(entry('001', notes=notes), 'html')

    def test_prerequisite_aliases_and_unknown_structured_ids_fail_closed(self):
        aliases = [
            '- **Prerequisites:** 002',
            '- **Prerequisite:** 002',
            '* __Blocked by:__ 002',
            '+ **Blocked on:** 002',
            '- **Requires:** 002',
            '- **Required:** 002',
            '- **Requires:** none',
            '- **Gate:** 002',
            '- **Needs:** 002',
            '* Other condition: 002',
            '  1. **Other condition:** 002',
            'Needs: 002',
        ]
        for alias in aliases:
            with self.subTest(alias=alias):
                self.assert_invalid(entry('001', notes='Notes') + alias + '\n' +
                                    entry('002', status='planned'), 'depends on')
        process, data = self.json_check(entry('001') + '- **Needs:** 002\n' + entry('002'))
        self.assertEqual(process.returncode, 2)
        self.assertIn('Needs', data['diagnostics'][0])
        self.assertIn('002', data['diagnostics'][0])
        for field in ('- **Needs:**\n  002\n', '* Needs:\n  002\n',
                      '- **Needs:**\n  - 002\n', '- **Needs:**\n- 002\n',
                      '- **Needs:**\n  `002`\n', 'Gate:\n  002\n'):
            with self.subTest(field=field):
                self.assert_invalid(entry('001', notes='Notes') + field + entry('002'),
                                    'depends on')

    def test_unknown_field_references_in_formatted_sibling_lists_fail_closed(self):
        for item in ('- `002`', '- [002](https://example.com)', '- **002**',
                     '- prerequisite is 002', '- see `002`', '* requires item 002',
                     '- unrelated text\n- `002`',
                     '- unrelated text\n- **002**',
                     '- `Dependencies: 002`\n- `002`'):
            with self.subTest(item=item):
                roadmap = entry('001', notes='Notes') + '- **Gate:**\n' + item + '\n' + entry('002')
                self.assert_invalid(roadmap, 'depends on')

    def test_known_fields_and_notes_examples_after_unknown_field(self):
        roadmap = (entry('001') + '- **Gate:**\n'
                   '- **Description:** Includes 002.\n'
                   '- **Notes:** Examples:\n- `Dependencies: 002`\n' + entry('002'))
        process, data = self.json_check(roadmap)
        self.assertEqual(process.returncode, 0, process.stdout)
        self.assertIn('`Dependencies: 002`', data['entry']['notes'])
        roadmap = entry('001') + '- **Gate:**\n- unrelated text\n- **Notes:** References 002.\n'
        process, data = self.json_check(roadmap)
        self.assertEqual(process.returncode, 0, process.stdout)
        self.assertEqual(data['entry']['notes'], 'References 002.')

    def test_need_alias_rejects_malformed_noncanonical_ids(self):
        for field in ('Needs: 2', '- **Needs:** 2', '- Need: 2',
                      '* `Need`: 2', '- **Needs:** none'):
            with self.subTest(field=field):
                self.assert_invalid(entry('001', notes='Notes') + field + '\n', 'depends on')
        process, data = self.json_check(entry('001') + 'Needs: 2\n')
        self.assertEqual(process.returncode, 2)
        self.assertIn('three-digit', data['diagnostics'][0])
        process, data = self.json_check(entry('001', notes='Notes') + '- **Team size:** 2\n')
        self.assertEqual(process.returncode, 0, process.stdout)
        self.assertEqual(data['result'], 'ready')

    def test_qualified_dependency_labels_are_rejected_regardless_of_value(self):
        labels = (
            '- **Prerequisite(s):** 2',
            'Prerequisite items: 2',
            'Blocked by items: 2',
            '- **Needs approval:** 2',
            'Needs evidence: invalid',
            '* __Requires contract:__ not-an-id',
            'Requires milestones: none',
            '- **Depends on milestones:** 2',
            'Depends on schema: missing',
            '- **Prerequisite(s) & review:** unknown',
            'Prerequisites [optional]: 2',
            '- **Prerequisite** items: 2',
            '- **Blocked by** (items): 2',
            '- **Requires** contract: invalid',
            '- **Depends** on schema: 2',
            'Other *needs* (contract): 2',
            '**Prerequisite** items: 2',
            '*Blocked by* (items): 2',
            '`Needs evidence`: 2',
        )
        for line in labels:
            with self.subTest(line=line):
                self.assert_invalid(entry('001', notes='Notes') + line + '\n', 'depends on')
        roadmap = (entry('001', notes='Examples:\n`Prerequisite items: 2`\n'
                        'Dependency words can appear in ordinary prose.') +
                   '- **Team size:** 2\n- **Description:** Item 002 is mentioned.\n')
        process, data = self.json_check(roadmap)
        self.assertEqual(process.returncode, 0, process.stdout)
        self.assertEqual(data['result'], 'ready')
        self.assertIn('`Prerequisite items: 2`', data['entry']['notes'])

    def test_all_canonical_shape_field_labels_receive_dependency_and_id_checks(self):
        dependency_labels = ('(API) Dependencies', '2 Prerequisites',
                             r'\(API\) Depends on', 'Å Dependencies', '$ Prerequisites')
        for label in dependency_labels:
            for value in ('2', '002', 'none'):
                with self.subTest(label=label, value=value):
                    self.assert_invalid(entry('001') + f'- **{label}:** {value}\n' +
                                        entry('002'), 'depends on')
        unknown_labels = ('(Gate)', '2026 gate', r'\(Gate\)', 'Å gate', '$ Gate')
        for label in unknown_labels:
            with self.subTest(label=label):
                process, data = self.json_check(entry('001') + f'- **{label}:** 002\n' +
                                                entry('002'))
                self.assertEqual(process.returncode, 2, process.stdout)
                self.assertIn('may be a roadmap id', data['diagnostics'][0].lower())
                self.assertIn('002', data['diagnostics'][0])

    def test_nonletter_field_labels_keep_pending_nested_continuations(self):
        for label in ('(Gate)', '2026 gate', r'\(Gate\)', 'Å gate', '$ Gate'):
            with self.subTest(label=label):
                roadmap = (entry('001') + f'- **{label}:**\n'
                           '  - **Description:** context\n  - `002`\n' + entry('002'))
                self.assert_invalid(roadmap, 'depends on')
        roadmap = (entry('001') + '- **(Gate):**\n  - **Description:** context\n'
                   '- **Description:** Includes 002.\n'
                   '- **Notes:** Example:\n`(Gate): 002`\n'
                   'Ordinary prose about 002.\n' + entry('002'))
        process, data = self.json_check(roadmap)
        self.assertEqual(process.returncode, 0, process.stdout)
        self.assertIn('`(Gate): 002`', data['entry']['notes'])
        self.assertIn('Ordinary prose about 002.', data['entry']['notes'])

    def test_nonbold_and_nested_nonletter_field_labels_fail_closed(self):
        dependency_labels = ('(API) Dependencies', '2 Prerequisites',
                             r'\(API\) Depends on', 'Å Dependencies', '$ Prerequisites')
        unknown_labels = ('(Gate)', '2026 gate', r'\(Gate\)', 'Å gate', '$ Gate')
        forms = ('- {}: {}\n', '{}: {}\n', '  - {}: {}\n', '  - **{}:** {}\n')
        for form in forms:
            for label in dependency_labels:
                with self.subTest(form=form, label=label):
                    self.assert_invalid(entry('001', notes='Notes') + form.format(label, '2') +
                                        entry('002'), 'depends on')
            for label in unknown_labels:
                with self.subTest(form=form, label=label):
                    process, data = self.json_check(entry('001', notes='Notes') +
                                                    form.format(label, '002') + entry('002'))
                    self.assertEqual(process.returncode, 2, process.stdout)
                    self.assertIn('may be a roadmap id', data['diagnostics'][0].lower())
                    self.assert_invalid(entry('001', notes='Notes') + form.format(label, '') +
                                        '  - **Description:** context\n  - 002\n' +
                                        entry('002'), 'depends on')
        roadmap = (entry('001', notes='A normal note about 002.\n'
                         '[Guide][r]: background information.\n`(Gate): 002`\n') +
                   '- **Team size:** 2\n- **(Gate):** 2\n'
                   '- **Description:** Includes 002.\n')
        process, data = self.json_check(roadmap)
        self.assertEqual(process.returncode, 0, process.stdout)
        self.assertEqual(data['result'], 'ready')

    def test_linked_field_labels_fail_closed_but_note_links_remain_prose(self):
        labels = (
            '- **[Prerequisites](https://example.com):** 002',
            '- **[Prerequisites](https://example.com):** 2',
            '- **[Gate](https://example.com):** 002',
            '* [Gate](https://example.com): 002',
            '- **[Gate](https://example.com):** 2',
            '- **[Pre-reqs](https://example.com):** invalid',
            '- **Board [Prerequisites](https://example.com):** 2',
            '- [Gate 002](https://example.com): pending',
        )
        for label in labels:
            with self.subTest(label=label):
                self.assert_invalid(entry('001', notes='Notes') + label + '\n' + entry('002'),
                                    'field')
        notes = ('Read [Prerequisites](https://example.com) before proceeding.\n'
                 '- [Guide](https://example.com): background information.\n'
                 '<https://example.com/guide>')
        process, data = self.json_check(entry('001', notes=notes))
        self.assertEqual(process.returncode, 0, process.stdout)
        self.assertEqual(data['entry']['notes'], notes)

    def test_reference_style_linked_labels_and_ordinary_links(self):
        labels = (
            '- **[Prerequisites][r]:** 002',
            '- **[Prerequisites][]:** 2',
            '- **[Prerequisites]:** invalid',
            '- **[Gate][r]:** 002',
            '* [Gate][]: 002',
            '- [Gate]: 002',
        )
        for label in labels:
            with self.subTest(label=label):
                self.assert_invalid(entry('001', notes='Notes') + label + '\n' + entry('002'),
                                    'field')
        notes = ('Reference [Guide][r] and [Guide][] and [Guide].\n'
                 '- [Guide][r]: background information.\n'
                 '[r]: https://example.com/specs/002/guide\n'
                 '[Guide]: ./specs/002/guide\n'
                 '<https://example.com/specs/002/guide>')
        process, data = self.json_check(entry('001', notes=notes))
        self.assertEqual(process.returncode, 0, process.stdout)
        self.assertEqual(data['entry']['notes'], notes)

    def test_many_adjacent_markdown_links_complete_within_timeout(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'roadmap.md'
            notes = 'See links:\n- ' + '[link](./doc)' * 100
            path.write_text('## Planned Specs\n' + entry('001', notes=notes), encoding='utf-8')
            process = subprocess.run(
                [sys.executable, str(SCRIPT), '001', '--json', '--roadmap', str(path)],
                capture_output=True, text=True, timeout=3,
            )
        self.assertEqual(process.returncode, 0, process.stdout)
        self.assertEqual(json.loads(process.stdout)['entry']['notes'], notes)

    def test_non_dependency_field_diagnostics_do_not_prescribe_dependencies(self):
        roadmap = entry('001') + '- **[Team size](https://example.com):** 2\n'
        process, data = self.json_check(roadmap)
        self.assertEqual(process.returncode, 2)
        diagnostic = data['diagnostics'][0].lower()
        self.assertIn('plain', diagnostic)
        self.assertNotIn('depends on', diagnostic)
        process, data = self.json_check(entry('001') + '- **Team count:** 100\n')
        self.assertEqual(process.returncode, 2)
        diagnostic = data['diagnostics'][0].lower()
        self.assertIn('may be', diagnostic)
        self.assertIn('100', diagnostic)
        self.assertIn('notes', diagnostic)
        self.assertRegex(diagnostic, r'if .*prerequisite.*depends on')

    def test_pending_unknown_survives_nested_structured_fields(self):
        for nested in ('  - **Description:** depends on\n  - 002\n',
                       '  - **Description:** depends on\n  - `002`\n',
                       '  - **Outcome:** unrelated\n  - [002](https://example.com)\n',
                       '  - **Other:** text\n  - **002**\n',
                       '  - Other: text\n  - unrelated\n  - 002\n',
                       '  - `Dependencies: 002`\n'):
            with self.subTest(nested=nested):
                roadmap = entry('001') + '- **Gate:**\n' + nested + entry('002')
                self.assert_invalid(roadmap, 'depends on')
        roadmap = entry('001', notes='Notes') + '* Gate:\n  - `Dependencies: 002`\n' + entry('002')
        self.assert_invalid(roadmap, 'depends on')
        roadmap = (entry('001') + '- **Gate:**\n  - **Description:** ordinary\n'
                   '- **Notes:** References 002.\n' + entry('002'))
        process, data = self.json_check(roadmap)
        self.assertEqual(process.returncode, 0, process.stdout)
        self.assertEqual(data['entry']['notes'], 'References 002.')

    def test_abbreviated_prerequisite_labels_fail_closed(self):
        labels = (
            '- **Prereqs:** 2',
            '- **Prereq:** invalid',
            '- **Pre-reqs:** 2',
            'Pre req(s) (schema): none',
            '- **PRE_REQ items:** missing',
            'Other pre-reqs: 2',
        )
        for label in labels:
            with self.subTest(label=label):
                self.assert_invalid(entry('001', notes='Notes') + label + '\n', 'depends on')
        process, data = self.json_check(entry('001', notes='Notes') + '- **Team size:** 2\n')
        self.assertEqual(process.returncode, 0, process.stdout)
        self.assertEqual(data['result'], 'ready')

    def test_known_fields_and_ordinary_notes_are_preserved(self):
        roadmap = (
            '### 001 — Example [status: planned]\n'
            '- **Description:** Includes 002.\n  Also references 003.\n'
            '- **Outcome:** Validates 002.\n'
            '- **Scope (in):** 002.\n'
            '- **Scope (out):** 003.\n'
            '- **Governed by:** C-002.\n'
            '- **Spec dir:** specs/001-example/\n'
            '- **Depends on:** none\n'
            '- **Notes:** Review blocker 002 manually.\n'
            'Example: the 002 schema may help.\n'
            '- This note mentions prerequisite 002 as context.\n'
        )
        process, data = self.json_check(roadmap)
        self.assertEqual(process.returncode, 0, process.stdout)
        self.assertIn('Example: the 002 schema may help.', data['entry']['notes'])
        self.assertIn('prerequisite 002 as context', data['entry']['notes'])

    def test_standalone_code_examples_in_notes_are_not_fields(self):
        for code in ('`Dependencies: 002`', '- `Dependencies: 002`',
                     '`Prerequisites: 002`', '```code```', ' ```code``` '):
            with self.subTest(code=code):
                process, data = self.json_check(entry('001', notes='Examples:\n' + code))
                self.assertEqual(process.returncode, 0, process.stdout)
                self.assertIn(code, data['entry']['notes'])
        notes = 'Examples:\n- Explanation: ordinary text\n- `Dependencies: 002`'
        process, data = self.json_check(entry('001', notes=notes))
        self.assertEqual(process.returncode, 0, process.stdout)
        self.assertEqual(data['entry']['notes'], notes)

    def test_quoted_label_with_external_value_still_fails(self):
        for field in ('- `Depends on`: 002', '- `Prerequisites`: 002',
                      '- `Dependencies`: 002', '* `Blocked by`: 002'):
            with self.subTest(field=field):
                self.assert_invalid(entry('001', notes='Notes') + field + '\n' +
                                    entry('002'), 'depends on')

    def test_triple_backtick_example_requires_notes_context(self):
        roadmap = entry('001', depends='002') + '```code```\n' + entry('002', status='implemented')
        self.assert_invalid(roadmap, 'fence')

    def test_conflicting_dependency_aliases_fail_closed(self):
        aliases = [
            '- **Dependencies:** 002',
            '- **Dependency:** 002',
            '- **Depend on:** 002',
            '- **Depends upon:** 002',
            '* __Dependencies:__ 002',
            '+ Dependency: 002',
            '  1. **Depend on**: 002',
            '- **DEPENDENCIES**: 002',
            '- Dependencies 002',
            'Dependencies: 002',
            '- **Dependencies :** 002, 003',
            '* **depends-upon:** 002',
            '- **Dependency:** none',
        ]
        for alias in aliases:
            with self.subTest(alias=alias):
                roadmap = entry('001', notes='Normal notes') + alias + '\n' + entry('002')
                self.assert_invalid(roadmap, 'depends on')

    def test_dependency_words_in_notes_remain_prose(self):
        notes = ('Dependencies are reviewed manually.\n'
                 '- Dependency risk is recorded here.\n'
                 'The team depends upon other evidence.\n'
                 'Depends on the quality of the review, not a lifecycle state.')
        process, data = self.json_check(entry('001', notes=notes))
        self.assertEqual(process.returncode, 0, process.stdout)
        self.assertEqual(data['entry']['notes'], notes)


    def test_indented_markdown_entry_headings_cannot_hide_duplicates(self):
        for indent in (' ', '  ', '   '):
            with self.subTest(indent=indent):
                self.assert_invalid(entry('001') + indent + entry('001'), 'heading')

    def test_dependency_like_fields_and_ambiguous_continuations_are_rejected(self):
        cases = [
            '* **Depends on:** 002',
            '+ **Depends on:** 002',
            '  - **Depends on:** 002',
            '1. **Depends on:** 002',
            '- __Depends on:__ 002',
            '- Depends on: 002',
            '- **Depends on**: 002',
            '- **Depends on :** 002',
            '- **depends on:** 002',
            '- **Depends-on:** 002',
            '- **Depends_on:** 002',
            '- **Depends on?** 002',
            '- **Depends on:** 002',
            '- 003',
            '* 003',
            '1. 003',
            '  003',
            '003',
            ', 003',
        ]
        for line in cases:
            with self.subTest(line=line):
                self.assert_invalid(entry('001') + line + '\n' + entry('002') + entry('003'),
                                    'depends on')
        for line in ('- 003', '* 003', '1. 003', '  003', '003', ', 003'):
            with self.subTest(line=line):
                self.assert_invalid(entry('001', depends='002') + '\n' + line + '\n' +
                                    entry('002', status='implemented') + entry('003'),
                                    'depends on')
        self.assert_invalid('* **Depends on:** 002\n' + entry('001') + entry('002'),
                            'depends on')
        self.assert_invalid(entry('001', notes='Background') +
                            '* **Depends on:** 002\n' + entry('002'), 'depends on')


if __name__ == '__main__':
    unittest.main()
