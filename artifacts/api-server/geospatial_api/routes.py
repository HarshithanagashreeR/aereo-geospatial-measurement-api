"""HTTP endpoints for uploading files and retrieving measurements."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path, PurePosixPath
from typing import Annotated

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from .database import get_db
from .models import Feature, UploadedFile
from .processing import GeospatialProcessingError, process_file
from .schemas import FileSummary, MeasurementsResponse

router = APIRouter()
MAX_UPLOAD_BYTES = 50 * 1024 * 1024
CHUNK_BYTES = 1024 * 1024


def _safe_filename(filename: str | None) -> str:
    value = (filename or "upload").replace("\\", "/")
    return PurePosixPath(value).name[:255] or "upload"


async def _save_upload(upload: UploadFile, target: Path) -> int:
    size = 0
    with target.open("wb") as output:
        while chunk := await upload.read(CHUNK_BYTES):
            size += len(chunk)
            if size > MAX_UPLOAD_BYTES:
                raise GeospatialProcessingError(
                    "Uploads are limited to 50 MiB."
                )
            output.write(chunk)
    return size


def _file_or_404(db: Session, file_id: int) -> UploadedFile:
    record = db.get(UploadedFile, file_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Uploaded file not found.")
    return record


@router.post(
    "/files/",
    response_model=FileSummary,
    status_code=status.HTTP_201_CREATED,
    summary="Upload and process a KML or zipped Shapefile",
)
async def upload_file(
    upload: Annotated[UploadFile, File(description="A .kml file or ZIP containing one Shapefile")],
    db: Annotated[Session, Depends(get_db)],
) -> UploadedFile:
    filename = _safe_filename(upload.filename)
    extension = Path(filename).suffix.lower()
    file_format = extension.removeprefix(".") or "unknown"
    record = UploadedFile(
        original_filename=filename,
        content_type=upload.content_type,
        format=file_format,
        size_bytes=0,
        status="processing",
    )
    db.add(record)
    db.commit()
    db.refresh(record)

    try:
        with tempfile.TemporaryDirectory(prefix="aereo-upload-") as temp_dir:
            target = Path(temp_dir) / filename
            try:
                record.size_bytes = await _save_upload(upload, target)
            except GeospatialProcessingError as exc:
                record.status = "failed"
                record.error_message = str(exc)
                db.commit()
                raise HTTPException(
                    status_code=413,
                    detail={"message": str(exc), "file_id": record.id},
                ) from exc

            if file_format not in {"kml", "zip"}:
                raise GeospatialProcessingError(
                    "Unsupported file type. Upload a .kml file or a .zip containing a Shapefile."
                )

            processed = process_file(str(target), file_format)
            record.source_crs = processed["source_crs"]
            record.measurement_crs = processed["measurement_crs"]
            record.feature_count = len(processed["features"])
            record.status = processed["status"]
            record.error_message = None
            db.add_all(
                [
                    Feature(file_id=record.id, **feature)
                    for feature in processed["features"]
                ]
            )
            db.commit()
            db.refresh(record)
            return record
    except HTTPException:
        raise
    except GeospatialProcessingError as exc:
        record.status = "failed"
        record.error_message = str(exc)
        db.commit()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"message": str(exc), "file_id": record.id},
        ) from exc
    except Exception as exc:
        db.rollback()
        persisted = db.get(UploadedFile, record.id)
        if persisted is not None:
            persisted.status = "failed"
            persisted.error_message = f"Processing failed: {exc}"
            db.commit()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"message": "File processing failed.", "file_id": record.id},
        ) from exc
    finally:
        await upload.close()


@router.get(
    "/files/{file_id}/",
    response_model=FileSummary,
    summary="Get uploaded file metadata and processing status",
)
def get_file(file_id: int, db: Annotated[Session, Depends(get_db)]) -> UploadedFile:
    return _file_or_404(db, file_id)


@router.get(
    "/files/{file_id}/measurements/",
    response_model=MeasurementsResponse,
    summary="Get feature geometries, properties, CRS, and measurements",
)
def get_measurements(
    file_id: int, db: Annotated[Session, Depends(get_db)]
) -> dict:
    record = _file_or_404(db, file_id)
    features = db.scalars(
        select(Feature).where(Feature.file_id == file_id).order_by(Feature.id)
    ).all()
    return {
        "file": record,
        "source_crs": record.source_crs,
        "measurement_crs": record.measurement_crs,
        "features": features,
    }


@router.get("/healthz", include_in_schema=False)
def health_check() -> dict[str, str]:
    return {"status": "ok"}
