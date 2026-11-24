"""change-window.py: the probe-validity checks (lead, 24 Nov: a stuck listener passed)."""
import importlib.util
import json
import pathlib
import tempfile
import unittest

HERE = pathlib.Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("change_window", HERE / "change-window.py")
cw = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cw)

T0 = 1_800_000_000_000_000   # unix us


def rows(seconds=400, every=1, latency_us=20_000):
    """A 1 ms audio probe: (number, complete_us, latency_us, source_us), every `every`-th unit."""
    out = []
    for n in range(0, seconds * 1000, every):
        source = T0 + n * 1000
        out.append((n, source + latency_us, latency_us, source))
    return out


class ChangeWindowValidity(unittest.TestCase):
    def window(self):
        return T0 + 200_000_000, T0 + 207_000_000

    def test_clean_probe_passes(self):
        a, b = self.window()
        r = cw.score(rows(), a, b, 5_000_000, frame_ms=1.0, min_null=120)
        self.assertTrue(r["pass"], r)
        self.assertAlmostEqual(r["change"]["coverage"], 1.0, places=2)

    def test_sparse_listener_fails_on_coverage(self):
        # Consecutive numbers far apart in time cannot show as missing; coverage catches them.
        sparse = [(i, c, l, s) for i, (n, c, l, s) in enumerate(rows(every=50))]
        a, b = self.window()
        r = cw.score(sparse, a, b, 5_000_000, frame_ms=1.0, min_null=120)
        self.assertFalse(r["pass"])
        self.assertIn("coverage", r["note"])

    def test_own_report_of_a_stuck_listener_fails_every_window(self):
        a, b = self.window()
        report = {"frame_ms": 1.0, "received_fraction": 0.001,
                  "client_missing_parts": None, "client_expected_parts": None}
        r = cw.score(rows(), a, b, 5_000_000, report=report, min_null=120)
        self.assertFalse(r["pass"])
        self.assertTrue(r["probe_invalid"])

    def test_own_report_read_beside_the_csv(self):
        with tempfile.TemporaryDirectory() as d:
            d = pathlib.Path(d)
            (d / "nt-uc.probe.json").write_text(json.dumps(
                {"audio_frame_ms": 1, "duration_seconds": 300, "audio_packets": 299_000}))
            (d / "nt-uc.uc.json").write_text(json.dumps({"expected_parts": 300_000, "missing_parts": 250_000}))
            report = cw.own_report(d / "nt-uc.probe.csv")
        self.assertEqual(report["frame_ms"], 1.0)
        self.assertAlmostEqual(report["received_fraction"], 0.9967, places=3)
        self.assertEqual(report["client_missing_parts"], 250_000)
        a, b = self.window()
        r = cw.score(rows(), a, b, 5_000_000, report=report, min_null=120)
        self.assertFalse(r["pass"])

    def test_window_without_units_fails(self):
        early = [r for r in rows() if r[3] < T0 + 100_000_000]
        a, b = self.window()
        r = cw.score(early, a, b, 5_000_000, frame_ms=1.0)
        self.assertFalse(r["pass"])


if __name__ == "__main__":
    unittest.main()
