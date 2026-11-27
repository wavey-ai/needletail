"""Render the transport report figures from the published case results."""

from __future__ import annotations

import csv
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.ticker import ScalarFormatter


DIRECTORY = Path(__file__).resolve().parent
COLORS = {
    "librist": "#4867a8",
    "srt": "#b37316",
    "raptorq": "#08796d",
    "raptorq-arq": "#7960a7",
}
LABELS = {
    "librist": "RIST (librist)",
    "srt": "SRT (libsrt)",
    "raptorq": "RaptorQ FEC",
    "raptorq-arq": "RaptorQ + ARQ",
}
VARIANTS = tuple(COLORS)
INK = "#182733"
SUBTLE = "#435564"
GRID = "#e3e9ee"

plt.rcParams.update({
    "font.family": "DejaVu Sans",
    "font.size": 11,
    "text.color": INK,
    "axes.labelcolor": SUBTLE,
    "xtick.color": SUBTLE,
    "ytick.color": SUBTLE,
    "axes.spines.top": False,
    "axes.spines.right": False,
    "axes.spines.left": False,
    "axes.edgecolor": GRID,
    "axes.axisbelow": True,
    "grid.color": GRID,
})

with (DIRECTORY / "case-results.csv").open(newline="") as stream:
    CASES = list(csv.DictReader(stream))


def matched_case(**fields: str) -> dict[str, str]:
    match, = [case for case in CASES if all(case[key] == value for key, value in fields.items())]
    return match


def heading(fig, title: str, subtitle: str) -> None:
    fig.text(0.06, 0.94, title, fontsize=21, fontweight="bold", va="top")
    fig.text(0.06, 0.85, subtitle, color=SUBTLE, fontsize=11, va="top")


def save(fig, name: str) -> None:
    fig.savefig(DIRECTORY / name, dpi=160, facecolor="white")
    plt.close(fig)


def real_path_latency() -> None:
    fig, axes = plt.subplots(1, 2, sharex=True, sharey=True, figsize=(10.5, 4.4))
    fig.subplots_adjust(left=0.18, right=0.96, top=0.68, bottom=0.22, wspace=0.18)
    heading(
        fig,
        "RaptorQ added less delay on both real routes",
        "247.9 Mb/s H.264  •  1% injected random packet loss  •  All 360 frames on time in every shown case",
    )
    route_variants = ("raptorq", "srt", "librist")
    for ax, (path, title) in zip(axes, (
        ("nyc", "London to Newark · 73 ms RTT"),
        ("syd", "London to Sydney · 257 ms RTT"),
    )):
        for index, variant in enumerate(route_variants):
            row = matched_case(set="G", path=path, variant=variant, rung="2160p240", loss="1%")
            assert row["carried_whole"] == "True" and not row["added_censored"]
            latency = float(row["added_p99_ms"])
            ax.hlines(index, 10, latency, color=COLORS[variant], alpha=0.32, linewidth=3)
            ax.scatter(latency, index, s=54, color=COLORS[variant], zorder=3)
            offset = (-9, 8) if latency > 900 else (8, 0)
            horizontal = "right" if latency > 900 else "left"
            vertical = "bottom" if latency > 900 else "center"
            ax.annotate(f"{latency:,.1f} ms", (latency, index), xytext=offset,
                        textcoords="offset points", ha=horizontal, va=vertical,
                        fontsize=10, fontweight="bold")
        ax.set_title(title, fontsize=12, loc="left", pad=15)
        ax.set_xscale("log")
        ax.set_xlim(10, 2150)
        ax.set_ylim(2.5, -0.5)
        ax.set_xticks((10, 30, 100, 300, 1000))
        ax.xaxis.set_major_formatter(ScalarFormatter())
        ax.set_yticks(range(3), [LABELS[name] for name in route_variants])
        ax.tick_params(axis="both", length=0)
        ax.grid(axis="x")
    fig.text(0.57, 0.08, "Added frame completion delay, p99 (ms, logarithmic scale)",
             ha="center", color=SUBTLE)
    save(fig, "real-path-latency.png")


