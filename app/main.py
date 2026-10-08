from contextlib import asynccontextmanager

from fastapi import FastAPI

from app import config
from app.database import init_db
from app.routers import files


@asynccontextmanager
async def lifespan(app: FastAPI):
    (config.BASE_DIR / "data").mkdir(exist_ok=True)
    config.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    init_db()
    yield


app = FastAPI(
    title="Geospatial File Measurement API",
    description="Upload a Shapefile (.zip) or KML and get area/length measurements per feature.",
    version="1.0.0",
    lifespan=lifespan,
)
app.include_router(files.router)


@app.get("/health", tags=["meta"])
def health():
    return {"status": "ok"}
