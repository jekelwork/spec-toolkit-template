#!/usr/bin/env python3
"""Historical file-status checker and shared parser; use roadmap_status.py for live gates."""

import argparse
import json
import re
import string
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
STATUSES = {
    'undecided', 'needs-info', 'planned', 'specced', 'in-progress',
    'implemented', 'verified', 'deferred', 'abandoned',
}
SATISFIED = {'implemented', 'verified'}
CONDITION = 'ordinary completion: status must be implemented or verified'
BASELINE = (
    'Baseline only: review roadmap notes, milestone evidence, and stale or '
    'conflicting data before starting work; milestones are not verified here.'
)
HEADING = re.compile(
    r'^### ([0-9]{3}) [—–-] (.+?)\s+\[status: ([a-z-]+)\]\s*$'
)
FIELD = re.compile(r'^- \*\*([^*]+):\*\*(?:\s+(.*))?$')
ID = re.compile(r'[0-9]{3}\Z')
DEPENDENCY_LIKE = re.compile(
    r'^[ \t]*(?:(?:[-+*]|\d+[.)])[ \t]*)?[*_`~]*[ \t]*'
    r'(?:depends?[ \t_-]+(?:on|upon)|dependenc(?:y|ies)|'
    r'prerequisites?|blocked[ \t_-]+(?:by|on)|requires?|required|needs?)\b'
    r'[*_`~]*[ \t]*(?::|=|\?|—|–|-|[0-9]|\bnone\b)',
    re.IGNORECASE,
)
DEPENDENCY_LABEL = re.compile(
    r'\b(?:depend\w*|pre[\W_]*req(?:uisit\w*|s?)|block(?:ed|ing|ers?)\w*|'
    r'requir\w*|need\w*)\b',
    re.IGNORECASE,
)
KNOWN_FIELDS = {
    'Description', 'Outcome', 'Scope (in)', 'Scope (out)',
    'Depends on', 'Governed by', 'Spec dir', 'Notes',
}
STRUCTURED_FIELD = re.compile(
    r'^[ \t]*(?:(?:[-+*]|\d+[.)])[ \t]+|(?=[*_`~]))'
    r'[*_`~]*[ \t]*(?P<label>[^:\n]+?)'
    r'[ \t]*[*_`~]*[ \t]*:[ \t]*[*_`~]*[ \t]*(?P<value>.*)$'
)
PLAIN_STRUCTURED_FIELD = re.compile(
    r'^[ \t]*(?P<label>[^:\n]+?):[ \t]*(?P<value>.*)$'
)
ROADMAP_REFERENCE = re.compile(r'(?<![A-Za-z0-9])[0-9]{3}(?![A-Za-z0-9])')
REFERENCE_DEFINITION = re.compile(
    r'^ {0,3}\[[^]\n]+\]:[ \t]*(?P<destination><[^>\n]+>|[^\s]+)(?:[ \t].*)?$'
)
RAW_HTML = re.compile(r'<(?:/?[A-Za-z][A-Za-z0-9:-]*(?=[\s/>?]|$)|[!?])')
AUTOLINK = re.compile(
    r"<(?:[A-Za-z][A-Za-z0-9+.-]{1,31}:[^<>\s]*|"
    r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*)>"
)


class InvalidData(ValueError):
    """Roadmap data or command arguments cannot be evaluated safely."""


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise InvalidData(f'{message}; use --help for usage')


def standalone_backtick_span(line):
    """Distinguish a same-line triple-backtick example from a fence opener."""
    return bool(re.fullmatch(r' {0,3}(`{3,})(?!`)(.*?)(?<!`)\1(?!`)[ \t]*', line))


def normalized_label(label):
    label = re.sub(r'\[([^]\n]+)\]\([^)\n]*\)', r'\1', label)
    return re.sub(r'[*_`~]', '', label).strip()


def is_reference_definition(line):
    definition = REFERENCE_DEFINITION.match(line)
    if definition:
        destination = definition.group('destination')
        if '/' in destination or ':' in destination or destination.startswith('#'):
            return True
    return False


