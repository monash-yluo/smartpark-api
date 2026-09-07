from pathlib import Path

import matplotlib.pyplot as plt
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch


OUTPUT_PATH = Path(__file__).with_name("cache_architecture.png")
INK = "#21343D"
GREEN = "#267653"
ORANGE = "#BC5738"
BLUE = "#297A99"
fig, ax = plt.subplots(figsize=(18, 12), dpi=180)
fig.patch.set_facecolor("white")
ax.set(xlim=(0, 18), ylim=(0, 12))
ax.axis("off")


def text(x, y, value, size=11, color=INK, weight="normal", align="center"):
    return ax.text(x, y, value, fontsize=size, color=color, fontweight=weight,
                   ha=align, va="center", linespacing=1.5, zorder=4)


def box(x, y, width, height, title, detail, fill="#F1F5F7", edge="#9AABB3"):
    ax.add_patch(FancyBboxPatch(
        (x, y), width, height, boxstyle="round,pad=0.02,rounding_size=0.08",
        facecolor=fill, edgecolor=edge, linewidth=1.4, zorder=2,
    ))
    text(x + width / 2, y + height * 0.76, title, 12, weight="bold")
    text(x + width / 2, y + height * 0.32, detail, 10)


def route(points, label="", label_at=None, color=BLUE):
    for start, end in zip(points[:-2], points[1:-1]):
        ax.plot([start[0], end[0]], [start[1], end[1]], color=color,
                linewidth=1.6, zorder=3)
    ax.add_patch(FancyArrowPatch(points[-2], points[-1], arrowstyle="-|>",
                                mutation_scale=13, color=color, linewidth=1.6,
                                shrinkA=0, shrinkB=2, zorder=3))
    if label:
        artist = text(*label_at, label, 10, color)
        artist.set_bbox({"facecolor": "white", "edgecolor": "none", "pad": 2})


text(0.5, 11.5, "SmartPark | Pod A Cache Lookup", 25, weight="bold", align="left")
text(0.5, 11.02, "Local L1 cache and YOLO inference; shared Redis L2", 12, align="left")
ax.add_patch(FancyBboxPatch(
    (3.9, 2.35), 8.1, 8.1, boxstyle="round,pad=0.02,rounding_size=0.12",
    facecolor="#F8FAFB", edgecolor="#7D96A3", linewidth=1.6,
    linestyle="--", zorder=0,
))
text(4.2, 10.1, "POD A  /  FastAPI + L1 + YOLO", 14, weight="bold", align="left")
box(0.4, 8.4, 2.8, 1.05, "API clients", "Find / Image / Ops requests")
box(0.4, 6.45, 2.8, 1.05, "Load balancer", "Route request to Pod A")
box(4.5, 8.25, 4.0, 1.05, "1  Look up L1", "Per-car-park, in-memory TTL cache", "#EAF5EF", GREEN)
box(4.5, 6.45, 4.0, 1.05, "2  Look up Redis L2", "Only after L1 miss or expiry", "#FFF1E9", ORANGE)
box(4.5, 4.5, 4.0, 1.2, "3  Resolve cache miss", "Under local lock: recheck L1\nJoin existing task or create one")
box(4.5, 2.75, 4.0, 1.05, "4  YOLO inside Pod A", "New task runs local inference", "#EAF2F9", BLUE)
box(9.35, 8.25, 2.1, 1.05, "Return result", "Cached or new", "#EAF5EF", GREEN)
box(9.15, 2.75, 2.5, 1.05, "5  Store result", "Write L1, then L2", "#EAF5EF", GREEN)
box(13.45, 6.45, 3.9, 1.15, "Redis L2", "Shared across API Pods\nAnalysis JSON + Base64 PNG", "#FFF1E9", ORANGE)
box(13.45, 3.35, 3.9, 2.0, "Background refresh only", "L1 or L2 hit aged 20-30 s\nReturn cached value immediately\nStart / join local refresh task", "#EDF5F8", BLUE)

route([(1.8, 8.4), (1.8, 7.5)])
route([(3.2, 6.98), (3.55, 6.98), (3.55, 8.78), (4.5, 8.78)])
route([(6.5, 8.25), (6.5, 7.5)], "Miss / expired", (6.5, 7.87), ORANGE)
route([(8.5, 8.78), (9.35, 8.78)], "Hit", (8.93, 9.03), GREEN)
route([(8.5, 7.16), (13.45, 7.16)], "GET by car park ID", (11.45, 7.48))
route([(13.45, 6.68), (8.5, 6.68)], "Result / miss / unavailable", (11.1, 6.35))
route([(8.5, 6.98), (10.4, 6.98), (10.4, 8.25)], "L2 hit", (10.4, 7.88), GREEN)
route([(6.5, 6.45), (6.5, 5.7)], "Miss / error", (6.5, 6.07), ORANGE)
route([(6.5, 4.5), (6.5, 3.8)], "New task", (6.5, 4.15), ORANGE)
route([(8.5, 5.1), (10.4, 5.1), (10.4, 6.0)], color=GREEN)
text(9.95, 5.65, "Recheck hit /\nawait existing task", 10, GREEN)
route([(10.4, 6.0), (10.4, 6.68)], color=GREEN)
route([(8.5, 3.28), (9.15, 3.28)], color=GREEN)
route([(10.4, 3.8), (10.4, 5.1)], "Return", (10.85, 4.45), GREEN)
route([(11.65, 3.05), (12.7, 3.05), (12.7, 6.53), (13.45, 6.53)], color=GREEN)
text(12.7, 5.83, "SET + TTL", 10, GREEN)

text(15.4, 2.82, "Redis refresh lock: SET NX EX (30 s)", 10, BLUE, "bold")
text(15.4, 2.15, "Owner rechecks L2; runs YOLO if needed.\nLock held: skip this refresh.\nRedis error: refresh locally.", 10)
route([(15.4, 3.35), (15.4, 3.06)])
text(0.5, 1.43, "CACHE POLICY", 11, weight="bold", align="left")
text(0.5, 0.98, "0-20 s: fresh hit   |   20-30 s: serve + background refresh   |   Expired: continue lookup / recompute", 11, align="left")
text(0.5, 0.53, "L2 hits do not populate L1. Cold misses use per-Pod single-flight, not the Redis refresh lock.", 10, align="left")
text(0.5, 0.16, "Payload: available / occupied spaces, confidence, annotated PNG. Image acquisition omitted from this view.", 10, align="left")
fig.savefig(OUTPUT_PATH, bbox_inches="tight", facecolor=fig.get_facecolor())
plt.close(fig)
print(OUTPUT_PATH)