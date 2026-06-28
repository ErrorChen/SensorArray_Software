from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response

from sensorarray_backend.api.dependencies import get_runtime
from sensorarray_backend.core.runtime import BackendRuntime
from sensorarray_backend.core.session_data import normalise_session_format

router = APIRouter(prefix="/api/export")


@router.get("/session")
def export_session(format: str = "h5", runtime: BackendRuntime = Depends(get_runtime)) -> Response:
    try:
        selected = normalise_session_format(format)
        data, media_type, extension = runtime.export_session_file(selected)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    headers = {"Content-Disposition": f'attachment; filename="sensorarray-session.{extension}"'}
    return Response(content=data, media_type=media_type, headers=headers)
