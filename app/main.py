from fastapi import FastAPI

from app.api.routes import jobs, health

app = FastAPI(
    title="Job Queue Service",
    description="Distributed background job processing system.",
    version="1.0.0",
)

app.include_router(jobs.router)
app.include_router(health.router)
