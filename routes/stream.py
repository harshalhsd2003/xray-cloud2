import json
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query
from jose import JWTError, jwt
from auth import SECRET_KEY, ALGORITHM, ADMIN_USER

router = APIRouter()


class ConnectionManager:
    def __init__(self):
        self.clients = []
        self.pc_socket = None
        self.latest_frame = None

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

    def disconnect_pc(self):
        print("[WS] Scanner PC disconnected")
        self.pc_socket = None
        self.latest_frame = None

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

    async def send_command_to_pc(self, cmd):
        if not self.pc_socket:
            return False

        try:
            await self.pc_socket.send_text(json.dumps(cmd))
            return True
        except:
            self.disconnect_pc()
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
                print("Frame received:", len(frame))
                await manager.broadcast_frame(frame)
                continue

            text = data.get("text")

            if text:
                try:
                    msg = json.loads(text)
                    await manager.broadcast_event(msg)
                except Exception as e:
                    print("JSON error:", e)

    except WebSocketDisconnect:
        manager.disconnect_pc()


@router.websocket("/watch")
async def watch_websocket(ws: WebSocket, token: str = Query(...)):

    user = auth_ws_token(token)

    if not user:
        await ws.close(code=4001)
        return

    await manager.connect_client(ws)

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