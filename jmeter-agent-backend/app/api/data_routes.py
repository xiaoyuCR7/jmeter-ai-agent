"""Test data management — CSV upload, list, delete for data-driven testing."""

import asyncio
import logging
import os
import uuid
import shutil
import tempfile
from typing import Optional, List
from app.core.errors import ServiceError
from fastapi import APIRouter, HTTPException, UploadFile, File, Query, Depends
from fastapi.responses import FileResponse

from app.core.config import settings

router = APIRouter(tags=["test-data"])
logger = logging.getLogger(__name__)

TEST_DATA_DIR = os.path.join(settings.BASE_DIR, "test_data")
UPLOAD_CHUNK_BYTES = 1024 * 1024
os.makedirs(TEST_DATA_DIR, exist_ok=True)


def _data_path(path: str, *, write: bool = False) -> str:
    """Resolve the real target before testing the data-directory boundary."""
    root = os.path.normcase(os.path.realpath(TEST_DATA_DIR))
    target = os.path.realpath(path)
    try:
        allowed = os.path.commonpath([root, os.path.normcase(target)]) == root
    except ValueError:
        allowed = False
    if not allowed:
        raise HTTPException(status_code=403, detail="Access denied")
    return target


async def _read_bounded_upload(file: UploadFile):
    """Read an upload in bounded chunks and spill larger bodies to disk."""
    limit = settings.TEST_DATA_MAX_UPLOAD_BYTES
    spool = tempfile.SpooledTemporaryFile(max_size=min(UPLOAD_CHUNK_BYTES, limit), mode="w+b")
    total = 0
    try:
        while True:
            # Read one byte beyond the remaining budget so an exact-limit file
            # can be distinguished from an oversized one without an unbounded read.
            chunk = await file.read(min(UPLOAD_CHUNK_BYTES, limit - total + 1))
            if not chunk:
                spool.seek(0)
                return spool
            total += len(chunk)
            if total > limit:
                raise HTTPException(
                    status_code=413,
                    detail=f"CSV file exceeds the configured upload limit of {limit} bytes",
                )
            spool.write(chunk)
    except BaseException:
        spool.close()
        raise


def _copy_upload_exclusive(spool, filepath: str):
    """Copy without overwriting a file created by a concurrent upload."""
    try:
        spool.seek(0)
        with open(filepath, "xb") as destination:
            shutil.copyfileobj(spool, destination, length=UPLOAD_CHUNK_BYTES)
    except FileExistsError:
        raise


async def _copy_upload_cancellable(spool, filepath: str):
    """Keep the spool alive until a worker-thread copy has really stopped."""
    task = asyncio.create_task(asyncio.to_thread(_copy_upload_exclusive, spool, filepath))
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        created = False
        try:
            await task
            created = True
        except FileExistsError:
            pass
        except Exception:
            # The copy helper removes partial output before surfacing its error.
            pass
        if created:
            try:
                os.remove(filepath)
            except FileNotFoundError:
                pass
            except OSError as error:
                logger.warning("Could not remove cancelled CSV upload %s: %s", filepath, error)
        raise
    except BaseException:
        try:
            os.remove(filepath)
        except FileNotFoundError:
            pass
        raise


def _inspect_csv(filepath: str):
    with open(filepath, "r", encoding="utf-8", errors="ignore") as stream:
        first_line = stream.readline().strip()
        columns = first_line.split(",") if first_line else []
        row_count = sum(1 for _ in stream)
    return columns, row_count, os.path.getsize(filepath)


