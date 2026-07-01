"""
Base handler interface.

Every job type implements this contract. The worker calls execute() and
expects either a result dict (success) or a raised exception (failure).
"""
from abc import ABC, abstractmethod
from typing import Any

from app.models.job import Job


class BaseHandler(ABC):

    @abstractmethod
    def execute(self, job: Job, db=None) -> dict[str, Any]:
        """
        Run the job and return a result dict that will be stored in job.result.

        Raise any exception to signal failure. The worker will handle retries.

        Args:
            job: The full Job ORM object. Read payload via job.payload.
            db:  Optional SQLAlchemy session. Only BatchHandler uses this for
                 mid-job progress commits; all other handlers ignore it.

        Returns:
            A dict — e.g. {"message_id": "abc123"} for email jobs.
        """
        ...
