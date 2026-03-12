import asyncio
import json
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Depends, Query
from auth import verify_token
from jose import JWTError, jwt
from auth import SECRET_KEY, ALGORITHM, ADMIN_USER
import os

router = APIRouter()

class ConnectionManager:
    def __init__(self):
        # Admin/app clients (receive events + stream frames)
        self.clients: list[WebSocket] = []
        # The PC edge device (sends frames + receives commands)
        self.pc_socket: WebSocket | None = None
        self.latest_frame: bytes | None = None

    async def connect_client(self, ws: WebSocket):
        await ws.accept()
        self.clients.append(ws)

    def disconnect_client(self, ws: WebSocket):
        if ws in self.clients:
            self.clients.remove(ws)

    async def connect_pc(self, ws: WebSocket):
        await ws.accept()
        self.pc_socket = ws

    def disconnect_pc(self):
        self.pc_socket = None
        self.latest_frame = None

    async def broadcast_frame(self, frame_bytes: bytes):
        """Relay JPEG frame from PC to all connected app/admin clients."""
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
        """Send JSON event to all connected clients."""
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
        """Send control command from admin to PC."""
        if self.pc_socket:
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

# ── PC connects here to push frames and receive commands ──────────────────────
@router.websocket("/pc")
async def pc_websocket(ws: WebSocket, token: str = Query(...)):
    user = auth_ws_token(token)
    if not user:
        await ws.close(code=4001)
        return

    await manager.connect_pc(ws)
    print("[WS] PC connected")
    try:
        while True:
            # PC sends raw JPEG bytes as video frames
            data = await ws.receive()
            if "bytes" in data:
                await manager.broadcast_frame(data["bytes"])
            elif "text" in data:
                # PC can send JSON status updates
                await manager.broadcast_event(json.loads(data["text"]))
    except WebSocketDisconnect:
        print("[WS] PC disconnected")
        manager.disconnect_pc()

# ── App / Admin browser connects here to receive frames + events ──────────────
@router.websocket("/watch")
async def watch_websocket(ws: WebSocket, token: str = Query(...)):
    user = auth_ws_token(token)
    if not user:
        await ws.close(code=4001)
        return

    await manager.connect_client(ws)
    print(f"[WS] Client connected. Total: {len(manager.clients)}")

    # Send latest frame immediately so app doesn't show blank screen
    if manager.latest_frame:
        await ws.send_bytes(manager.latest_frame)

    try:
        while True:
            # Clients can send commands (settings changes)
            data = await ws.receive_text()
            cmd = json.loads(data)
            await manager.send_command_to_pc(cmd)
    except WebSocketDisconnect:
        manager.disconnect_client(ws)
        print(f"[WS] Client disconnected. Total: {len(manager.clients)}")

# ── REST endpoint to send a command to PC (from admin panel HTTP) ─────────────
@router.post("/command")
async def send_command(command: dict, _=Depends(verify_token)):
    sent = await manager.send_command_to_pc(command)
    return {"sent": sent}
