from pathlib import Path
import csv

import matplotlib.pyplot as plt


pod_counts = [1, 2, 4, 8]
max_supported_users = [35, 80, 80, 80]

output_path = Path(__file__).with_name("max_supported_users_by_pod.png")

plt.style.use("seaborn-v0_8-whitegrid")
fig, ax = plt.subplots(figsize=(8, 5), dpi=180)
fig.patch.set_facecolor("#f7f5ef")
ax.set_facecolor("#fffdf8")

ax.plot(
    pod_counts,
    max_supported_users,
    color="#176b87",
    linewidth=2.5,
    marker="o",
    markersize=8,
    markerfacecolor="#e07a5f",
    markeredgecolor="#fffdf8",
    markeredgewidth=2,
)

for pod_count, user_count in zip(pod_counts, max_supported_users):
    ax.annotate(
        f"{user_count} users",
        (pod_count, user_count),
        xytext=(0, 12),
        textcoords="offset points",
        ha="center",
        color="#24323d",
        fontsize=10,
        fontweight="bold",
    )

ax.set_title("Maximum Supported Users by Pod Count", loc="left", pad=18, fontsize=15, fontweight="bold", color="#24323d")
ax.set_xlabel("Number of Pods", labelpad=10, color="#24323d")
ax.set_ylabel("Maximum Supported Users", labelpad=10, color="#24323d")
ax.set_xticks(pod_counts)
ax.set_ylim(0, 100)
ax.set_yticks(range(0, 101, 20))
ax.spines["top"].set_visible(False)
ax.spines["right"].set_visible(False)
ax.spines["left"].set_color("#a9b8bd")
ax.spines["bottom"].set_color("#a9b8bd")
ax.grid(axis="y", color="#d9e0df", linewidth=0.8)
ax.grid(axis="x", visible=False)

fig.text(
    0.125,
    0.02,
    "Based on sustainable-load test results; acceptable failure rate maintained.",
    fontsize=8.5,
    color="#5d6b70",
)
fig.tight_layout(rect=(0, 0.05, 1, 1))
fig.savefig(output_path, bbox_inches="tight", facecolor=fig.get_facecolor())
print(output_path)


csv_path = Path(__file__).parents[1] / "pod_performance_sustainable_load.csv"
response_time_by_pod = {}
user_loads = set()

with csv_path.open(newline="", encoding="utf-8") as csv_file:
    for row in csv.DictReader(csv_file):
        pod_count = int(row["Pod number"])
        test_users = int(row["Test Users"])
        response_time = float(row["Avg Response Time (ms)"])
        response_time_by_pod.setdefault(pod_count, {})[test_users] = response_time
        user_loads.add(test_users)

response_time_output_path = Path(__file__).with_name("avg_response_time_by_pod.png")
fig, ax = plt.subplots(figsize=(8, 5), dpi=180)
fig.patch.set_facecolor("#f7f5ef")
ax.set_facecolor("#fffdf8")

line_colors = {1: "#176b87", 2: "#e07a5f", 4: "#5a9367", 8: "#8c5e8a"}
for pod_count in sorted(response_time_by_pod):
    points = response_time_by_pod[pod_count]
    sorted_users = sorted(points)
    ax.plot(
        sorted_users,
        [points[user_count] for user_count in sorted_users],
        label=f"{pod_count} Pod" if pod_count == 1 else f"{pod_count} Pods",
        color=line_colors.get(pod_count, "#24323d"),
        linewidth=2.2,
        marker="o",
        markersize=6,
    )

ax.set_title("Average Response Time by Pod Configuration", loc="left", pad=18, fontsize=15, fontweight="bold", color="#24323d")
ax.set_xlabel("Test Users", labelpad=10, color="#24323d")
ax.set_ylabel("Avg Response Time (ms)", labelpad=10, color="#24323d")
ax.set_xticks(sorted(user_loads))
ax.legend(frameon=False, ncol=2)
ax.spines["top"].set_visible(False)
ax.spines["right"].set_visible(False)
ax.spines["left"].set_color("#a9b8bd")
ax.spines["bottom"].set_color("#a9b8bd")
ax.grid(axis="y", color="#d9e0df", linewidth=0.8)
ax.grid(axis="x", visible=False)

fig.text(
    0.125,
    0.02,
    "Measured under sustainable-load tests; lines connect available test points.",
    fontsize=8.5,
    color="#5d6b70",
)
fig.tight_layout(rect=(0, 0.05, 1, 1))
fig.savefig(response_time_output_path, bbox_inches="tight", facecolor=fig.get_facecolor())
print(response_time_output_path)
