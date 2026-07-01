"""
Batch job handler.

Processes a list of items one by one, updating job.progress after each item.

Expected payload keys:
    items (list) — list of items to process (each item is a dict or scalar)

Expected result:
    {"processed": <int>, "failed": <int>, "total": <int>}
"""
import time
import random
from typing import Any

from sqlalchemy.orm import Session

from app.models.job import Job
from app.worker.handlers.base import BaseHandler


class BatchHandler(BaseHandler):

    def execute(self, job: Job, db: Session = None) -> dict[str, Any]:
        items = job.payload.get("items")
        if not isinstance(items, list):
            raise ValueError("Batch job payload must contain 'items' as a list")

        total = len(items)
        failed_count = 0

        for index, item in enumerate(items):
            time.sleep(random.uniform(0.1, 0.3))

            job.progress = int((index + 1) / total * 100)
            if db is not None:
                db.commit()

        return {
            "processed": total - failed_count,
            "failed": failed_count,
            "total": total,
        }
