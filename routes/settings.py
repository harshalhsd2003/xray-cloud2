from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from pydantic import BaseModel
from typing import Optional
from database import SystemSettings, get_db
from auth import verify_token
from datetime import datetime
from routes.stream import manager
import asyncio

router = APIRouter()


class SettingsUpdate(BaseModel):
    confidence_threshold: Optional[float] = None
    confirm_frames: Optional[int] = None
    camera_index: Optional[int] = None


class ImageSettingsUpdate(BaseModel):
    brightness:  Optional[float] = None   # -100 to 100
    contrast:    Optional[float] = None   # 0.5 to 3.0
    saturation:  Optional[float] = None   # 0.0 to 3.0
    hue:         Optional[float] = None   # -90 to 90
    sharpness:   Optional[float] = None   # 0.0 to 5.0
    hflip:       Optional[bool]  = None
    vflip:       Optional[bool]  = None
    rotation:    Optional[int]   = None   # 0 | 90 | 180 | 270


@router.get("/")
async def get_settings(
    db: AsyncSession = Depends(get_db),
    _=Depends(verify_token)
):
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
        s.confidence_threshold = body.confidence_threshold
    if body.confirm_frames is not None:
        s.confirm_frames = body.confirm_frames
    if body.camera_index is not None:
        s.camera_index = body.camera_index

    s.updated_at = datetime.utcnow()
    await db.commit()

    cmd = {
        "type":                 "settings_update",
        "confidence_threshold": s.confidence_threshold,
        "confirm_frames":       s.confirm_frames,
        "camera_index":         s.camera_index,
    }

    await manager.send_command_to_pc(cmd)
    await manager.broadcast_event(cmd)

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


@router.post("/update_image")
async def update_image_settings(
    body: ImageSettingsUpdate,
    _=Depends(verify_token)
):
    """
    Relay image processing settings to the PC over WebSocket.
    These are NOT persisted in the DB — the PC holds the state locally.
    The admin panel sends them here and we forward via the PC socket.
    """
    payload = body.dict(exclude_none=True)

    cmd = {
        "type":            "settings_update",
        "image_settings":  payload,
    }

    # Forward to PC
    await manager.send_command_to_pc(cmd)
    # Also broadcast so other connected clients (mobile app) see the change
    await manager.broadcast_event(cmd)

    return {"ok": True, "image_settings": payload}


@router.get("/image")
async def get_image_settings(_=Depends(verify_token)):
    """
    Returns the last known image settings from the PC.
    The PC pushes its current settings periodically via status_update,
    but this endpoint can be polled if needed.
    """
    # We rely on the PC to push settings; just return defaults here
    # since we don't persist image settings on the cloud side.
    return {
        "brightness":  0,
        "contrast":    1.0,
        "saturation":  1.0,
        "hue":         0,
        "sharpness":   0.0,
        "hflip":       False,
        "vflip":       False,
        "rotation":    0,
    }
