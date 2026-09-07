from pathlib import Path

import matplotlib.pyplot as plt
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch


OUTPUT_PATH = Path(__file__).with_name("cache_architecture.png")

COLORS = {
    "ink": "#17324D",
    "muted": "#60758A",
    "line": "#8BA5B8",
    "request": "#177E89",
    "hit": "#2D8A57",
    "miss": "#D46A3A",
    "refresh": "#8B5FBF",
    "panel": "#F4F8FB",
    "pod": "#FFFFFF",
    "l1": "#E7F4EE",
    "redis": "#FFF0EA",
    "compute": "#EAF1FA",
    "camera": "#FFF7DD",
}


def box(ax, x, y, width, height, title, subtitle, facecolor, edgecolor=None):
    patch = FancyBboxPatch(
        (x, y),
        width,
        height,
        boxstyle="round,pad=0.012,rounding_size=0.018",
        linewidth=1.8,
        edgecolor=edgecolor or COLORS["line"],
        facecolor=facecolor,
        zorder=3,
    )
    ax.add_patch(patch)
    ax.text(
        x + width / 2,
        y + height * 0.62,
        title,
        ha="center",
        va="center",
        fontsize=12,
        fontweight="bold",
        color=COLORS["ink"],
        zorder=4,
    )
    ax.text(
        x + width / 2,
        y + height * 0.28,
        subtitle,
        ha="center",
        va="center",
        fontsize=8.5,
        color=COLORS["muted"],
        linespacing=1.35,
        zorder=4,
    )


def arrow(
    ax,
    start,
    end,
    label="",
    color=None,
    connectionstyle="arc3,rad=0",
    label_offset=(0, 0.018),
    dashed=False,
):
    color = color or COLORS["line"]
    patch = FancyArrowPatch(
        start,
        end,
        arrowstyle="-|>",
        mutation_scale=14,
        linewidth=1.8,
        linestyle="--" if dashed else "-",
        color=color,
        connectionstyle=connectionstyle,
        shrinkA=3,
        shrinkB=3,
        zorder=2,
    )
    ax.add_patch(patch)
    if label:
        mid_x = (start[0] + end[0]) / 2 + label_offset[0]
        mid_y = (start[1] + end[1]) / 2 + label_offset[1]
        ax.text(
            mid_x,
            mid_y,
            label,
            ha="center",
            va="center",
            fontsize=8.5,
            fontweight="bold",
            color=color,
            bbox={"boxstyle": "round,pad=0.2", "fc": "white", "ec": "none", "alpha": 0.92},
            zorder=5,
        )


fig, ax = plt.subplots(figsize=(16, 9), dpi=180)
fig.patch.set_facecolor("#FFFFFF")
ax.set_xlim(0, 1)
ax.set_ylim(0, 1)
ax.axis("off")

ax.text(
    0.055,
    0.945,
    "SmartPark Two-Level Cache Architecture",
    fontsize=23,
    fontweight="bold",
    color=COLORS["ink"],
    va="top",
)
ax.text(
    0.055,
    0.895,
    "Fast per-Pod reads, shared cross-Pod results, and protected background refresh",
    fontsize=11,
    color=COLORS["muted"],
    va="top",
)

box(ax, 0.055, 0.54, 0.12, 0.13, "API Clients", "Find car parks\nImage & Ops APIs", "#F2F6F8")
box(ax, 0.225, 0.54, 0.13, 0.13, "Load Balancer", "Routes each request\nto an available Pod", "#F2F6F8")

pod_group = FancyBboxPatch(
    (0.405, 0.38),
    0.285,
    0.42,
    boxstyle="round,pad=0.018,rounding_size=0.02",
    linewidth=1.5,
    linestyle="--",
    edgecolor="#7EA0B7",
    facecolor=COLORS["panel"],
    zorder=1,
)
ax.add_patch(pod_group)
ax.text(0.425, 0.765, "FastAPI Pods", fontsize=12, fontweight="bold", color=COLORS["ink"], zorder=4)
ax.text(0.425, 0.735, "Each Pod owns L1 cache + YOLO model", fontsize=8.5, color=COLORS["muted"], zorder=4)

