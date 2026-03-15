import json
import asyncio
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query, Depends
from fastapi.responses import StreamingResponse, Response
from jose import JWTError, jwt
from auth import SECRET_KEY, ALGORITHM, ADMIN_USER, verify_token

router = APIRouter()

# How often to ping the PC to detect dead connections (seconds)
PC_PING_INTERVAL = 5


class ConnectionManager:
    def __init__(self):
        self.clients = []
        self.pc_socket = None
        self.latest_frame = None
        self._pc_ping_task = None

    async def connect_client(self, ws: WebSocket):
        await ws.accept()
        self.clients.append(ws)

    def disconnect_client(self, ws: WebSocket):
        if ws in self.clients:
            self.clients.remove(ws)

    async def connect_pc(self, ws: WebSocket):
        await ws.accept()

        # Close any existing stale PC connection first
        if self.pc_socket:
            try:
                await self.pc_socket.close()
            except:
                pass

        self.pc_socket = ws
        print("[WS] Scanner PC connected")

        # Cancel old ping task if running, start fresh
        if self._pc_ping_task:
            self._pc_ping_task.cancel()
        self._pc_ping_task = asyncio.create_task(self._ping_pc_loop())

        # Notify all watch clients that PC is now online
        await self.broadcast_event({"type": "pc_status", "online": True})

    async def disconnect_pc(self):
        print("[WS] Scanner PC disconnected")
        self.pc_socket = None
        self.latest_frame = None

        # Stop ping loop
        if self._pc_ping_task:
            self._pc_ping_task.cancel()
            self._pc_ping_task = None

        # Immediately tell all watch clients PC went offline
        await self.broadcast_event({"type": "pc_status", "online": False})

    async def _ping_pc_loop(self):
        """Send periodic pings to the PC. If ping fails, mark PC offline immediately."""
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

    async def broadcast_frame(self, frame_bytes):
        self.latest_frame = frame_bytes

        dead = []
        for ws in self.clients:
            try:
                await ws.send_bytes(frame_bytes)
            except:
                dead.append(ws)
        for ws in dead:
            self.disconnect_client(ws)

    async def broadcast_event(self, event):
        msg = json.dumps(event)

        dead = []
        for ws in self.clients:
            try:
                await ws.send_text(msg)
            except:
                dead.append(ws)
        for ws in dead:
            self.disconnect_client(ws)

    async def broadcast_settings(self, settings):
        msg = json.dumps({
        "type": "settings_update",
        "data": settings
        })

        for ws in self.clients:
            try:
                await ws.send_text(msg)
            except:
                pass
    
    async def send_command_to_pc(self, cmd):
        if not self.pc_socket:
            return False
        try:
            await self.pc_socket.send_text(json.dumps(cmd))
            return True
        except:
            await self.disconnect_pc()
            return False


manager = ConnectionManager()


def auth_ws_token(token: str):
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        if payload.get("sub") != ADMIN_USER:
            return None
        return payload.get("sub")
    except JWTError:
        return None


@router.websocket("/pc")
async def pc_websocket(ws: WebSocket, token: str = Query(...)):

    user = auth_ws_token(token)
    if not user:
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
                    # Ignore pong replies from PC
                    if msg.get("type") == "pong":
                        continue
                    await manager.broadcast_event(msg)
                except Exception as e:
                    print("JSON error:", e)

    except WebSocketDisconnect:
        await manager.disconnect_pc()


@router.websocket("/watch")
async def watch_websocket(ws: WebSocket, token: str = Query(...)):

    user = auth_ws_token(token)
    if not user:
        await ws.close(code=4001)
        return

    await manager.connect_client(ws)

    # Send current PC status immediately on connect so badge is accurate
    await ws.send_text(json.dumps({
        "type": "pc_status",
        "online": manager.pc_socket is not None
    }))

    # Send last frame if we have one
    if manager.latest_frame:
        try:
            await ws.send_bytes(manager.latest_frame)
        except:
            pass

    try:
        while True:
            data = await ws.receive_text()
            cmd = json.loads(data)
            await manager.send_command_to_pc(cmd)

    except WebSocketDisconnect:
        manager.disconnect_client(ws)


# ── MJPEG PROXY ───────────────────────────────────────────────────────────────
# Browser calls GET /api/stream/mjpeg?token=...
# Railway serves the latest frame buffer as a continuous MJPEG stream over HTTPS.
# This bypasses the browser's mixed-content block (https page → http localhost).

BOUNDARY = b"--mjpegframe"

async def mjpeg_generator():
    """Yield MJPEG frames from the latest_frame buffer as fast as they arrive."""
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
        await asyncio.sleep(0.033)   # ~30 fps max polling


@router.get("/mjpeg")
async def mjpeg_stream(token: str = Query(...)):
    # Validate token manually (can't use Depends with StreamingResponse easily)
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        if payload.get("sub") != ADMIN_USER:
            return Response(status_code=401)
    except JWTError:
        return Response(status_code=401)

    return StreamingResponse(
        mjpeg_generator(),
        media_type=f"multipart/x-mixed-replace; boundary=mjpegframe",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    )

