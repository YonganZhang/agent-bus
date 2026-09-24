#!/usr/bin/env python3
"""Regression tests for the browser-side history overlap merger."""

from pathlib import Path
import subprocess
import unittest


class FrontendHistoryOverlapTest(unittest.TestCase):
    def test_overlap_finds_page_already_contained_inside_hot_window(self) -> None:
        html = (Path(__file__).resolve().parents[2] / "dashboard" / "index.html").read_text(encoding="utf-8")
        start = html.index("    function findOverlapTrimLength(")
        end = html.index("\n    function combineBlocksWithOlder(", start)
        function_source = html[start:end]
        script = f"""
const blockScrollKey = block => String(block.key);
{function_source}
const block = key => ({{ key }});
const cases = [
  {{ older: ['c', 'd', 'e'], head: ['a', 'b', 'c', 'd', 'e'], want: 3 }},
  {{ older: ['x', 'y', 'a', 'b'], head: ['a', 'b', 'c'], want: 2 }},
  // A partial suffix merely repeated in the middle is not a safe boundary.
  {{ older: ['x', 'y', 'c', 'd'], head: ['a', 'b', 'c', 'd', 'e'], want: 0 }},
  {{ older: ['x', 'y'], head: ['a', 'b', 'c'], want: 0 }},
];
for (const item of cases) {{
  const got = findOverlapTrimLength(item.older.map(block), item.head.map(block));
  if (got !== item.want) throw new Error(`overlap ${{got}} !== ${{item.want}}`);
}}
"""
        result = subprocess.run(
            ["node", "-e", script],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)


if __name__ == "__main__":
    unittest.main()
