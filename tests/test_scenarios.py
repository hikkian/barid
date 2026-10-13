"""A small subset of the panel scenario sweep (tests/scenario_sweep.py) as a test: odd boards x window widths x themes, audited for a page that scrolls
sideways, cards outside the window or on top of each other, text cut off in buttons. The whole sweep is run by hand before a release."""
import os
import shutil
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


@unittest.skipUnless(shutil.which("firefox") and os.environ.get("BARID_SKIP_UI") != "1", "needs Firefox; BARID_SKIP_UI=1 skips")
class PanelScenarios(unittest.TestCase):
    def test_the_quick_sweep_finds_nothing_wrong(self):
        import scenario_sweep
        pages, problems = scenario_sweep.sweep(quick=True, log=lambda *_: None)
        self.assertGreaterEqual(pages, 15)
        self.assertEqual(problems, [], scenario_sweep.report(pages, problems))


if __name__ == "__main__":
    unittest.main()