def linked_field(line):
    """Scan a field label once, treating Markdown link destinations as opaque."""
    if is_reference_definition(line):
        return None
    prefix = re.match(r'^[ \t]*(?:(?:[-+*]|\d+[.)])[ \t]+|(?=[*_`~\[]))', line)
    if not prefix:
        return None

    def closing(start, opener, closer):
        depth = 1
        index = start + 1
        while index < len(line):
            char = line[index]
            if char == '\\' and index + 1 < len(line):
                index += 2
                continue
            if char == opener:
                depth += 1
            elif char == closer:
                depth -= 1
                if not depth:
                    return index
            index += 1
        return None

    position = prefix.end()
    label = []
    has_link = False
    while position < len(line):
        char = line[position]
        if char == '\\' and position + 1 < len(line):
            label.append(line[position:position + 2])
            position += 2
        elif char == '[':
            end = closing(position, '[', ']')
            if end is None:
                return None
            has_link = True
            label.append(line[position + 1:end])
            position = end + 1
            if position < len(line) and line[position] in '([':
                opener = line[position]
                end = closing(position, opener, ')' if opener == '(' else ']')
                if end is None:
                    return None
                position = end + 1
        elif char == ':':
            if not has_link:
                return None
            return normalized_label(''.join(label)), line[position + 1:]
        else:
            label.append(char)
            position += 1
    return None


def unknown_field_diagnostic(line_number, field, reference):
    return InvalidData(
        f"line {line_number}: unknown field {field!r} contains {reference}; this may be "
        'a roadmap ID. Clarify the field or use Notes for non-dependency information. '
        'If this is a prerequisite, use the canonical Depends on field.'
    )


