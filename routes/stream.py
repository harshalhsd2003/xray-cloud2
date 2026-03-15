import json
import asyncio
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query
from fastapi.responses import StreamingResponse, Response
from jose import JWTError, jwt
from auth import SECRET_KEY, ALGORITHM, ADMIN_USER

router = APIRouter()

# PERF FIX: increase ping interval — 5 s was generating noisy traffic that
# competed with frame bytes on the same WebSocket.
PC_PING_INTERVAL = 15


class ConnectionManager:
    def __init__(self):
        self.clients:            list         = []
        self.pc_socket:          WebSocket    = None
        self.latest_frame:       bytes        = None
        self.camera_snapshots:   dict         = {}
        self.pc_current_cam:     int          = 0
        self._pc_ping_task:      asyncio.Task = None
        self._broadcast_lock:    asyncio.Lock = asyncio.Lock()
        # Track when PC first connected so web UI can show real uptime
        self.pc_connected_at:    float        = None   # epoch seconds (float)

    # ── CLIENT CONNECTIONS ────────────────────────────────────────────
    async def connect_client(self, ws: WebSocket):
        await ws.accept()
        self.clients.append(ws)

    def disconnect_client(self, ws: WebSocket):
        if ws in self.clients:
            self.clients.remove(ws)

    # ── PC CONNECTION ─────────────────────────────────────────────────
    async def connect_pc(self, ws: WebSocket):
        import time as _time
        await ws.accept()
        if self.pc_socket:
            try:
                await self.pc_socket.close()
            except Exception:
                pass
        self.pc_socket = ws
        self.pc_connected_at = _time.time()   # record real connection epoch
        print("[WS] Scanner PC connected")

        if self._pc_ping_task:
            self._pc_ping_task.cancel()
        self._pc_ping_task = asyncio.create_task(self._ping_pc_loop())

        await self.broadcast_event({
            "type": "pc_status",
            "online": True,
            "connected_at": self.pc_connected_at   # ← send epoch to all clients
        })

        try:
            from routes.notifications import notify_pc_online
            asyncio.create_task(notify_pc_online())
        except Exception:
            pass

    async def disconnect_pc(self):
        print("[WS] Scanner PC disconnected")
        self.pc_socket    = None
        self.latest_frame = None
        self.pc_connected_at = None

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
        """
        Broadcast a raw JPEG frame to all watch clients.
        PERF FIX: skip clients that are slow/backlogged rather than letting
        one slow client hold up all others.
        """
        self.latest_frame = frame_bytes
        self.camera_snapshots[self.pc_current_cam] = frame_bytes

        dead = []
        for ws in self.clients:
            try:
                # BUG FIX: use wait_for with a short timeout so a stalled client
                # doesn't block the entire broadcast loop
                await asyncio.wait_for(ws.send_bytes(frame_bytes), timeout=0.5)
            except asyncio.TimeoutError:
                # Client is too slow — skip this frame, don't drop the client
                pass
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
        return self.camera_snapshots.get(cam_idx)


manager = ConnectionManager()


# ── TOKEN VALIDATION ─────────────────────────────────────────────────
def verify_ws_token(token: str):
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        if payload.get("sub") != ADMIN_USER:
            return None
        return payload.get("sub")
    except JWTError:
        return None


# ── PC WEBSOCKET ──────────────────────────────────────────────────────
@router.websocket("/pc")
async def pc_websocket(ws: WebSocket, token: str = Query(...)):
    if not verify_ws_token(token):
        await ws.close(code=4001)
        return

    await manager.connect_pc(ws)

    try:
        while True:
            data = await ws.receive()

            frame = data.get("bytes") or data.get("binary")
            if frame:
                await manager.broadcast_frame(frame)
                continue

            text = data.get("text")
            if text:
                try:
                    msg = json.loads(text)
                    if msg.get("type") == "pong":
                        continue
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

    await ws.send_text(json.dumps({
        "type":         "pc_status",
        "online":       manager.pc_socket is not None,
        "connected_at": manager.pc_connected_at
    }))

    # Send latest frame so the watch client gets a picture immediately
    if manager.latest_frame:
        try:
            await ws.send_bytes(manager.latest_frame)
        except Exception:
            pass

    try:
        while True:
            data = await ws.receive_text()
            try:
                cmd = json.loads(data)
                await manager.send_command_to_pc(cmd)
            except Exception:
                pass
    except WebSocketDisconnect:
        manager.disconnect_client(ws)


# ── SNAPSHOT ENDPOINT ────────────────────────────────────────────────
@router.get("/snapshot")
async def snapshot_endpoint(
    cam: int   = Query(0, ge=0, le=7),
    token: str = Query(...)
):
    if not verify_ws_token(token):
        return Response(status_code=401)

    jpg_bytes = manager.get_snapshot(cam)
    if not jpg_bytes:
        return Response(status_code=204)

    return Response(
        content=jpg_bytes,
        media_type="image/jpeg",
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"}
    )


# ── MJPEG PROXY ───────────────────────────────────────────────────────
BOUNDARY = b"--mjpegframe"


async def mjpeg_generator():
    """
    Yield MJPEG frames.
    PERF FIX: track last-sent frame by object identity (id()) rather than
    reference equality — avoids sending the same bytes object twice if the
    reference hasn't changed since last poll, and correctly detects new frames
    even when the size is identical.
    PERF FIX: reduced poll sleep from 33 ms to 20 ms to reduce buffering delay
    while still keeping CPU usage low.
    """
    last_id = None
    while True:
        frame = manager.latest_frame
        fid = id(frame) if frame is not None else None
        if frame is not None and fid != last_id:
            last_id = fid
            yield (
                BOUNDARY + b"\r\n"
                b"Content-Type: image/jpeg\r\n"
                b"Content-Length: " + str(len(frame)).encode() + b"\r\n\r\n" +
                frame + b"\r\n"
            )
        await asyncio.sleep(0.020)   # 50 fps cap — smooth but not wasteful


@router.get("/mjpeg")
async def mjpeg_stream(token: str = Query(...)):
    if not verify_ws_token(token):
        return Response(status_code=401)

    return StreamingResponse(
        mjpeg_generator(),
        media_type="multipart/x-mixed-replace; boundary=mjpegframe",
        headers={
            "Cache-Control":               "no-cache, no-store",
            "X-Accel-Buffering":           "no",
            "Access-Control-Allow-Origin": "*",
        }
    )
