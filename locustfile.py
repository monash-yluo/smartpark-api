"""SmartPark API 的 Locust 负载测试脚本。 / Locust load test for the SmartPark API.

该脚本模拟用户搜索附近停车场并查看停车场标注图片。两个任务权重相同，
因此每次选择任务时各有约 50% 的概率被执行。
This script simulates users searching for nearby car parks and viewing annotated
car-park images. Both tasks have the same weight, so each is selected roughly 50%
of the time.
"""

import itertools
import random

from locust import HttpUser, between, constant, task


# 为每个虚拟用户生成进程内唯一且递增的编号。 / Generate a unique, increasing ID per virtual user in this process.
_user_ids = itertools.count(1)

# 单个 HTTP 请求的客户端超时；超过 10 秒会被 Locust 记录为失败。
# Client-side timeout for one HTTP request; requests exceeding 10 seconds are recorded as failures by Locust.
REQUEST_TIMEOUT_SECONDS = 10


class SmartParkUser(HttpUser):
    """模拟一名持续使用 SmartPark 的虚拟用户。 / Simulate one virtual user continuously using SmartPark."""

    # 每次请求完成后随机等待 1 至 2 秒，再选择并执行下一个任务。
    # Wait randomly for 1 to 2 seconds after a request before selecting the next task.
    wait_time = between(1, 2)

    # 可用于极限吞吐量测试，但会产生没有思考时间的连续请求。
    # Can be used for maximum-throughput testing, but sends continuous requests with no think time.
    # wait_time = constant(0)

    # 构造 30 个模拟停车场 ID：CBD_001 至 CBD_030。
    # Build 30 simulated car-park IDs, from CBD_001 through CBD_030.
    carpark_ids = [f"CBD_{number:03d}" for number in range(1, 31)]

    def on_start(self):
        """为新启动的虚拟用户分配稳定 ID。 / Assign a stable ID to a newly started virtual user."""
        # 同一用户的两个任务会复用此 ID，以模拟连续的用户活动。
        # Both tasks reuse this ID to simulate activity from the same user session.
        self.uid = f"load-test-user-{next(_user_ids):04d}"

    @task(1)
    def find_carparks(self):
        """搜索十个停车场候选项。 / Search for ten candidate car parks."""
        self.client.get(
            "/api/find-carparks",
            # uuid 标识虚拟用户；n=10 指定最多返回十个停车场。
            # uuid identifies the virtual user; n=10 requests up to ten car parks.
            params={"uuid": self.uid, "n": 10},
            # 固定统计名称，避免查询参数导致同一端点被拆分成多个统计项。
            # Use a fixed stats name so query parameters do not split one endpoint into multiple entries.
            name="/api/find-carparks",
            # 超时发生时 Locust 会记录失败；这不会保证服务器端任务被取消。
            # Locust records a failure on timeout; this does not guarantee cancellation on the server.
            timeout=REQUEST_TIMEOUT_SECONDS,
        )

    @task(1)
    def annotate_carpark(self):
        """随机查看一个停车场的标注图片。 / View an annotated image for a random car park."""
        # 均匀随机选择停车场，以便在测试期间覆盖全部模拟停车场。
        # Select uniformly at random to cover all simulated car parks during the test.
        carpark_id = random.choice(self.carpark_ids)
        self.client.get(
            "/api/annotate-carpark",
            # 将随机停车场和当前虚拟用户作为查询参数发送。
            # Send the random car park and current virtual user as query parameters.
            params={"carpark_id": carpark_id, "uuid": self.uid},
            # 所有停车场请求汇总在同一个端点统计项中。
            # Aggregate requests for all car parks under one endpoint statistics entry.
            name="/api/annotate-carpark",
            # 与搜索请求使用相同的 10 秒客户端超时。
            # Apply the same 10-second client-side timeout as the search request.
            timeout=REQUEST_TIMEOUT_SECONDS,
        )