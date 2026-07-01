from app.worker.handlers.email_handler import EmailHandler
from app.worker.handlers.webhook_handler import WebhookHandler
from app.worker.handlers.report_handler import ReportHandler
from app.worker.handlers.batch_handler import BatchHandler
from app.models.job import JobType

# Maps job_type to its handler class.
# The worker uses this to dispatch without a chain of if/elif.
HANDLER_REGISTRY = {
    JobType.EMAIL: EmailHandler,
    JobType.WEBHOOK: WebhookHandler,
    JobType.REPORT: ReportHandler,
    JobType.BATCH: BatchHandler,
}

__all__ = ["HANDLER_REGISTRY", "EmailHandler", "WebhookHandler", "ReportHandler", "BatchHandler"]
