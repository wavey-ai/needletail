"""Render every published transport case as one matrix cell."""

from __future__ import annotations

import csv
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle


DIRECTORY = Path(__file__).resolve().parent
INK = "#182733"
SUBTLE = "#435564"
MUTED = "#7f8c95"
FAILURE = "#533129"
MISSING = "#f2f5f7"
MISSING_EDGE = "#cbd5dc"
SCALE = (
    (0, "0%", "#dceddf"),
    (1, ">0–1%", "#f6ead2"),
    (5, ">1–5%", "#f2d5ad"),
    (20, ">5–20%", "#e9b989"),
    (50, ">20–50%", "#d58b71"),
    (100, ">50–100%", "#88453d"),
)
LABELS = {
    "udp": "UDP reference",
    "librist": "RIST · librist",
    "rist-rs": "rist-rs · 904e3e8",
    "rist-rs-tuned": "rist-rs · tuned",
    "rist-rs-614": "rist-rs · 614c10a",
    "srt": "SRT · libsrt",
    "srt-reorder": "SRT · lossmaxttl 64",
    "srt-fec": "SRT · row and column FEC",
    "srt-fec-row": "SRT · row-only FEC",
    "raptorq": "RaptorQ · adaptive FEC",
    "raptorq-fixed": "RaptorQ · fixed repair",
    "raptorq-main": "RaptorQ · fixed 3% repair",
    "raptorq-arq": "RaptorQ · FEC + ARQ",
    "raptorq-client": "RaptorQ · client geometry",
    "raptorq-contrib": "RaptorQ · contributor geometry",
}
RUNGS = (
    ("2160p5", "5.6"),
    ("2160p25", "24.9"),
    ("2160p80", "80.9"),
    ("2160p160", "165.1"),
    ("2160p240", "247.9"),
    ("2160p320", "333.5"),
)
RTTS = ("1", "30", "100", "250")
LOSS_PROFILES = (
    ("Clean", {"loss": "0", "outage": ""}),
    ("1%", {"loss": "1%", "outage": ""}),
    ("5%", {"loss": "5%", "outage": ""}),
    ("Bursts", {"loss": "ge", "outage": ""}),
    ("20 ms\noutages", {"loss": "0", "outage": "out20/2000ms"}),
)

plt.rcParams.update({
    "font.family": "DejaVu Sans",
    "font.size": 10,
    "text.color": INK,
    "xtick.color": SUBTLE,
    "ytick.color": SUBTLE,
    "hatch.linewidth": 0.5,
})


class CaseAtlas:
    def __init__(self) -> None:
        with (DIRECTORY / "case-results.csv").open(newline="") as stream:
            records = list(csv.DictReader(stream))
        self.by_set: dict[str, list[dict[str, str]]] = {}
        self.rendered: set[str] = set()
        for case in records:
            case["display_variant"] = case["variant"]
            if case["id"].endswith("-lossmaxttl64"):
                case["display_variant"] = "srt-reorder"
            if case["set"] == "C" and case["variant"] == "raptorq":
                case["display_variant"] = "raptorq-fixed"
            self.by_set.setdefault(case["set"], []).append(case)
        self.ids = {case["id"] for case in records}
        if len(self.ids) != len(records):
            raise ValueError("The case CSV contains duplicate IDs")

    def match(self, set_name: str, filters: dict[str, str]) -> dict[str, str] | None:
        matches = [
            case for case in self.by_set[set_name]
            if all(case[key] == value for key, value in filters.items())
        ]
        if not matches:
            return None
        if len(matches) != 1:
            raise ValueError(f"Ambiguous case selection in set {set_name}: {filters}")
        case, = matches
        if case["id"] in self.rendered:
            raise ValueError(f"Case rendered twice: {case['id']}")
        self.rendered.add(case["id"])
        return case

    def check_complete(self) -> None:
        if self.rendered != self.ids:
            raise ValueError(f"Cases missing from atlas: {sorted(self.ids - self.rendered)}")


def draw_cell(ax, x, y, case) -> None:
    if case is None:
        ax.add_patch(Rectangle(
            (x + 0.025, y + 0.055), 0.95, 0.89,
            facecolor=MISSING, edgecolor=MISSING_EDGE, hatch="///", linewidth=0.4,
        ))
        ax.text(x + 0.5, y + 0.5, "–", color=MUTED, ha="center", va="center")
        return

    value = 100 * int(case["lost_or_late_max"]) / int(case["frames_expected"])
    _, _, color = next(scale for scale in SCALE if value <= scale[0])
    failed = case["status"] == "transport_failed"
    ax.add_patch(Rectangle(
        (x + 0.025, y + 0.055), 0.95, 0.89,
        facecolor=color, edgecolor=FAILURE if failed else "none",
        linestyle="--" if failed else "-", linewidth=1.4 if failed else 0,
    ))
    label = f"{value:.1f}".removesuffix(".0")
    if failed:
        label += "†"
    ax.text(
        x + 0.5, y + 0.5, label, ha="center", va="center",
        color="white" if value > 50 else INK, fontsize=9.5,
        fontweight="semibold" if failed else "normal",
    )


