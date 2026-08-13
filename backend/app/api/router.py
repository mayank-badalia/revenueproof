"""Aggregate router. Feature routers are mounted here as each feature is built."""

from __future__ import annotations

from fastapi import APIRouter

from app.api import (
    anomaly, auth, contracts, events_api, identity, ingestion, pipeline,
    reconciliation, review, revenue, room, workspaces,
)

api_router = APIRouter()

api_router.include_router(auth.router)
api_router.include_router(workspaces.router)
api_router.include_router(events_api.router)
api_router.include_router(ingestion.router)
api_router.include_router(identity.router)
api_router.include_router(contracts.router)
api_router.include_router(reconciliation.router)
api_router.include_router(revenue.router)
api_router.include_router(anomaly.router)
api_router.include_router(review.router)
api_router.include_router(room.router)
api_router.include_router(pipeline.router)
