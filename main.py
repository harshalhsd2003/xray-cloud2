import os
import asyncio
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse
from contextlib import asynccontextmanager
from database import init_db
from routes.detections import router as detections_router
from routes.settings import router as settings_router
from routes.admin import router as admin_router
from routes.stream import router as stream_router, manager

@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    yield

app = FastAPI(title="XRay Scanner Cloud API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(admin_router,      prefix="/api/admin",      tags=["Admin"])
app.include_router(detections_router, prefix="/api/detections", tags=["Detections"])
app.include_router(settings_router,   prefix="/api/settings",   tags=["Settings"])
app.include_router(stream_router,     prefix="/api/stream",     tags=["Stream"])

# Serve admin web panel (static HTML)
os.makedirs("static", exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/", response_class=HTMLResponse)
async def root():
    with open("static/admin.html", "r") as f:
        return f.read()

@app.get("/health")
async def health():
    return {"status": "ok"}
