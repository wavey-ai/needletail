"""Render the latency-by-RTT and fan-out figures from the published cases."""

from render_charts import COLORS, GRID, LABELS, SUBTLE, heading, matched_case, save

import matplotlib.pyplot as plt
from matplotlib.lines import Line2D
from matplotlib.ticker import ScalarFormatter


EXTRA_COLORS = {"rist-rs": "#677582", "srt-fec": "#a04b4b"}
EXTRA_LABELS = {"rist-rs": "Rust RIST (904e3e8)", "srt-fec": "SRT + FEC"}


def latency_by_rtt() -> None:
    rtts = (1, 30, 100, 250)
    profiles = (
        ("librist", "s", "-", 62),
        ("srt", "o", "--", 33),
        ("raptorq", "^", "-", 62),
        ("raptorq-arq", "D", "--", 33),
    )
    fig, (ax, censored_ax) = plt.subplots(
        2, 1, sharex=True, figsize=(10.5, 5.5), height_ratios=(6, 1)
    )
    fig.subplots_adjust(left=0.13, right=0.96, top=0.75, bottom=0.27, hspace=0.12)
    heading(
        fig,
        "RaptorQ kept added p99 low as RTT increased",
        "165.1 Mb/s H.264  •  1% random packet loss  •  360 frames per case  •  London with emulated RTT",
    )

    def case(variant: str, rtt: int) -> dict[str, str]:
        return matched_case(
            set="A", variant=variant, rung="2160p160", path="lon",
            rtt_ms=str(rtt), loss="1%", outage="",
        )

    for variant, marker, linestyle, size in profiles:
        rows = [case(variant, rtt) for rtt in rtts]
        assert all(row["status"] == "ok" and "p99" not in row["added_censored"] for row in rows)
        latencies = [float(row["added_p99_ms"]) for row in rows]
        color = COLORS[variant]
        ax.plot(rtts, latencies, color=color, linestyle=linestyle, linewidth=1.8, label=LABELS[variant])
        for rtt, latency, row in zip(rtts, latencies, rows):
            face = color if row["carried_whole"] == "True" else "white"
            ax.scatter(
                rtt, latency, s=size, marker=marker, facecolors=face,
                edgecolors=color, linewidths=1.2, zorder=3,
            )

    censored_rows = [case("rist-rs", rtt) for rtt in rtts]
    assert all(row["status"] == "ok" and "p99" in row["added_censored"] for row in censored_rows)
    censored_ax.set_facecolor("#f5f7f9")
    censored_ax.scatter(rtts, [0] * len(rtts), marker="x", color=EXTRA_COLORS["rist-rs"], s=36)
    censored_ax.set_yticks([0], ["p99 censored"])
    censored_ax.set_ylim(-0.6, 0.6)
    censored_ax.set_xticks(rtts)
    censored_ax.set_xlim(-8, 261)
    censored_ax.set_xlabel("Round-trip time (ms)")
    censored_ax.tick_params(axis="both", length=0)

    ax.set_yscale("log")
    ax.set_ylim(9, 1700)
    ax.set_yticks((10, 30, 100, 300, 1000))
    ax.yaxis.set_major_formatter(ScalarFormatter())
    ax.set_ylabel("Added p99 over UDP (ms)")
    ax.set_xticks(rtts)
    ax.tick_params(axis="both", length=0)
    ax.grid(axis="y")
    ax.grid(axis="x", color=GRID, linewidth=0.65)

    handles, labels = ax.get_legend_handles_labels()
    legend_items = [
        Line2D([], [], color=EXTRA_COLORS["rist-rs"], marker="x", linestyle="none"),
        Line2D([], [], color=SUBTLE, marker="o", markerfacecolor="white", linestyle="none"),
    ]
    handles.extend(legend_items)
    labels.extend((EXTRA_LABELS["rist-rs"], "Case missed a deadline"))
    fig.legend(handles, labels, loc="lower center", bbox_to_anchor=(0.52, 0.075), ncol=3, frameon=False)
    fig.text(
        0.06, 0.035, "Latency uses a log scale. Buffers and deadlines differ by transport.",
        color=SUBTLE, fontsize=9,
    )
    save(fig, "latency-by-rtt.png")


def fanout() -> None:
    profiles = (
        ("2160p25", "10", "24.9"),
        ("2160p80", "10", "80.9"),
        ("2160p5", "100", "5.6"),
        ("2160p25", "100", "24.9"),
    )
    variants = ("rist-rs", "librist", "srt", "srt-fec", "raptorq")
    colors = COLORS | EXTRA_COLORS
    labels = LABELS | EXTRA_LABELS
    fig, ax = plt.subplots(figsize=(10.5, 5.25))
    fig.subplots_adjust(left=0.10, right=0.97, top=0.73, bottom=0.28)
    heading(
        fig,
        "RaptorQ kept more receivers fully on time",
        "30 ms RTT  •  1% random packet loss  •  One sender  •  360 frames per receiver",
    )

    width = 0.15
    for index, variant in enumerate(variants):
        color = colors[variant]
        for group, (rung, fanout_count, _) in enumerate(profiles):
            row = matched_case(
                set="F", path="lon", variant=variant, rung=rung,
                rtt_ms="30", loss="1%", fanout=fanout_count,
            )
            receivers = int(row["receivers"])
            whole = int(row["receivers_whole"])
            assert int(row["receivers_with_summary"]) == receivers
            completion = 100 * whole / receivers
            position = group + (index - 2) * width
            if row["status"] == "transport_failed":
                assert completion == 0
                ax.scatter(position, 5, s=38, marker="x", color=color, zorder=3)
            else:
                assert row["status"] == "ok"
                ax.bar(position, completion, width * 0.88, color=color)
                if completion == 0:
                    ax.hlines(0.8, position - width / 3, position + width / 3, color, linewidth=2)
                label = f"{whole}/{receivers}" if whole else "0"
                ax.text(
                    position, completion + 2.2, label, ha="center", color=SUBTLE, fontsize=8.5,
                )

    ax.set_xticks(
        range(len(profiles)),
        [f"{n} receivers\n× {rate} Mb/s each" for _, n, rate in profiles],
    )
    ax.set_yticks((0, 25, 50, 75, 100), ["0%", "25%", "50%", "75%", "100%"])
    ax.set_ylim(0, 111)
    ax.set_ylabel("Receivers with every frame on time")
    ax.tick_params(axis="both", length=0)
    ax.grid(axis="y")
    handles = [Line2D([], [], color=colors[name], linewidth=6) for name in variants]
    handles.append(Line2D([], [], color=SUBTLE, marker="x", linestyle="none"))
    fig.legend(
        handles, [labels[name] for name in variants] + ["Transport case failed"],
        loc="lower center", bbox_to_anchor=(0.53, 0.087), ncol=3, frameon=False,
    )
    fig.text(
        0.06, 0.035, "At 100 × 24.9 Mb/s, the test VLAN saturated for several variants and none kept a receiver whole.",
        color=SUBTLE, fontsize=9,
    )
    save(fig, "fanout.png")


if __name__ == "__main__":
    latency_by_rtt()
    fanout()
