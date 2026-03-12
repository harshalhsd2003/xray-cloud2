from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from pydantic import BaseModel
from database import SystemSettings, get_db
from auth import verify_token
from datetime import datetime

router = APIRouter()

class SettingsUpdate(BaseModel):
    confidence_threshold: float | None = None
    confirm_frames:       int   | None = None
    camera_index:         int   | None = None

@router.get("/")
async def get_settings(db: AsyncSession = Depends(get_db), _=Depends(verify_token)):
    result = await db.execute(select(SystemSettings).limit(1))
    s = result.scalar()
    return {
        "confidence_threshold": s.confidence_threshold,
        "confirm_frames":       s.confirm_frames,
        "camera_index":         s.camera_index,
        "updated_at":           s.updated_at.isoformat()
    }

@router.post("/update")
async def update_settings(
    body: SettingsUpdate,
    db: AsyncSession = Depends(get_db),
    _=Depends(verify_token)
):
    result = await db.execute(select(SystemSettings).limit(1))
    s = result.scalar()

    if body.confidence_threshold is not None:
        s.confidence_threshold = round(max(0.1, min(1.0, body.confidence_threshold)), 2)
    if body.confirm_frames is not None:
        s.confirm_frames = max(1, min(30, body.confirm_frames))
    if body.camera_index is not None:
        s.camera_index = body.camera_index

    s.updated_at = datetime.utcnow()
    await db.commit()

    # Push new settings to all connected clients + PC
    from routes.stream import manager
    # send to scanner PC
    await manager.send_command_to_pc({
    "type": "settings_update",
    "confidence_threshold": s.confidence_threshold,
    "confirm_frames": s.confirm_frames,
    "camera_index": s.camera_index
})

    # also notify dashboard
    await manager.broadcast_event({
    "type": "settings_update",
    "confidence_threshold": s.confidence_threshold,
    "confirm_frames": s.confirm_frames,
    "camera_index": s.camera_index
})
