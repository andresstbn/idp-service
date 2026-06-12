"""Agrupación de rutas de la versión v1 de la API."""

from __future__ import annotations

from fastapi import APIRouter

from api.v1.endpoints import extract

api_router = APIRouter(prefix="/v1")
api_router.include_router(extract.router, tags=["extraction"])