box(ax, 0.43, 0.59, 0.105, 0.115, "Pod A", "Request handler\nSingle-flight tasks", COLORS["pod"])
box(ax, 0.555, 0.59, 0.105, 0.115, "L1 Cache", "In-memory TTL\nThread-safe", COLORS["l1"], "#67A47F")
box(ax, 0.43, 0.445, 0.105, 0.09, "Pod B...N", "Same local\ncache pattern", COLORS["pod"])
box(ax, 0.555, 0.445, 0.105, 0.09, "YOLO model", "Local inference\nin every Pod", COLORS["compute"], "#648DB8")

box(ax, 0.77, 0.59, 0.16, 0.13, "Redis L2", "Shared analysis JSON\n+ Base64 annotated PNG", COLORS["redis"], "#D97850")
box(ax, 0.77, 0.39, 0.16, 0.11, "Refresh Lock", "Redis SET NX EX\none owner per car park", "#F3EAFB", "#9670BB")
box(ax, 0.77, 0.16, 0.16, 0.11, "Camera API", "Fetch latest car park image", COLORS["camera"], "#C9A84A")

arrow(ax, (0.175, 0.605), (0.225, 0.605), "HTTPS", COLORS["request"], label_offset=(0, 0.025))
arrow(ax, (0.355, 0.605), (0.43, 0.65), "route", COLORS["request"], label_offset=(0, 0.026))
arrow(ax, (0.535, 0.65), (0.555, 0.65), "1  lookup", COLORS["request"], label_offset=(0, 0.03))
arrow(ax, (0.66, 0.65), (0.77, 0.65), "2  L1 miss", COLORS["miss"], label_offset=(0, 0.03))
arrow(ax, (0.77, 0.61), (0.66, 0.61), "L2 hit", COLORS["hit"], connectionstyle="arc3,rad=-0.18", label_offset=(0, -0.018))

arrow(
    ax,
    (0.85, 0.59),
    (0.85, 0.50),
    "refresh due",
    COLORS["refresh"],
    label_offset=(0.06, 0),
    dashed=True,
)
arrow(
    ax,
    (0.77, 0.445),
    (0.85, 0.27),
    "3  lock owner",
    COLORS["miss"],
    connectionstyle="arc3,rad=-0.08",
    label_offset=(0.045, 0.005),
)
arrow(
    ax,
    (0.77, 0.215),
    (0.66, 0.49),
    "4  image to Pod",
    COLORS["request"],
    connectionstyle="arc3,rad=0.18",
    label_offset=(-0.035, 0.005),
)
arrow(
    ax,
    (0.61, 0.49),
    (0.77, 0.62),
    "5  write L2",
    COLORS["hit"],
    connectionstyle="arc3,rad=-0.08",
    label_offset=(0, 0.026),
)
arrow(
    ax,
    (0.61, 0.49),
    (0.61, 0.535),
    "YOLO",
    COLORS["refresh"],
    label_offset=(0.035, 0),
    dashed=True,
)

info = FancyBboxPatch(
    (0.055, 0.115),
    0.56,
    0.19,
    boxstyle="round,pad=0.018,rounding_size=0.015",
    linewidth=1.2,
    edgecolor="#CBD7DF",
    facecolor="#FAFCFD",
    zorder=1,
)
ax.add_patch(info)
ax.text(0.08, 0.265, "Cache policy", fontsize=11, fontweight="bold", color=COLORS["ink"])
policy_lines = [
    ("0-20 s", "Fresh hit: return immediately", COLORS["hit"]),
    ("20-30 s", "Serve current value and start one background refresh", COLORS["refresh"]),
    ("After 30 s", "Hard expiry: fetch image and run inference", COLORS["miss"]),
    ("Cache key", '("carpark-analysis", carpark_id) / smartpark:analysis:{id}', COLORS["request"]),
]
for index, (label, text, color) in enumerate(policy_lines):
    y = 0.228 - index * 0.035
    ax.text(0.08, y, label, fontsize=8.7, fontweight="bold", color=color, va="center")
    ax.text(0.16, y, text, fontsize=8.7, color=COLORS["muted"], va="center")

ax.text(
    0.055,
    0.055,
    "Cached payload: available spaces, occupied spaces, confidence score, and annotated PNG",
    fontsize=8.5,
    color=COLORS["muted"],
)
ax.text(
    0.945,
    0.055,
    "SmartPark API",
    fontsize=8.5,
    fontweight="bold",
    color=COLORS["ink"],
    ha="right",
)

fig.savefig(OUTPUT_PATH, bbox_inches="tight", facecolor=fig.get_facecolor())
plt.close(fig)
print(OUTPUT_PATH)