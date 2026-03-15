from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from pydantic import BaseModel
from database import SystemSettings, get_db
from auth import verify_token
from datetime import datetime
from routes.stream import manager
import asyncio

router = APIRouter()

class SettingsUpdate(BaseModel):
    confidence_threshold: float | None = None
    confirm_frames: int | None = None
    camera_index: int | None = None


@router.get("/")
async def get_settings(db: AsyncSession = Depends(get_db), _=Depends(verify_token)):
    result = await db.execute(select(SystemSettings).limit(1))
    s = result.scalar()

    return {
        "confidence_threshold": s.confidence_threshold,
        "confirm_frames": s.confirm_frames,
        "camera_index": s.camera_index,
        "updated_at": s.updated_at.isoformat()
    }


@router.post("/update")
async def update_settings(body: SettingsUpdate, db: AsyncSession = Depends(get_db), _=Depends(verify_token)):

    result = await db.execute(select(SystemSettings).limit(1))
    s = result.scalar()

    if body.confidence_threshold is not None:
        s.confidence_threshold = body.confidence_threshold

    if body.confirm_frames is not None:
        s.confirm_frames = body.confirm_frames

    if body.camera_index is not None:
        s.camera_index = body.camera_index

    s.updated_at = datetime.utcnow()

    await db.commit()

    cmd = {
        "type": "settings_update",
        "confidence_threshold": s.confidence_threshold,
        "confirm_frames": s.confirm_frames,
        "camera_index": s.camera_index
    }

    await manager.send_command_to_pc(cmd)
    await manager.broadcast_event(cmd)

    # Push notification to all browsers + mobile
    try:
        from routes.notifications import notify_settings_changed
        asyncio.create_task(notify_settings_changed(
            confidence=s.confidence_threshold,
            frames=s.confirm_frames,
            camera=s.camera_index
        ))
    except Exception:
        pass

    return {"settings": cmd}