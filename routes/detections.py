import os, io, base64
from fastapi import APIRouter, Depends, UploadFile, File, Form, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc
from database import Detection, get_db
from auth import verify_token
from datetime import datetime
import cloudinary
import cloudinary.uploader

router = APIRouter()

# Cloudinary setup (set env vars on Railway)
cloudinary.config(
    cloud_name = os.getenv("CLOUDINARY_CLOUD_NAME", ""),
    api_key    = os.getenv("CLOUDINARY_API_KEY", ""),
    api_secret = os.getenv("CLOUDINARY_API_SECRET", "")
)

USE_CLOUDINARY = bool(os.getenv("CLOUDINARY_CLOUD_NAME"))

# ── PC pushes detection here ──────────────────────────────────────────────────
@router.post("/push")
async def push_detection(
    class_name: str  = Form(...),
    conf:        float = Form(...),
    camera_idx:  int   = Form(0),
    image: UploadFile  = File(...),
    db: AsyncSession   = Depends(get_db),
    _=Depends(verify_token)
):
    now = datetime.utcnow()
    image_bytes = await image.read()

    os.makedirs("static/detections", exist_ok=True)

    # Always save locally as a fallback
    local_path = f"static/detections/{image.filename}"
    with open(local_path, "wb") as f:
        f.write(image_bytes)

    # BUG FIX: Railway filesystem is ephemeral — files vanish on redeploy.
    # Upload to Cloudinary when configured so images persist and are reachable
    # from any browser without depending on Railwayx local disk.
    if USE_CLOUDINARY:
        try:
            upload_result = cloudinary.uploader.upload(
                image_bytes,
                folder="xray_detections",
                public_id=image.filename.rsplit(".", 1)[0],
                overwrite=True,
                resource_type="image"
            )
            image_url = upload_result.get("secure_url", f"/static/detections/{image.filename}")
        except Exception:
            image_url = f"/static/detections/{image.filename}"
    else:
        image_url = f"/static/detections/{image.filename}"

    det = Detection(
        timestamp  = now,
        date_dmy   = now.strftime("%d/%m/%Y"),
        time_hms   = now.strftime("%I:%M:%S %p"),
        class_name = class_name,
        filename   = image.filename,
        image_url  = image_url,
        conf       = conf,
        camera_idx = camera_idx
    )
    db.add(det)
    await db.commit()
    await db.refresh(det)

    # Notify all connected WebSocket clients (mobile app, web panel)
    from routes.stream import manager
    await manager.broadcast_event({
        "type":       "new_detection",
        "id":         det.id,
        "class_name": det.class_name,
        "time_hms":   det.time_hms,
        "date_dmy":   det.date_dmy,
        "conf":       det.conf,
        "image_url":  det.image_url,
        "camera_idx": det.camera_idx
    })

    # Send Web Push + Expo notification to all subscribed devices
    try:
        from routes.notifications import notify_detection
        await notify_detection(
            class_name=det.class_name,
            conf=det.conf or 0,
            camera_idx=det.camera_idx,
            time_hms=det.time_hms,
            image_url=det.image_url,
            det_id=det.id
        )
    except Exception:
        pass  # Push is best-effort — never block the detection save

    return {"id": det.id, "image_url": image_url}

# ── Admin / App fetches detection list ───────────────────────────────────────
@router.get("/list")
async def list_detections(
    page:  int = Query(1, ge=1),
    limit: int = Query(30, le=100),
    db: AsyncSession = Depends(get_db),
    _=Depends(verify_token)
):
    offset = (page - 1) * limit
    result = await db.execute(
        select(Detection).order_by(desc(Detection.timestamp)).offset(offset).limit(limit)
    )
    rows = result.scalars().all()
    return [
        {
            "id":         r.id,
            "timestamp":  r.timestamp.isoformat(),
            "date_dmy":   r.date_dmy,
            "time_hms":   r.time_hms,
            "class_name": r.class_name,
            "image_url":  r.image_url,
            "conf":       r.conf,
            "camera_idx": r.camera_idx
        }
        for r in rows
    ]

# ── CSV export ────────────────────────────────────────────────────────────────
@router.get("/export")
async def export_csv(
    db: AsyncSession = Depends(get_db),
    _=Depends(verify_token)
):
    import csv, io as _io
    result = await db.execute(select(Detection).order_by(desc(Detection.timestamp)))
    rows = result.scalars().all()
    buf = _io.StringIO()
    w = csv.writer(buf)
    w.writerow(["ID","Timestamp","Date","Time","Class","Confidence","Camera","Image URL"])
    for r in rows:
        w.writerow([r.id, r.timestamp, r.date_dmy, r.time_hms,
                    r.class_name, r.conf, r.camera_idx, r.image_url])
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="detections.csv"'}
    )