def matrix(atlas, ax, set_name, title, rows, columns, filters, show_labels=True) -> None:
    ax.set_title(title, loc="left", pad=12, fontsize=12, fontweight="semibold")
    ax.set_xlim(0, len(columns))
    ax.set_ylim(len(rows), 0)
    ax.set_xticks([index + 0.5 for index in range(len(columns))], [label for label, _ in columns])
    ax.set_yticks([index + 0.5 for index in range(len(rows))], [LABELS[row] for row in rows])
    ax.xaxis.tick_top()
    ax.tick_params(axis="x", length=0, pad=5)
    ax.tick_params(axis="y", length=0, pad=7, labelleft=show_labels)
    for spine in ax.spines.values():
        spine.set_visible(False)
    for row_index, row in enumerate(rows):
        for column_index, (_, selection) in enumerate(columns):
            case = atlas.match(set_name, {**filters, **selection, "display_variant": row})
            draw_cell(ax, column_index, row_index, case)


def page(width, height, title, subtitle, note=""):
    fig = plt.figure(figsize=(width, height), facecolor="white")
    fig.text(0.055, 1 - 0.29 / height, title, fontsize=21, fontweight="bold", va="top")
    fig.text(0.055, 1 - 0.70 / height, subtitle, color=SUBTLE, fontsize=11, va="top")
    fig.text(
        0.055, 1.04 / height, "Frames missing or late, % of the worst receiver’s 360 frames",
        fontsize=10, fontweight="semibold", va="center",
    )
    legend = fig.add_axes((0.055, 0.16 / height, 0.9, 0.71 / height))
    legend.set_axis_off()
    for index, (_, label, color) in enumerate(SCALE):
        x = index / 6
        legend.add_patch(Rectangle((x, 0.60), 0.021, 0.29, facecolor=color, edgecolor="none"))
        legend.text(x + 0.028, 0.74, label, fontsize=9.5, color=SUBTLE, va="center")
    legend.text(0, 0.24, "–  Not tested", fontsize=9.5, color=SUBTLE, va="center")
    legend.text(0.155, 0.24, "†  Transport failed", fontsize=9.5, color=SUBTLE, va="center")
    if note:
        legend.text(0.34, 0.24, note, fontsize=9, color=SUBTLE, va="center")
    return fig


def layout(fig, nrows, ncols=1, *, hspace=0.28, wspace=0.09):
    height = fig.get_figheight()
    return fig.add_gridspec(
        nrows, ncols, left=0.25, right=0.965, top=1 - 1.68 / height,
        bottom=1.38 / height, hspace=hspace, wspace=wspace,
    )


def save(fig, set_name) -> None:
    fig.savefig(DIRECTORY / f"case-matrix-{set_name.lower()}.png", dpi=160, facecolor="white")
    plt.close(fig)


def loss_matrix(atlas) -> None:
    rows = (
        "udp", "librist", "rist-rs", "rist-rs-tuned", "rist-rs-614", "srt", "srt-fec",
        "srt-fec-row", "raptorq", "raptorq-main", "raptorq-arq",
    )
    fig = page(
        12.4, 15.4, "A  ·  Random loss, bursts, and recurring outages",
        "420 cases  ·  London VLAN  ·  Single receiver  ·  Burst loss ≈2% on average  ·  20 ms outage every 2 s",
    )
    grid = layout(fig, 4, 2, hspace=0.38, wspace=0.11)
    for i, rtt in enumerate(RTTS):
        for j, (rung, rate) in enumerate((RUNGS[1], RUNGS[3])):
            matrix(atlas, fig.add_subplot(grid[i, j]), "A", f"{rate} Mbit/s · {rtt} ms RTT",
                   rows, LOSS_PROFILES, {"rung": rung, "rtt_ms": rtt}, show_labels=j == 0)
    save(fig, "A")


def throughput_matrix(atlas) -> None:
    rows = (
        "udp", "librist", "rist-rs", "rist-rs-tuned", "rist-rs-614", "srt", "srt-fec",
        "raptorq", "raptorq-main", "raptorq-arq", "raptorq-client", "raptorq-contrib",
    )
    fig = page(
        11.4, 13.4, "B  ·  Throughput ladder",
        "216 cases  ·  London VLAN  ·  Single receiver  ·  H.264 source rates shown above each matrix",
    )
    grid = layout(fig, 3, hspace=0.33)
    columns = [(f"{rate} Mbit/s", {"rung": rung}) for rung, rate in RUNGS]
    for i, (rtt, loss) in enumerate((("1", "0"), ("30", "1%"), ("250", "1%"))):
        condition = "Clean" if loss == "0" else "1% packet loss"
        matrix(atlas, fig.add_subplot(grid[i]), "B", f"{rtt} ms RTT · {condition}",
               rows, columns, {"rtt_ms": rtt, "loss": loss})
    save(fig, "B")