def visible_section_lines(text):
    """Find the sole ledger section, excluding comments and fenced code."""
    lines = []
    section_count = 0
    in_section = False
    in_comment = False
    fence = None
    previous_visible = ''
    for line_number, raw in enumerate(text.splitlines(), 1):
        if fence:
            closing = re.match(r'^ {0,3}(' + re.escape(fence[0]) + r'{3,})(.*)$', raw)
            if closing and len(closing.group(1)) >= len(fence) and not closing.group(2).strip():
                fence = None
            if in_section:
                lines.append((line_number, '', False))
            previous_visible = ''
            continue

        if not in_comment:
            opening = re.match(r'^ {0,3}(`{3,}|~{3,})(.*)$', raw)
            if opening and not standalone_backtick_span(raw):
                fence, info = opening.groups()
                if fence[0] == '`' and '`' in info:
                    raise InvalidData(f'line {line_number}: malformed fence opener')
                if in_section:
                    lines.append((line_number, '', False))
                previous_visible = ''
                continue

        # Preserve inline code and autolinks in notes but mask them for HTML
        # detection. Comment delimiters inside code spans are literal text.
        visible = ''
        html_check = ''
        code_spans = []
        position = 0
        while position < len(raw):
            if in_comment:
                end = raw.find('-->', position)
                nested = raw.find('<!--', position)
                if nested != -1 and (end == -1 or nested < end):
                    raise InvalidData(f'line {line_number}: nested HTML comment delimiter')
                if end == -1:
                    break
                else:
                    in_comment = False
                    position = end + 3
                    visible += ' '
                    html_check += ' '
            elif raw.startswith('<!--', position):
                position += 4
                visible += ' '
                html_check += ' '
                in_comment = True
            elif raw.startswith('-->', position):
                raise InvalidData(f'line {line_number}: unexpected HTML comment closing delimiter')
            elif (raw[position] == '\\' and position + 1 < len(raw)
                  and raw[position + 1] in string.punctuation):
                escaped = raw[position:position + 2]
                visible += escaped
                html_check += '  '
                position += 2
            elif raw[position] == '<' and (link := AUTOLINK.match(raw, position)):
                visible += link.group()
                html_check += ' ' * len(link.group())
                position = link.end()
            elif raw[position] == '`':
                # A fence after a comment is handled by the fence scanner below.
                candidate = visible + raw[position:]
                if (not visible.strip() and re.match(r'^ {0,3}`{3,}', candidate)
                        and not standalone_backtick_span(candidate)):
                    visible += raw[position:]
                    html_check += raw[position:]
                    break
                run = re.match(r'`+', raw[position:]).group()
                end = next((match for match in re.finditer(r'`+', raw[position + len(run):])
                            if len(match.group()) == len(run)), None)
                if end is None:
                    raise InvalidData(
                        f'line {line_number}: unclosed or multiline code span; '
                        'close it on the same line or use a fenced code block'
                    )
                end_position = position + len(run) + end.end()
                code_spans.append((len(visible), len(visible) + end_position - position))
                visible += raw[position:end_position]
                html_check += ' ' * (end_position - position)
                position = end_position
            else:
                visible += raw[position]
                html_check += raw[position]
                position += 1

        opening = re.match(r'^ {0,3}(`{3,}|~{3,})(.*)$', visible)
        if opening and not standalone_backtick_span(visible):
            fence, info = opening.groups()
            if fence[0] == '`' and '`' in info:
                raise InvalidData(f'line {line_number}: malformed fence opener')
            if in_section:
                lines.append((line_number, '', False))
            previous_visible = ''
            continue

        if RAW_HTML.search(html_check):
            raise InvalidData(
                f'line {line_number}: unsupported raw HTML; remove it or use a fenced code example'
            )

        if standalone_backtick_span(visible) and not in_section:
            raise InvalidData(f'line {line_number}: ambiguous fence or code span outside Notes')

        if re.fullmatch(r' {0,3}(?:=+|-+)[ \t]*', visible) and previous_visible.strip():
            if re.match(r'^planned[ \t]+specs\b', previous_visible.strip(), re.IGNORECASE):
                raise InvalidData(f'line {line_number}: ambiguous Planned Specs section heading')
            if in_section:
                raise InvalidData(f'line {line_number}: ambiguous Setext section boundary')
        previous_visible = visible
        heading = re.match(r'^ {0,3}(#{1,6})[ \t]+(.*?)[ \t]*$', visible)
        if heading:
            marks, title = heading.groups()
            if re.match(r'^planned[ \t]+specs\b', title, re.IGNORECASE):
                if marks != '##' or title != 'Planned Specs':
                    raise InvalidData(f'line {line_number}: malformed Planned Specs section heading')
                section_count += 1
                if section_count > 1:
                    raise InvalidData(f'line {line_number}: duplicate Planned Specs section')
                in_section = True
                continue
            if len(marks) <= 2:
                in_section = False
        elif re.match(r'^ {0,3}#{1,2}(?!#)[ \t]*planned[ \t]+specs\b',
                      visible, re.IGNORECASE):
            raise InvalidData(f'line {line_number}: malformed Planned Specs section heading')
        elif in_section and re.match(r'^ {0,3}#{1,2}(?!#)\S', visible):
            raise InvalidData(f'line {line_number}: malformed section boundary: {visible}')
        if not in_section and HEADING.fullmatch(visible):
            raise InvalidData(f'line {line_number}: entry heading outside Planned Specs')
        if in_section:
            example = False
            if len(code_spans) == 1:
                start, end = code_spans[0]
                example = (bool(re.fullmatch(r'[ \t]*(?:(?:[-+*]|\d+[.)])[ \t]+)?',
                                             visible[:start]))
                           and not visible[end:].strip())
            lines.append((line_number, visible, example))

    if in_comment:
        raise InvalidData('unclosed HTML comment; close the comment and retry')
    if fence:
        raise InvalidData('unclosed fenced code block; close the fence and retry')
    if section_count != 1:
        raise InvalidData('missing Planned Specs section; add exactly one ## Planned Specs heading')
    return lines


