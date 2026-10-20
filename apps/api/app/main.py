from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from app.auth_routes import router as auth_router
from app.communication_routes import router as communication_router
from app.database import database_is_ready
from app.engagement_routes import router as engagement_router
from app.hardening_routes import router as hardening_router
from app.observability import metrics_response, observability_middleware
from app.product_routes import router as product_router
from app.routes import router
from app.settings import settings

app = FastAPI(
    title="Risheh Engage API",
    version="0.8.0",
    docs_url="/docs" if settings.environment != "production" else None,
    redoc_url=None,
)
app.middleware("http")(observability_middleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(auth_router)
app.include_router(router)
app.include_router(communication_router)
app.include_router(engagement_router)
app.include_router(hardening_router)
app.include_router(product_router)


@app.get("/health", tags=["system"])
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "api", "environment": settings.environment}


@app.get("/ready", tags=["system"])
async def readiness() -> dict[str, str]:
    if not await database_is_ready():
        raise HTTPException(status_code=503, detail="Database is not ready")
    return {"status": "ready", "database": "ok"}


@app.get("/metrics", include_in_schema=False)
async def metrics():
    if not settings.metrics_enabled:
        raise HTTPException(status_code=404, detail="Metrics disabled")
    return metrics_response()
