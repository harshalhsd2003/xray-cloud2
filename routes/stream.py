import json
import asyncio
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query
from fastapi.responses import StreamingResponse, Response
from jose import JWTError, jwt
from auth import SECRET_KEY, ALGORITHM, ADMIN_USER

router = APIRouter()

PC_PING_INTERVAL = 5


class ConnectionManager:
    def __init__(self):
        self.clients:            list       = []
        self.pc_socket:          WebSocket  = None
        self.latest_frame:       bytes      = None
        # camera_idx → latest jpeg bytes for preview grid
        self.camera_snapshots:   dict       = {}
        # track what camera the PC is currently streaming
        self.pc_current_cam:     int        = 0
        self._pc_ping_task:      asyncio.Task = None

    # ── CLIENT CONNECTIONS ────────────────────────────────────────────
    async def connect_client(self, ws: WebSocket):
        await ws.accept()
        self.clients.append(ws)

    def disconnect_client(self, ws: WebSocket):
        if ws in self.clients:
            self.clients.remove(ws)

    # ── PC CONNECTION ─────────────────────────────────────────────────
    async def connect_pc(self, ws: WebSocket):
        await ws.accept()
        # Close any stale PC connection first
        if self.pc_socket:
            try:
                await self.pc_socket.close()
            except Exception:
                pass
        self.pc_socket = ws
        print("[WS] Scanner PC connected")

        if self._pc_ping_task:
            self._pc_ping_task.cancel()
        self._pc_ping_task = asyncio.create_task(self._ping_pc_loop())

        await self.broadcast_event({"type": "pc_status", "online": True})

        try:
            from routes.notifications import notify_pc_online
            asyncio.create_task(notify_pc_online())
        except Exception:
            pass

    async def disconnect_pc(self):
        print("[WS] Scanner PC disconnected")
        self.pc_socket     = None
        self.latest_frame  = None

        if self._pc_ping_task:
            self._pc_ping_task.cancel()
            self._pc_ping_task = None

        await self.broadcast_event({"type": "pc_status", "online": False})

        try:
            from routes.notifications import notify_pc_offline
            asyncio.create_task(notify_pc_offline())
        except Exception:
            pass

    async def _ping_pc_loop(self):
        try:
            while True:
                await asyncio.sleep(PC_PING_INTERVAL)
                if self.pc_socket is None:
                    break
                try:
                    await self.pc_socket.send_text(json.dumps({"type": "ping"}))
                except Exception:
                    print("[WS] PC ping failed — marking offline")
                    await self.disconnect_pc()
                    break
        except asyncio.CancelledError:
            pass

    # ── FRAME DISTRIBUTION ────────────────────────────────────────────
    async def broadcast_frame(self, frame_bytes: bytes):
        """Broadcast a raw JPEG frame to all watch clients.
           Also stores it as the latest snapshot for the current PC camera."""
        self.latest_frame = frame_bytes
        # Store snapshot keyed by the camera the PC is currently on
        self.camera_snapshots[self.pc_current_cam] = frame_bytes

        dead = []
        for ws in self.clients:
            try:
                await ws.send_bytes(frame_bytes)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect_client(ws)

    # ── EVENT DISTRIBUTION ────────────────────────────────────────────
    async def broadcast_event(self, event: dict):
        msg = json.dumps(event)
        dead = []
        for ws in self.clients:
            try:
                await ws.send_text(msg)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect_client(ws)

    # ── SEND COMMAND TO PC ────────────────────────────────────────────
    async def send_command_to_pc(self, cmd: dict) -> bool:
        if not self.pc_socket:
            return False
        try:
            await self.pc_socket.send_text(json.dumps(cmd))
            return True
        except Exception:
            await self.disconnect_pc()
            return False

    # ── SNAPSHOT RETRIEVAL ────────────────────────────────────────────
    def get_snapshot(self, cam_idx: int):
        """Return stored JPEG bytes for a camera index, or None."""
        return self.camera_snapshots.get(cam_idx)


manager = ConnectionManager()


