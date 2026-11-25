#!/usr/bin/env python3
"""The lab tools' environment (local proof 2's tooling list): every
NEEDLETAIL_ variable a script reads is in docs/lab-environment.md, and the
audio media are found from a worktree under rm/ or refused by name.
python3 -m unittest scripts/test_lab_environment.py"""

from __future__ import annotations

import pathlib
import shutil
import re
import sys
import tempfile
import unittest

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
import edge_lab  # noqa: E402

DOC = ROOT / "docs" / "lab-environment.md"


class Listed(unittest.TestCase):
    def test_every_variable_the_scripts_read_is_in_the_document(self):
        read = set()
        for path in list(HERE.glob("*.py")) + list((HERE / "compare").glob("*.py")):
            if path.name.startswith("test_"):
                continue
            read |= set(re.findall(r"NEEDLETAIL_[A-Z0-9_]+[A-Z0-9]", path.read_text()))
        listed = set(re.findall(r"NEEDLETAIL_[A-Z0-9_]+[A-Z0-9]", DOC.read_text()))
        self.assertEqual(sorted(read - listed), [])


class Media(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def tree(self):
        """~/wavey.ai with the main checkout `needletail` (its media built)
        and a worktree rm/needletail-x of it."""
        main = self.tmp / "wavey.ai" / "needletail"
        media = main.joinpath(*edge_lab.MEDIA_DIR, "album")
        media.mkdir(parents=True)
        (media / "track.s24le").write_bytes(b"\0" * 6)
        (main / ".git" / "worktrees" / "needletail-x").mkdir(parents=True)
        worktree = self.tmp / "wavey.ai" / "rm" / "needletail-x"
        worktree.mkdir(parents=True)
        (worktree / ".git").write_text(f"gitdir: {main}/.git/worktrees/needletail-x\n")
        return main, worktree

    def test_a_worktree_under_rm_finds_the_main_checkout_s_media(self):
        main, worktree = self.tree()
        self.assertEqual(edge_lab.main_checkout(worktree), main)
        self.assertIsNone(edge_lab.main_checkout(main))
        found = edge_lab.media_path("NEEDLETAIL_X", "album", "track.s24le", environ={},
                                    root=worktree)
        self.assertEqual(found, main.joinpath(*edge_lab.MEDIA_DIR, "album", "track.s24le"))
        # The variable wins.
        self.assertEqual(edge_lab.media_path("NEEDLETAIL_X", "album", environ={
            "NEEDLETAIL_X": "/elsewhere"}, root=worktree), pathlib.Path("/elsewhere"))

    def test_media_that_is_nowhere_is_refused_by_its_variable(self):
        _main, worktree = self.tree()
        missing = edge_lab.media_path("NEEDLETAIL_AUDIO_PCM", "none.s24le", environ={},
                                      root=worktree)
        with self.assertRaises(edge_lab.LabError) as caught:
            edge_lab.require_media(missing, "NEEDLETAIL_AUDIO_PCM")
        self.assertIn("export NEEDLETAIL_AUDIO_PCM", str(caught.exception))
        self.assertIn("docs/lab-environment.md", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
