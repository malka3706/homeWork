"""
Report job handler.

Simulates generating a report file.

Expected payload keys:
    report_type (str) — e.g. "monthly_sales", "user_activity"
    params      (dict) — arbitrary report parameters (optional)

Expected result:
    {"file_url": "https://mock-storage.example.com/reports/<id>.pdf", "pages": <int>}
"""
import time
import random
import uuid
from typing import Any

from app.models.job import Job
from app.worker.handlers.base import BaseHandler


class ReportHandler(BaseHandler):

    def execute(self, job: Job, db=None) -> dict[str, Any]:
        if "report_type" not in job.payload:
            raise ValueError("Report job payload missing required field: 'report_type'")

        time.sleep(random.uniform(3, 5))

        return {
            "file_url": f"https://mock-storage.example.com/reports/{uuid.uuid4()}.pdf",
            "report_type": job.payload["report_type"],
            "pages": random.randint(1, 50),
        }
