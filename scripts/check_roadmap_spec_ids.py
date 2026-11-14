#!/usr/bin/env python3
"""Check that spec directory numbers identify their roadmap entries."""
import re
import sys
from pathlib import Path


def check(root):
    errors = []
    roadmap = (root / 'ROADMAP.md').read_text()
    entry = None
    entries = set()
    prefixes = set()
    for line in roadmap.splitlines():
        heading = re.match(r'^### (\d{3})\s+[—–-]', line)
        if heading:
            entry = heading.group(1)
            entries.add(entry)
        directory = re.match(r'^- \*\*Spec dir:\*\*\s+`?(specs/([^/`\s]+))/?`?', line)
        if directory:
            path, name = directory.groups()
            if entry is None or not name.startswith(f'{entry}-'):
                errors.append(f'{path}: directory prefix does not match roadmap entry {entry}')
            if not (root / path / 'spec.md').is_file():
                errors.append(f'{path}: spec.md does not exist')

    for spec in sorted((root / 'specs').glob('*/spec.md')):
        name = spec.parent.name
        prefix = re.match(r'^(\d{3})-', name)
        if prefix:
            number = prefix.group(1)
            if number not in entries:
                errors.append(f'{name}: roadmap entry {number} does not exist')
            if number in prefixes:
                errors.append(f'{name}: duplicate spec directory prefix {number}')
            prefixes.add(number)
        input_line = next((line for line in spec.read_text().splitlines() if line.startswith('**Input**:')), '')
        reference = re.search(r'ROADMAP\.md`?.*?entry\s+\*{0,2}(\d{3})\b', input_line, re.IGNORECASE)
        if not prefix or not reference:
            errors.append(f'{spec.relative_to(root)}: missing numbered folder or roadmap entry in Input')
        elif prefix.group(1) != reference.group(1):
            errors.append(f'{spec.relative_to(root)}: directory prefix does not match roadmap entry {reference.group(1)}')
    return errors


def main():
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent
    errors = check(root)
    if errors:
        print('\n'.join(errors), file=sys.stderr)
        return 1
    print('Roadmap and spec directory IDs match.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