def parameter_matrix(atlas) -> None:
    arq = ("librist", "rist-rs", "rist-rs-tuned", "srt", "srt-fec", "raptorq-arq")
    buffers = [(f"{ratio}×", {"latency_mult": ratio}) for ratio in ("1.5", "2.5", "4", "6")]
    repair = [(f"{ratio}%", {"repair": f"{ratio / 100:g}"}) for ratio in (5, 10, 20, 40)]
    fig = page(
        11.8, 9.2, "C  ·  Retransmission buffers and repair ratios",
        "112 cases  ·  London VLAN  ·  Single receiver  ·  80.9 Mbit/s H.264",
        "FEC-only cases used a configured budget of 2× RTT.",
    )
    grid = layout(fig, 2, 2, hspace=0.55, wspace=0.16)
    for i, rtt in enumerate(("30", "250")):
        for j, loss in enumerate(("1%", "5%")):
            subgrid = grid[i, j].subgridspec(2, 1, height_ratios=(6, 1), hspace=0.75)
            filters = {"rtt_ms": rtt, "loss": loss}
            matrix(atlas, fig.add_subplot(subgrid[0]), "C",
                   f"{rtt} ms RTT · {loss} loss\nARQ buffer as a multiple of RTT", arq,
                   buffers, filters, show_labels=j == 0)
            matrix(atlas, fig.add_subplot(subgrid[1]), "C", "FEC-only repair as % of source symbols",
                   ("raptorq-fixed",), repair, filters, show_labels=j == 0)
    save(fig, "C")


def outage_matrix(atlas) -> None:
    rows = ("udp", "librist", "rist-rs", "rist-rs-tuned", "srt", "srt-fec", "raptorq", "raptorq-arq")
    columns = [(f"{duration:,} ms", {"outage": f"out{duration}@7s"}) for duration in (50, 200, 500, 1000)]
    fig = page(
        10.8, 5.65, "D  ·  Single outages",
        "32 cases  ·  London VLAN  ·  100 ms RTT  ·  80.9 Mbit/s H.264  ·  400 ms configured budget",
    )
    grid = layout(fig, 1)
    matrix(atlas, fig.add_subplot(grid[0]), "D", "Outage duration · starts 7 s into the stream", rows, columns, {})
    save(fig, "D")


def jitter_matrix(atlas) -> None:
    rows = ("udp", "librist", "rist-rs", "srt", "srt-reorder", "srt-fec", "raptorq")
    columns = [
        (f"{rate} Mbit/s\n{label}", {"rung": rung, "loss": loss})
        for rung, rate in (RUNGS[1], RUNGS[3])
        for loss, label in (("0", "Clean"), ("1%", "1% loss"))
    ]
    fig = page(
        10.8, 5.6, "E  ·  Jitter and SRT reorder tolerance",
        "28 cases  ·  London VLAN  ·  30 ms RTT  ·  5 ms configured jitter  ·  Single receiver",
    )
    grid = layout(fig, 1)
    matrix(atlas, fig.add_subplot(grid[0]), "E", "Source rate and injected random packet loss", rows, columns, {})
    save(fig, "E")


def fanout_matrix(atlas) -> None:
    rows = ("librist", "rist-rs", "srt", "srt-fec", "raptorq")
    columns = [
        (f"{rate} Mbit/s\n{receivers} receivers", {"rung": rung, "fanout": receivers})
        for (rung, rate), receivers in (
            (RUNGS[1], "10"), (RUNGS[2], "10"), (RUNGS[0], "100"), (RUNGS[1], "100"),
        )
    ]
    fig = page(
        10.8, 4.95, "F  ·  Sender fan-out",
        "20 cases  ·  London VLAN  ·  30 ms RTT  ·  1% injected random packet loss",
    )
    grid = layout(fig, 1)
    matrix(atlas, fig.add_subplot(grid[0]), "F", "Source rate per receiver and simultaneous receivers", rows, columns, {})
    save(fig, "F")


def regional_matrix(atlas) -> None:
    rows = (
        "udp", "librist", "rist-rs", "rist-rs-tuned", "srt", "srt-fec", "raptorq",
        "raptorq-client", "raptorq-contrib",
    )
    columns = [
        (f"{rate}\n{label}", {"rung": rung, "loss": loss})
        for rung, rate in RUNGS[1:5]
        for loss, label in (("0", "Clean"), ("1%", "1% loss"))
    ]
    fig = page(
        11.4, 9.7, "G  ·  Real wide-area routes",
        "144 cases  ·  Single receiver  ·  Column headers show H.264 source rate (Mbit/s) and injected loss",
    )
    grid = layout(fig, 2, hspace=0.47)
    for i, (path, title) in enumerate((("nyc", "London to Newark · 73 ms RTT"), ("syd", "London to Sydney · 257 ms RTT"))):
        matrix(atlas, fig.add_subplot(grid[i]), "G", title, rows, columns, {"path": path})
    save(fig, "G")


def main() -> None:
    atlas = CaseAtlas()
    loss_matrix(atlas)
    throughput_matrix(atlas)
    parameter_matrix(atlas)
    outage_matrix(atlas)
    jitter_matrix(atlas)
    fanout_matrix(atlas)
    regional_matrix(atlas)
    atlas.check_complete()
    print(f"Rendered {len(atlas.rendered)} unique cases across seven matrices.")


if __name__ == "__main__":
    main()
