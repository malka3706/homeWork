"""
Email job handler.

Simulates sending an email.

Expected payload keys:
    to      (str)  — recipient email address
    subject (str)  — email subject
    body    (str)  — email body text

Expected result:
    {"message_id": "<some-mock-id>", "status": "sent"}
"""
import time
import random
import uuid
from typing import Any

from app.models.job import Job
from app.worker.handlers.base import BaseHandler


class EmailHandler(BaseHandler):

    def execute(self, job: Job, db=None) -> dict[str, Any]:
        for key in ("to", "subject", "body"):
            if key not in job.payload:
                raise ValueError(f"Email job payload missing required field: '{key}'")

        time.sleep(random.uniform(1, 3))

        return {
            "message_id": str(uuid.uuid4()),
            "status": "sent",
            "to": job.payload["to"],
        }