def parse_roadmap(text, entries, *, statuses=STATUSES):
    current = None
    active_field = None
    unknown_field = None
    for line_number, line, code_example in visible_section_lines(text):
        if re.match(r'^ {0,3}#{1,6}[ \t]*[0-9]', line):
            if not HEADING.fullmatch(line):
                raise InvalidData(f'line {line_number}: malformed entry heading or status: {line}')
        if re.match(r'^ {0,3}#{1,6}\s', line):
            current = None
            active_field = None
            unknown_field = None
            match = HEADING.fullmatch(line)
            if match:
                number, title, status = match.groups()
                if not title.strip() or '[status:' in title:
                    raise InvalidData(f'line {line_number}: malformed entry heading')
                if status not in statuses:
                    raise InvalidData(f'line {line_number}: invalid status {status!r} for {number}')
                if number in entries:
                    raise InvalidData(f'line {line_number}: duplicate entry ID {number}')
                current = {'id': number, 'title': title.strip(), 'status': status,
                           'notes': '', 'depends': None}
                entries[number] = current
            elif '[status:' in line.lower():
                raise InvalidData(f'line {line_number}: malformed entry heading or status: {line}')
            continue
        if standalone_backtick_span(line) and (current is None or active_field != 'Notes'):
            raise InvalidData(f'line {line_number}: ambiguous fence or code span outside Notes')
        if current is not None and active_field == 'Notes' and code_example:
            example = re.fullmatch(
                r'[ \t]*(?:(?:[-+*]|\d+[.)])[ \t]+)?(`+)(.*?)\1[ \t]*', line
            )
            example_field = PLAIN_STRUCTURED_FIELD.match(example.group(2)) if example else None
            # A quoted field is an example, not an actual field. Keep any
            # preceding unknown field pending for subsequent continuations.
            if (unknown_field is None or (not line[:1].isspace() and (
                    (example and DEPENDENCY_LIKE.match(example.group(2)))
                    or (example_field and DEPENDENCY_LABEL.search(
                        normalized_label(example_field.group('label'))))))):
                current['notes'] += '\n' + line
                continue
        field = FIELD.match(line)
        canonical_peer = field and field.group(1) in KNOWN_FIELDS
        if unknown_field and not canonical_peer:
            reference = ROADMAP_REFERENCE.search(line)
            if reference:
                raise unknown_field_diagnostic(line_number, unknown_field, reference.group())
        if is_reference_definition(line):
            if current is not None and active_field == 'Notes':
                current['notes'] += '\n' + line
            continue
        linked = linked_field(line)
        if linked:
            label, value = linked
            bold = bool(re.match(r'^[ \t]*(?:(?:[-+*]|\d+[.)])[ \t]+)?(?:\*\*|__)', line))
            if (DEPENDENCY_LABEL.search(label) or bold or ROADMAP_REFERENCE.search(label)
                    or ROADMAP_REFERENCE.search(value)
                    or re.match(r'^[ \t]*[*_`~]*[ \t]*[0-9]', value)):
                if DEPENDENCY_LABEL.search(label):
                    raise InvalidData(
                        f'line {line_number}: dependency-like linked field label {label!r}; '
                        'use the canonical Depends on field with three-digit IDs'
                    )
                raise InvalidData(
                    f'line {line_number}: unsupported linked field label {label!r}; '
                    'use a plain-text field label and clarify its meaning'
                )
        structured = STRUCTURED_FIELD.match(line) if not linked else None
        plain = PLAIN_STRUCTURED_FIELD.match(line) if not structured and not linked else None
        # A FIELD match is authoritative even when alternate label parsers do
        # not recognize its spelling; it must never evade the same checks.
        if field:
            candidate_label, candidate_value = field.group(1), field.group(2) or ''
        elif structured or plain:
            candidate = structured or plain
            candidate_label, candidate_value = candidate.group('label', 'value')
        else:
            candidate_label, candidate_value = None, ''
        if (candidate_label is not None and DEPENDENCY_LABEL.search(normalized_label(candidate_label))
                and not (field and field.group(1) == 'Depends on')):
            reference = ROADMAP_REFERENCE.search(candidate_value)
            detail = f' (roadmap ID {reference.group()})' if reference else ''
            raise InvalidData(
                f"line {line_number}: dependency-like field {candidate_label!r}{detail} "
                'is not canonical; use - **Depends on:** none or comma-separated three-digit IDs'
            )
        if not structured:
            if plain:
                plain_value = plain.group('value').strip()
                if (not plain_value or re.match(r'^[*_`~]*[0-9]{3}(?![A-Za-z0-9])', plain_value)
                        or (active_field != 'Notes' and ROADMAP_REFERENCE.search(plain_value))):
                    structured = plain
        if field or structured:
            label = normalized_label(field.group(1) if field else structured.group('label'))
            if not unknown_field or canonical_peer:
                unknown_field = label if label not in KNOWN_FIELDS else None
            value = (field.group(2) or '') if field else structured.group('value')
        elif unknown_field:
            # Nested items and lazy continuations remain ambiguous until a
            # new structured field or heading resolves the context.
            value = line
        else:
            value = ''
        if unknown_field:
            reference = ROADMAP_REFERENCE.search(value)
            if reference:
                raise unknown_field_diagnostic(line_number, unknown_field, reference.group())
        if current is not None and active_field == 'Notes' and code_example:
            current['notes'] += '\n' + line
            continue
        if DEPENDENCY_LIKE.match(line) and not (field and field.group(1) == 'Depends on'):
            raise InvalidData(
                f'line {line_number}: malformed Depends on or prerequisite field; '
                'use - **Depends on:** none or comma-separated three-digit IDs'
            )
        if current is None:
            if field and field.group(1) == 'Depends on':
                raise InvalidData(f'line {line_number}: Depends on field outside a roadmap entry')
            continue
        if field:
            name, value = field.groups()
            value = value or ''
            active_field = name
            if name.lower().strip() == 'depends on' and name != 'Depends on':
                raise InvalidData(f"{current['id']}: malformed Depends on field at line {line_number}")
            if name == 'Depends on':
                if current['depends'] is not None:
                    raise InvalidData(f"{current['id']}: duplicate Depends on field")
                current['depends'] = value
            elif name == 'Notes':
                if current.get('_has_notes'):
                    raise InvalidData(f"{current['id']}: duplicate Notes field")
                current['_has_notes'] = True
                current['notes'] = value
        elif active_field == 'Notes':
            current['notes'] += '\n' + line
        elif active_field == 'Depends on' and line.strip():
            raise InvalidData(f"{current['id']}: ambiguous Depends on continuation at line {line_number}")
        elif line.strip():
            active_field = None

    if not entries:
        raise InvalidData('no roadmap entries found; check the roadmap headings')
    for number, item in entries.items():
        value = item['depends']
        if value is None or not value.strip():
            raise InvalidData(f'{number}: missing or empty Depends on field')
        value = value.strip()
        if value == 'none':
            dependencies = []
        else:
            dependencies = [part.strip() for part in value.split(',')]
            if 'none' in dependencies:
                raise InvalidData(f"{number}: 'none' cannot be mixed with dependencies")
            for dependency in dependencies:
                if not ID.fullmatch(dependency):
                    raise InvalidData(f'{number}: invalid dependency {dependency!r}; use canonical three-digit IDs')
            if len(set(dependencies)) != len(dependencies):
                raise InvalidData(f'{number}: duplicate dependency in Depends on')
        item['depends'] = dependencies
        item['notes'] = item['notes'].rstrip('\n')
        item.pop('_has_notes', None)

    for number, item in entries.items():
        for dependency in item['depends']:
            if dependency not in entries:
                raise InvalidData(f'{number}: dependency {dependency} does not exist in roadmap')

    # Iterative depth-first traversal avoids Python recursion limits on long graphs.
    finished = set()
    for number in entries:
        if number in finished:
            continue
        path = [number]
        active = {number}
        stack = [iter(entries[number]['depends'])]
        while stack:
            dependency = next(stack[-1], None)
            if dependency is None:
                finished.add(path.pop())
                active.remove(number)
                stack.pop()
                if path:
                    number = path[-1]
            elif dependency in active:
                start = path.index(dependency)
                raise InvalidData('dependency cycle: ' + ' -> '.join(path[start:] + [dependency]))
            elif dependency not in finished:
                number = dependency
                path.append(number)
                active.add(number)
                stack.append(iter(entries[number]['depends']))
    return entries


