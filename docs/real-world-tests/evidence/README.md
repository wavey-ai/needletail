# Versioned qualification evidence

This directory contains sanitized machine-readable summaries of real provider
tests. One JSON file represents one qualification invocation, including failed
invocations. It records the raw local artifact directory so an operator with
the same workspace can inspect the full API snapshots and Prometheus captures.

These summaries deliberately contain no credential paths, private keys,
tokens, authorization headers, or secret material. A passing result is copied
from the gate's `qualification.json`. When a gate exits before producing that
file, the summary is assembled from the immutable before/during/after captures
and names the exact failed gate.

Evidence index:

- `20260715T223437Z.json`: recovery failed before the controlled-loss phase;
- `20260715T225642Z.json`: complete passing restart, failover, recovery, and 2% loss run;
- `20260715T230001Z.json`: repeat met latency gates but failed the strict one-object expiry bound;
- `20260715T230319Z.json`: second complete passing run after the strict variance failure;
- `20260715T230643Z.json`: third complete passing run;
- `20260715-corrected-series-summary.json`: aggregate ranges across the four corrected-build repeats.
