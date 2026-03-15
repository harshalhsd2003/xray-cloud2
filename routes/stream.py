import json
import asyncio
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query, Depends
from fastapi.responses import StreamingResponse, Response
from jose import JWTError, jwt
from auth import SECRET_KEY, ALGORITHM, ADMIN_USER, verify_token

router = APIRouter()

PC_PING_INTERVAL = 5


class ConnectionManager:
    def __init__(self):
        self.clients     = []
        self.pc_socket   = None
        self.latest_frame = None
        # Per-camera latest snapshot bytes {cam_idx: bytes}
        self.camera_snapshots: dict[int, bytes] = {}
        self._pc_ping_task = None

    async def connect_client(self, ws: WebSocket):
        await ws.accept()
        self.clients.append(ws)

    def disconnect_client(self, ws: WebSocket):
        if ws in self.clients:
            self.clients.remove(ws)

    async def connect_pc(self, ws: WebSocket):
        await ws.accept()
        if self.pc_socket:
            try:
                await self.pc_socket.close()
            except:
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
        self.pc_socket = None
        self.latest_frame = None
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

    async def broadcast_frame(self, frame_bytes: bytes, cam_idx: int = 0):
        self.latest_frame = frame_bytes
        # Store as camera snapshot for the camera preview grid
        self.camera_snapshots[cam_idx] = frame_bytes

        dead = []
        for ws in self.clients:
            try:
                await ws.send_bytes(frame_bytes)
            except:
                dead.append(ws)
        for ws in dead:
            self.disconnect_client(ws)

    async def broadcast_event(self, event: dict):
        msg = json.dumps(event)
        dead = []
        for ws in self.clients:
            try:
                await ws.send_text(msg)
            except:
                dead.append(ws)
        for ws in dead:
            self.disconnect_client(ws)

    async def send_command_to_pc(self, cmd: dict) -> bool:
        if not self.pc_socket:
            return False
        try:
            await self.pc_socket.send_text(json.dumps(cmd))
            return True
        except:
            await self.disconnect_pc()
            return False

    def get_snapshot(self, cam_idx: int) -> bytes | None:
        """Return latest stored JPEG snapshot for a given camera index."""
        return self.camera_snapshots.get(cam_idx)


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

            # Binary = video frame
            frame = data.get("bytes") or data.get("binary")
            if frame:
                # Try to read camera index from the current settings
                # (PC embeds cam_idx in a preceding text message)
                await manager.broadcast_frame(frame)
                continue

            text = data.get("text")
            if text:
                try:
                    msg = json.loads(text)
                    if msg.get("type") == "pong":
                        continue
                    # If PC sends a frame with cam info, store snapshot
                    if msg.get("type") == "frame_meta":
                        pass  # future use
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

    # Send current PC status immediately
    await ws.send_text(json.dumps({
        "type": "pc_status",
        "online": manager.pc_socket is not None
    }))

    # Send last frame if available
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


# ── SNAPSHOT ENDPOINT — used by camera preview grid ──────────────────────────
@router.get("/snapshot")
async def snapshot(
    cam: int = Query(0, ge=0, le=7),
    token: str = Query(...)
):
    """Return a JPEG still of the latest frame for the given camera index."""
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        if payload.get("sub") != ADMIN_USER:
            return Response(status_code=401)
    except JWTError:
        return Response(status_code=401)

    jpg_bytes = manager.get_snapshot(cam)
    if not jpg_bytes:
        # Return 204 so the browser img onerror fires cleanly
        return Response(status_code=204)

    return Response(
        content=jpg_bytes,
        media_type="image/jpeg",
        headers={"Cache-Control": "no-cache, no-store"}
    )


# ── MJPEG PROXY ───────────────────────────────────────────────────────────────
BOUNDARY = b"--mjpegframe"


async def mjpeg_generator():
    """Yield MJPEG frames from the latest_frame buffer at up to 30 fps."""
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
        await asyncio.sleep(0.033)   # ~30 fps cap


@router.get("/mjpeg")
async def mjpeg_stream(token: str = Query(...)):
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        if payload.get("sub") != ADMIN_USER:
            return Response(status_code=401)
    except JWTError:
        return Response(status_code=401)

    return StreamingResponse(
        mjpeg_generator(),
        media_type="multipart/x-mixed-replace; boundary=mjpegframe",
        headers={
            "Cache-Control":    "no-cache",
            "X-Accel-Buffering": "no"
        }
    )