# ── TOKEN VALIDATION (WebSocket / HTTP) ──────────────────────────────
def verify_ws_token(token: str):
    """Returns username string if valid, None if invalid."""
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        if payload.get("sub") != ADMIN_USER:
            return None
        return payload.get("sub")
    except JWTError:
        return None


# ── PC WEBSOCKET ─────────────────────────────────────────────────────
@router.websocket("/pc")
async def pc_websocket(ws: WebSocket, token: str = Query(...)):
    if not verify_ws_token(token):
        await ws.close(code=4001)
        return

    await manager.connect_pc(ws)

    try:
        while True:
            data = await ws.receive()

            # Binary data = JPEG video frame
            frame = data.get("bytes") or data.get("binary")
            if frame:
                await manager.broadcast_frame(frame)
                continue

            # Text data = JSON event / settings update
            text = data.get("text")
            if text:
                try:
                    msg = json.loads(text)
                    if msg.get("type") == "pong":
                        continue
                    # Track which camera the PC is currently streaming
                    if msg.get("type") == "settings_update" and "camera_index" in msg:
                        manager.pc_current_cam = int(msg["camera_index"])
                    await manager.broadcast_event(msg)
                except Exception as e:
                    print("[WS] JSON parse error:", e)

    except WebSocketDisconnect:
        await manager.disconnect_pc()


# ── WATCH WEBSOCKET ───────────────────────────────────────────────────
@router.websocket("/watch")
async def watch_websocket(ws: WebSocket, token: str = Query(...)):
    if not verify_ws_token(token):
        await ws.close(code=4001)
        return

    await manager.connect_client(ws)

    # Immediately send current PC status so badge is accurate
    await ws.send_text(json.dumps({
        "type":   "pc_status",
        "online": manager.pc_socket is not None
    }))

    # Send the last available frame if we have one
    if manager.latest_frame:
        try:
            await ws.send_bytes(manager.latest_frame)
        except Exception:
            pass

    try:
        while True:
            # Any text the watch client sends is forwarded to the PC as a command
            data = await ws.receive_text()
            try:
                cmd = json.loads(data)
                await manager.send_command_to_pc(cmd)
            except Exception:
                pass
    except WebSocketDisconnect:
        manager.disconnect_client(ws)


# ── SNAPSHOT ENDPOINT — for camera preview grid ──────────────────────
@router.get("/snapshot")
async def snapshot_endpoint(
    cam: int    = Query(0, ge=0, le=7),
    token: str  = Query(...)
):
    """Return a single JPEG frame for the specified camera index.
       Used by the Camera pane preview grid in the admin panel."""
    if not verify_ws_token(token):
        return Response(status_code=401)

    jpg_bytes = manager.get_snapshot(cam)
    if not jpg_bytes:
        # 204 No Content — browser img.onerror fires, shows "NO SIGNAL"
        return Response(status_code=204)

    return Response(
        content=jpg_bytes,
        media_type="image/jpeg",
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"}
    )


# ── MJPEG PROXY ───────────────────────────────────────────────────────
BOUNDARY = b"--mjpegframe"


async def mjpeg_generator():
    """Yield MJPEG frames at up to ~30 fps.
       Only sends a new chunk when a NEW frame has arrived."""
    last_sent = None
    while True:
        frame = manager.latest_frame
        if frame is not None and frame is not last_sent:
            last_sent = frame
            yield (
                BOUNDARY + b"\r\n"
                b"Content-Type: image/jpeg\r\n"
                b"Content-Length: " + str(len(frame)).encode() + b"\r\n\r\n" +
                frame + b"\r\n"
            )
        await asyncio.sleep(0.033)   # poll at ~30 fps cap


@router.get("/mjpeg")
async def mjpeg_stream(token: str = Query(...)):
    if not verify_ws_token(token):
        return Response(status_code=401)

    return StreamingResponse(
        mjpeg_generator(),
        media_type="multipart/x-mixed-replace; boundary=mjpegframe",
        headers={
            "Cache-Control":     "no-cache, no-store",
            "X-Accel-Buffering": "no",
            "Access-Control-Allow-Origin": "*",
        }
    )
