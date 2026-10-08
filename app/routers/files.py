import json
import shutil
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile
from fastapi.responses import JSONResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import config
from app.database import get_db
from app.models import Feature, UploadedFile
from app.schemas import FeatureMeasurement, FileInfo, MeasurementOut, MeasurementsResponse, MeasurementSummary
from app.services.parser import SUPPORTED_EXTENSIONS
from app.services.processing import process_file

router = APIRouter(prefix="/api/files", tags=["files"])


def _file_info(record: UploadedFile) -> FileInfo:
    return FileInfo(
        id=record.id,
        filename=record.filename,
        feature_count=record.feature_count,
        crs=record.crs,
        status=record.status,
        error=record.error,
    )


def _get_file_or_404(db: Session, file_id: str) -> UploadedFile:
    record = db.get(UploadedFile, file_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"File '{file_id}' not found.")
    return record


@router.post("/", response_model=FileInfo, status_code=201)
async def upload_file(file: UploadFile, db: Session = Depends(get_db)):
    """Upload a .zip (containing a Shapefile) or a .kml and process it immediately."""
    filename = Path(file.filename or "").name  # strip any client-supplied directories
    if not filename.lower().endswith(SUPPORTED_EXTENSIONS):
        raise HTTPException(status_code=400, detail="Unsupported file type. Upload a .zip (Shapefile) or .kml file.")

    record = UploadedFile(filename=filename)
    db.add(record)
    db.flush()  # assigns the id used for the storage folder

    save_dir = config.UPLOAD_DIR / record.id
    save_dir.mkdir(parents=True, exist_ok=True)
    save_path = save_dir / filename

    # Stream to disk in chunks so a huge upload can't exhaust memory.
    size = 0
    with save_path.open("wb") as out:
        while chunk := await file.read(1024 * 1024):
            size += len(chunk)
            if size > config.MAX_UPLOAD_BYTES:
                out.close()
                shutil.rmtree(save_dir, ignore_errors=True)
                db.rollback()
                raise HTTPException(status_code=413, detail="File too large.")
            out.write(chunk)

    process_file(db, record, save_path)

    if record.status == "FAILED":
        return JSONResponse(status_code=422, content={"detail": record.error, "id": record.id, "status": "FAILED"})
    return _file_info(record)


@router.get("/{file_id}/", response_model=FileInfo)
def get_file(file_id: str, db: Session = Depends(get_db)):
    return _file_info(_get_file_or_404(db, file_id))


@router.get("/{file_id}/measurements/", response_model=MeasurementsResponse)
def get_measurements(
    file_id: str,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    """Per-feature measurements (paginated) plus totals for the whole file."""
    record = _get_file_or_404(db, file_id)
    if record.status != "COMPLETED":
        raise HTTPException(status_code=409, detail=f"File status is {record.status}; measurements unavailable.")

    rows = db.scalars(
        select(Feature).where(Feature.file_id == file_id).order_by(Feature.index).limit(limit).offset(offset)
    ).all()

    totals = db.execute(
        select(
            func.coalesce(func.sum(Feature.measurement_value).filter(Feature.measurement_type == "area"), 0.0),
            func.coalesce(func.sum(Feature.measurement_value).filter(Feature.measurement_type == "length"), 0.0),
            func.count(Feature.measurement_value),
        ).where(Feature.file_id == file_id)
    ).one()

    return MeasurementsResponse(
        file_id=record.id,
        crs=record.crs,
        feature_count=record.feature_count,
        limit=limit,
        offset=offset,
        summary=MeasurementSummary(
            total_area_m2=round(totals[0], 4),
            total_length_m=round(totals[1], 4),
            measured_features=totals[2],
            unmeasured_features=record.feature_count - totals[2],
        ),
        features=[_feature_out(f, record.crs) for f in rows],
    )


def _feature_out(f: Feature, crs: str | None) -> FeatureMeasurement:
    measurement = None
    if f.measurement_type is not None:
        measurement = MeasurementOut(
            type=f.measurement_type, value=f.measurement_value, unit=f.measurement_unit, projected_crs=f.projected_crs
        )
    return FeatureMeasurement(
        feature_index=f.index,
        geometry_type=f.geometry_type,
        crs=crs,
        geometry=json.loads(f.geometry_json) if f.geometry_json else None,
        properties=json.loads(f.properties_json),
        measurement=measurement,
        note=f.note,
    )
