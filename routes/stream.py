import json
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Depends, Query
from jose import JWTError, jwt
from auth import verify_token, SECRET_KEY, ALGORITHM, ADMIN_USER

router = APIRouter()


class ConnectionManager:
    def __init__(self):
        # Admin / dashboard clients
        self.clients: list[WebSocket] = []

        # Scanner PC connection
        self.pc_socket: WebSocket | None = None

        # Last received frame
        self.latest_frame: bytes | None = None

    async def connect_client(self, ws: WebSocket):
        await ws.accept()
        self.clients.append(ws)

    def disconnect_client(self, ws: WebSocket):
        if ws in self.clients:
            self.clients.remove(ws)

    async def connect_pc(self, ws: WebSocket):
        await ws.accept()

        # Close old scanner connection if exists
        if self.pc_socket:
            try:
                await self.pc_socket.close()
            except:
                pass

        self.pc_socket = ws
        print("[WS] Scanner PC connected")

    def disconnect_pc(self):
        print("[WS] Scanner PC disconnected")
        self.pc_socket = None
        self.latest_frame = None

    async def broadcast_frame(self, frame_bytes: bytes):
        """Send video frame to all dashboard clients"""
        self.latest_frame = frame_bytes

        dead = []

        for ws in self.clients:
            try:
                await ws.send_bytes(frame_bytes)
            except Exception:
                dead.append(ws)

        for ws in dead:
            self.disconnect_client(ws)

    async def broadcast_event(self, event: dict):
        """Send JSON event to dashboard clients"""
        msg = json.dumps(event)

        dead = []

        for ws in self.clients:
            try:
                await ws.send_text(msg)
            except Exception:
                dead.append(ws)

        for ws in dead:
            self.disconnect_client(ws)

    async def send_command_to_pc(self, command: dict):
        """Send command from dashboard to scanner PC"""

        if not self.pc_socket:
            return False

        try:
            await self.pc_socket.send_text(json.dumps(command))
            return True

        except Exception:
            self.disconnect_pc()
            return False


manager = ConnectionManager()


def auth_ws_token(token: str = Query(...)):
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])

        if payload.get("sub") != ADMIN_USER:
            return None

        return payload.get("sub")

    except JWTError:
        return None


# ─────────────────────────────────────────────
# Scanner PC WebSocket
# ─────────────────────────────────────────────

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

            # Video frame
            if "bytes" in data:
                await manager.broadcast_frame(data["bytes"])

            # Status JSON
            elif "text" in data:
                await manager.broadcast_event(json.loads(data["text"]))

    except WebSocketDisconnect:
        manager.disconnect_pc()


# ─────────────────────────────────────────────
# Admin / Dashboard WebSocket
# ─────────────────────────────────────────────

@router.websocket("/watch")
async def watch_websocket(ws: WebSocket, token: str = Query(...)):

    user = auth_ws_token(token)

    if not user:
        await ws.close(code=4001)
        return

    await manager.connect_client(ws)

    print(f"[WS] Client connected. Total: {len(manager.clients)}")

    # Send last frame immediately
    if manager.latest_frame:
        try:
            await ws.send_bytes(manager.latest_frame)
        except:
            pass

    try:
        while True:

            # Receive command from dashboard
            data = await ws.receive_text()

            cmd = json.loads(data)

            await manager.send_command_to_pc(cmd)

    except WebSocketDisconnect:

        manager.disconnect_client(ws)

        print(f"[WS] Client disconnected. Total: {len(manager.clients)}")


# ─────────────────────────────────────────────
# REST command endpoint
# ─────────────────────────────────────────────

@router.post("/command")
async def send_command(command: dict, _=Depends(verify_token)):

    sent = await manager.send_command_to_pc(command)

    return {"sent": sent}