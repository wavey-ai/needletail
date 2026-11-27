#!/usr/bin/env python3
"""Measure the steal time of every node of the lab under a 20-second
processor burn, and name the nodes whose host takes more than a few percent.

The labs use dedicated-CPU plans only, which should show no steal. The burn
checks that: an instance only shows steal when it wants the processor, so an
idle node looks clean, and the burn makes the node ask for one full vCPU.
`linode-small-mesh-lab.py replace NAME...` recreates a node on another host.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import small_mesh_common as common  # noqa: E402

BURN = r"""
a=$(awk '/^cpu /{print $2+$3+$4+$5+$6+$7+$8+$9, $9}' /proc/stat)
timeout 20 sh -c 'while :; do :; done'
b=$(awk '/^cpu /{print $2+$3+$4+$5+$6+$7+$8+$9, $9}' /proc/stat)
echo "$a $b" | awk '{printf "%.1f", 100*($4-$2)/($3-$1)}'
"""


def main() -> None:
    limit = float(sys.argv[1]) if len(sys.argv) > 1 else 3.0
    state = common.load_state()
    nodes = list(state["nodes"].values())
    results = common.parallel(nodes, lambda node: float(common.ssh_text(node, BURN, timeout=90) or 0))
    noisy = []
    for name in sorted(results):
        flag = "  <-- noisy host" if results[name] > limit else ""
        print(f"{name:<18} steal {results[name]:5.1f}%{flag}")
        if results[name] > limit:
            noisy.append(name)
    print("noisy:", " ".join(noisy) or "none")


if __name__ == "__main__":
    main()