@router.post("/data/upload")
async def upload_csv(
    file: UploadFile = File(..., description="CSV file for data-driven testing"),
    project_id: str = "default",
):
    """Upload a CSV file for use in data-driven JMeter tests.

    The file is saved to test_data/{project_id}/ and can be referenced
    in JMX test plans via CSV DataSet Config.
    """
    filename = file.filename or ""
    if not filename.endswith(".csv"):
        raise HTTPException(status_code=400, detail="Only CSV files are supported")

    from app.services.execution_lifecycle import validate_id
    try:
        validate_id(project_id)
    except ValueError:
        raise HTTPException(status_code=403, detail="Access denied") from None
    project_dir = _data_path(os.path.join(TEST_DATA_DIR, project_id), write=True)

    # Sanitize filename
    safe_name = filename.replace("..", "").replace("/", "").replace("\\", "")
    filepath = _data_path(os.path.join(project_dir, safe_name), write=True)
    if os.path.exists(filepath):
        base, ext = os.path.splitext(safe_name)
        safe_name = f"{base}_{str(uuid.uuid4())[:6]}{ext}"
        filepath = _data_path(os.path.join(project_dir, safe_name), write=True)

    spool = await _read_bounded_upload(file)
    try:
        # Body streaming may yield while membership or filesystem aliases change.
        project_dir = _data_path(project_dir, write=True)
        os.makedirs(project_dir, exist_ok=True)
        while True:
            filepath = _data_path(os.path.join(project_dir, safe_name), write=True)
            try:
                await _copy_upload_cancellable(spool, filepath)
                break
            except FileExistsError:
                base, ext = os.path.splitext(safe_name)
                safe_name = f"{base}_{str(uuid.uuid4())[:6]}{ext}"

        columns, row_count, size_bytes = await asyncio.to_thread(_inspect_csv, filepath)
    finally:
        spool.close()

    return {
        "filename": safe_name,
        "filepath": filepath,
        "project_id": project_id,
        "size_bytes": size_bytes,
        "row_count": row_count,
        "columns": columns,
        "message": f"CSV file uploaded: {safe_name} ({row_count} rows, {size_bytes} bytes)",
    }


@router.get("/data/files")
async def list_data_files(
    project_id: Optional[str] = Query(None, description="Filter by project"),
):
    """List all uploaded CSV test data files."""
    files = []
    base_dir = TEST_DATA_DIR

    if project_id:
        search_dirs = [_data_path(os.path.join(base_dir, project_id))]
    else:
        search_dirs = []
        if os.path.exists(base_dir):
            for item in os.listdir(base_dir):
                item_path = os.path.join(base_dir, item)
                if os.path.isdir(item_path):
                    try:
                        search_dirs.append(_data_path(item_path))
                    except (HTTPException, ServiceError) as error:
                        if error.status_code not in (403, 404):
                            raise
                        continue

    for search_dir in search_dirs:
        if not os.path.exists(search_dir):
            continue
        pid = os.path.basename(search_dir)
        for fname in os.listdir(search_dir):
            if fname.endswith(".csv"):
                try:
                    fpath = _data_path(os.path.join(search_dir, fname))
                except (HTTPException, ServiceError) as error:
                    if error.status_code not in (403, 404):
                        raise
                    continue
                files.append({
                    "filename": fname,
                    "filepath": fpath,
                    "project_id": pid,
                    "size_bytes": os.path.getsize(fpath),
                    "modified": os.path.getmtime(fpath),
                })

    return {"files": sorted(files, key=lambda f: f["modified"], reverse=True)}


@router.get("/data/preview")
async def preview_csv(
    filepath: str = Query(..., description="Full path to the CSV file"),
    rows: int = Query(10, ge=1, le=100, description="Number of rows to preview"),
):
    """Preview the first N rows of a CSV file."""
    filepath = _data_path(filepath)
    if not os.path.exists(filepath):
        raise HTTPException(status_code=404, detail="File not found")

    with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
        header = f.readline().strip()
        preview_lines = [f.readline().strip() for _ in range(rows)]
        preview_lines = [l for l in preview_lines if l]

    return {
        "filepath": filepath,
        "header": header,
        "columns": header.split(","),
        "rows": [line.split(",") for line in preview_lines],
        "preview_count": len(preview_lines),
    }


@router.delete("/data/files")
async def delete_data_file(
    filepath: str = Query(..., description="Full path to the CSV file"),
):
    """Delete an uploaded CSV file."""
    filepath = _data_path(filepath, write=True)
    if not os.path.exists(filepath):
        raise HTTPException(status_code=404, detail="File not found")
    if not filepath.endswith(".csv"):
        raise HTTPException(status_code=400, detail="Only CSV files can be deleted via this endpoint")

    os.remove(filepath)
    return {"message": f"Deleted: {os.path.basename(filepath)}"}
