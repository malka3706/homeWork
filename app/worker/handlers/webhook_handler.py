"""
Webhook job handler.

Simulates calling an external HTTP webhook.
Intentionally fails 20% of the time to exercise retry logic.

Expected payload keys:
    url     (str)  — webhook URL to call
    method  (str)  — HTTP method, e.g. "POST" (optional, default "POST")
    data    (dict) — body to send (optional)

Expected result (on success):
    {"status_code": 200, "response": "ok"}
"""
import time
import random
from typing import Any

from app.models.job import Job
from app.worker.handlers.base import BaseHandler


class WebhookHandler(BaseHandler):

    FAILURE_RATE = 0.20  # 20% simulated failure

    def execute(self, job: Job, db=None) -> dict[str, Any]:
        if "url" not in job.payload:
            raise ValueError("Webhook job payload missing required field: 'url'")

        time.sleep(random.uniform(1, 2))

        if random.random() < self.FAILURE_RATE:
            raise RuntimeError("Simulated webhook failure")

        return {
            "status_code": 200,
            "response": "ok",
            "url": job.payload["url"],
        }