def loss_profiles() -> None:
    profiles = (
        ("1% random", "1%", ""),
        ("5% random", "5%", ""),
        ("Bursty loss", "ge", ""),
        ("20 ms outages", "0", "out20/2000ms"),
    )
    fig, ax = plt.subplots(figsize=(10.5, 4.75))
    fig.subplots_adjust(left=0.10, right=0.97, top=0.68, bottom=0.22)
    heading(
        fig,
        "FEC alone missed frames when losses clustered",
        "165.1 Mb/s H.264  •  Worst count across 30, 100 and 250 ms RTT  •  360 frames per case",
    )
    width = 0.18
    for index, variant in enumerate(VARIANTS):
        values = []
        for _, loss, outage in profiles:
            cases = [
                matched_case(set="A", variant=variant, rung="2160p160",
                             rtt_ms=rtt, loss=loss, outage=outage)
                for rtt in ("30", "100", "250")
            ]
            values.append(max(int(case["lost_or_late_max"]) for case in cases))
        positions = [group + (index - 1.5) * width for group in range(len(profiles))]
        bars = ax.bar(positions, values, width, color=COLORS[variant], label=LABELS[variant])
        ax.bar_label(bars, padding=3, fontsize=9)
    ax.set_xticks(range(len(profiles)), [profile[0] for profile in profiles])
    ax.set_ylabel("Frames missing or late")
    ax.set_ylim(0, 34)
    ax.set_yticks((0, 10, 20, 30))
    ax.grid(axis="y")
    ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.18), ncol=4, frameon=False)
    save(fig, "loss-profiles.png")


def single_outages() -> None:
    durations = (50, 200, 500, 1000)
    fig, (loss_ax, recovery_ax) = plt.subplots(1, 2, figsize=(10.5, 5))
    fig.subplots_adjust(left=0.09, right=0.98, top=0.65, bottom=0.24, wspace=0.31)
    heading(
        fig,
        "Retransmission changed the outage outcome",
        "80.9 Mb/s H.264  •  100 ms RTT  •  400 ms configured latency budget on all four variants",
    )
    markers = ("o", "s", "^", "D")
    for index, variant in enumerate(VARIANTS):
        cases = [matched_case(set="D", variant=variant, outage=f"out{duration}@7s")
                 for duration in durations]
        misses = [int(case["lost_or_late_max"]) for case in cases]
        recovery = [float(case["recovery_ms"]) for case in cases]
        for ax, values in ((loss_ax, misses), (recovery_ax, recovery)):
            ax.plot(durations, values, color=COLORS[variant], linewidth=2,
                    marker=markers[index], markersize=6, markerfacecolor="white",
                    label=LABELS[variant])
    loss_ax.set_title("Delivery deadline", loc="left", fontsize=12, pad=13)
    loss_ax.set_ylabel("Frames missing or late")
    loss_ax.set_ylim(-1, 27)
    recovery_ax.set_title("Time to steady delivery", loc="left", fontsize=12, pad=13)
    recovery_ax.set_ylabel("Recovery after outage (ms)")
    recovery_ax.set_ylim(-10, 320)
    for ax in (loss_ax, recovery_ax):
        ax.set_xticks(durations, [f"{duration:,}" for duration in durations])
        ax.set_xlim(0, 1060)
        ax.set_xlabel("Outage length (ms)")
        ax.grid(axis="y")
    handles, labels = loss_ax.get_legend_handles_labels()
    fig.legend(handles, labels, loc="lower center", bbox_to_anchor=(0.52, 0.025), ncol=4, frameon=False)
    save(fig, "single-outages.png")


if __name__ == "__main__":
    real_path_latency()
    loss_profiles()
    single_outages()