def evaluate(entries, number, *, assess=None):
    if number not in entries:
        raise InvalidData(f'roadmap entry {number} not found; select an existing canonical ID')
    item = entries[number]
    dependencies = []
    for dependency in item['depends']:
        prerequisite = entries[dependency]
        assessment = (
            assess(dependency) if assess else {
                'satisfied': prerequisite['status'] in SATISFIED, 'condition': CONDITION,
            }
        )
        dependencies.append({
            'id': dependency,
            'title': prerequisite['title'],
            'status': prerequisite['status'],
            **assessment,
            'notes': prerequisite['notes'],
        })
    return {
        'result': 'ready' if all(d['satisfied'] for d in dependencies) else 'blocked',
        'entry': {key: item[key] for key in ('id', 'title', 'status', 'notes')},
        'dependencies': dependencies,
        'baseline': BASELINE,
        'diagnostics': [],
    }


def print_human(data):
    if data['result'] == 'error':
        print('Error: ' + '\nError: '.join(data['diagnostics']), file=sys.stderr)
        return
    item = data['entry']
    print(f"{data['result'].upper()}: {item['id']} — {item['title']} [{item['status']}]")
    if item['notes']:
        print(f"Requested entry notes: {item['notes']}")
    if not data['dependencies']:
        print('Dependencies: none')
    for dependency in data['dependencies']:
        state = 'satisfied' if dependency['satisfied'] else 'BLOCKER'
        print(f"{state}: {dependency['id']} — {dependency['title']} "
              f"[{dependency['status']}]; {dependency['condition']}")
        if dependency['notes']:
            print(f"  Prerequisite notes: {dependency['notes']}")
    print(BASELINE)


