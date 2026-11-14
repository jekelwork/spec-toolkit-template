import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).with_name('check_roadmap_spec_ids.py')


class RoadmapSpecIdsTest(unittest.TestCase):
    def run_check(self, roadmap, folders):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'ROADMAP.md').write_text(roadmap)
            for name, entry in folders.items():
                folder = root / 'specs' / name
                folder.mkdir(parents=True)
                (folder / 'spec.md').write_text(f'**Input**: Parent roadmap: `ROADMAP.md` entry {entry}.')
            return subprocess.run([sys.executable, str(SCRIPT), str(root)], capture_output=True, text=True)

    def test_matching_entry_and_directory_pass(self):
        result = self.run_check('### 012 — Example [status: specced]\n- **Spec dir:** specs/012-example/\n', {'012-example': '012'})
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_wrong_roadmap_directory_prefix_fails(self):
        result = self.run_check('### 012 — Example [status: specced]\n- **Spec dir:** specs/005-example/\n', {'005-example': '012'})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('012', result.stderr)

    def test_wrong_spec_input_fails_even_without_roadmap_pointer(self):
        result = self.run_check('### 005 — Other [status: planned]\n### 012 — Example [status: planned]\n', {'005-example': '012'})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('012', result.stderr)

    def test_missing_directory_fails(self):
        result = self.run_check('### 012 — Example [status: specced]\n- **Spec dir:** specs/012-example/\n', {})
        self.assertNotEqual(result.returncode, 0)

    def test_unknown_roadmap_entry_fails(self):
        result = self.run_check('### 005 — Other [status: planned]\n', {'012-example': '012'})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('012', result.stderr)

    def test_duplicate_prefix_fails(self):
        result = self.run_check('### 012 — Example [status: planned]\n', {'012-example': '012', '012-other': '012'})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('012', result.stderr)
