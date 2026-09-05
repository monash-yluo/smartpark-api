import itertools
import random

from locust import HttpUser, between, task


_user_ids = itertools.count(1)


class SmartParkUser(HttpUser):
    wait_time = between(1, 2)
    carpark_ids = [f"CBD_{number:03d}" for number in range(1, 17)]

    def on_start(self):
        self.uid = f"load-test-user-{next(_user_ids):04d}"

    @task(1)
    def find_carparks(self):
        self.client.get(
            "/api/find-carparks",
            params={"uuid": self.uid, "n": 3},
            name="/api/find-carparks",
        )

    @task(1)
    def annotate_carpark(self):
        carpark_id = random.choice(self.carpark_ids)
        self.client.get(
            "/api/annotate-carpark",
            params={"carpark_id": carpark_id, "uuid": self.uid},
            name="/api/annotate-carpark",
        )