def main(argv=None):
    print(
        'Warning: file-status dependency checking is historical; '
        'use scripts/roadmap_status.py --id <ID> for live GitHub completion.',
        file=sys.stderr,
    )
    argv = sys.argv[1:] if argv is None else argv
    json_mode = '--json' in argv
    parser = Parser(description=__doc__)
    parser.add_argument('id', help='canonical three-digit roadmap entry ID')
    parser.add_argument('--json', action='store_true', help='output machine-readable JSON')
    parser.add_argument('--roadmap', type=Path, default=ROOT / 'ROADMAP.md',
                        help='alternate roadmap file for synthetic tests')
    args = None
    entries = {}
    try:
        args = parser.parse_args(argv)
        if not ID.fullmatch(args.id):
            raise InvalidData(f'{args.id!r}: expected a canonical three-digit roadmap ID')
        try:
            text = args.roadmap.read_text(encoding='utf-8')
        except (OSError, UnicodeError) as error:
            raise InvalidData(f'cannot read roadmap {args.roadmap}: {error}; check the file and encoding') from error
        data = evaluate(parse_roadmap(text, entries), args.id)
    except InvalidData as error:
        item = entries.get(args.id) if args else None
        requested = ({key: item[key] for key in ('id', 'title', 'status', 'notes')}
                     if item else {'id': args.id, 'title': None, 'status': None, 'notes': None}
                     if args else None)
        data = {'result': 'error', 'entry': requested, 'dependencies': [],
                'baseline': BASELINE, 'diagnostics': [str(error)]}
    if json_mode:
        print(json.dumps(data, ensure_ascii=False))
    else:
        print_human(data)
    return {'ready': 0, 'blocked': 1, 'error': 2}[data['result']]


if __name__ == '__main__':
    sys.exit(main())
