"""Document ingest endpoints (M5 upload; M6 Google Docs link added later)."""
import logging

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from haven import docingest

log = logging.getLogger("haven")

router = APIRouter(prefix="/api/knowledge", tags=["docingest"])


@router.post("/upload")
async def upload(file: UploadFile = File(...), title: str = Form("")) -> dict:
    """Upload a local doc (docx/pdf/txt/md) → schema-valid source-page draft in
    the approval queue. Nothing writes to SecondBrain until GT approves."""
    data = await file.read()
    try:
        return await docingest.ingest_document(
            title_hint=title or file.filename, filename=file.filename or "upload",
            data=data, origin="upload")
    except docingest.IngestError as e:
        raise HTTPException(400, str(e))
    except Exception as e:  # noqa: BLE001
        log.error("upload ingest failed: %s", e)
        raise HTTPException(500, f"ingest failed: {type(e).__name__}: {e}")


@router.get("/ingests")
async def ingests() -> dict:
    return {"ingests": docingest.list_recent()}
