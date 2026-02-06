#!/usr/bin/env python3
"""
StreamSync Music Streaming Backend
A production-ready music streaming API built with FastAPI, MongoDB, and GridFS
Features: JWT auth, role-based access, file uploads, streaming, caching, WebSockets
"""

import os
import io
import uuid
import hashlib
import asyncio
import mimetypes
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any
from contextlib import asynccontextmanager
import webbrowser
import re

# FastAPI & dependencies
from fastapi import FastAPI, HTTPException, Depends, UploadFile, File, Form, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import BaseModel, EmailStr
from passlib.context import CryptContext
import uvicorn

# Database
import motor.motor_asyncio
from pymongo.errors import DuplicateKeyError
import gridfs
from bson import ObjectId

# Caching & utils
import redis.asyncio as redis
from PIL import Image
import mutagen
import json
import logging
import certifi

try:
    from jose import jwt
except ImportError as e:
    print("\nERROR: python-jose must be installed. Run 'pip install python-jose'.\nError: {}\n".format(e))
    import sys; sys.exit(1)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Settings ---
class Settings:
    MONGODB_URI = os.getenv("MONGODB_URI", "mongodb+srv://streetsofahmedabad2_db_user:mAEtqTMGGmEOziVE@cluster0.9u0xk1w.mongodb.net/")
    DATABASE_NAME = os.getenv("DATABASE_NAME", "streamsync")
    REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
    JWT_SECRET = os.getenv("JWT_SECRET", "your-super-secret-jwt-key-change-in-production")
    JWT_ALGORITHM = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES = 30
    REFRESH_TOKEN_EXPIRE_DAYS = 7
    UPLOAD_DIR = os.getenv("UPLOAD_DIR", "./uploads")
    MAX_FILE_SIZE = 50 * 1024 * 1024
    ALLOWED_AUDIO_TYPES = {".mp3", ".wav", ".flac", ".m4a", ".aac"}
    ALLOWED_IMAGE_TYPES = {".jpg", ".jpeg", ".png", ".webp"}

settings = Settings()
os.makedirs(settings.UPLOAD_DIR, exist_ok=True)
os.makedirs(f"{settings.UPLOAD_DIR}/audio", exist_ok=True)
os.makedirs(f"{settings.UPLOAD_DIR}/images", exist_ok=True)

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
security = HTTPBearer()

db = None
redis_client = None

# --- Models ---
class UserBase(BaseModel):
    name: str
    email: EmailStr

class UserCreate(UserBase):
    password: str

class UserLogin(BaseModel):
    email: EmailStr
    password: str

class User(UserBase):
    id: str
    role: str = "user"
    created_at: datetime
    is_active: bool = True

class Token(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    user: User

class SongBase(BaseModel):
    title: str
    artist: str
    album: Optional[str] = None
    genre: Optional[str] = None
    duration: Optional[int] = None

class Song(SongBase):
    id: str
    file_url: str
    cover_url: Optional[str] = None
    play_count: int = 0
    created_at: datetime
    uploaded_by: str

class SearchResponse(BaseModel):
    songs: List[Song]
    artists: List[Dict[str, Any]]
    albums: List[Dict[str, Any]]

# --- Database Manager ---
class DatabaseManager:
    @staticmethod
    async def init_db():
        global db
        try:
            client = motor.motor_asyncio.AsyncIOMotorClient(
                settings.MONGODB_URI, 
                tlsCAFile=certifi.where(),
                tlsAllowInvalidCertificates=True,  # Added to solve local SSL issues
                serverSelectionTimeoutMS=5000,
                connectTimeoutMS=10000
            )
            db = client[settings.DATABASE_NAME]
            # Verify connection
            await client.admin.command('ping')
            
            await db.users.create_index("email", unique=True)
            await db.songs.create_index("title")
            await db.songs.create_index("artist")
            await db.songs.create_index("genre")
            await db.playlists.create_index("owner_id")
            await db.play_history.create_index([("user_id", 1), ("played_at", -1)])
            logger.info("Database initialized successfully")
        except Exception as e:
            logger.error(f"Database initialization failed: {e}")
            logger.info("Retrying connection without SSL verification...")
            try:
                client = motor.motor_asyncio.AsyncIOMotorClient(
                    settings.MONGODB_URI,
                    tls=False,
                    serverSelectionTimeoutMS=5000
                )
                db = client[settings.DATABASE_NAME]
                await client.admin.command('ping')
                logger.info("Connected to Database (without SSL)")
            except Exception as e2:
                logger.error(f"Fallback connection failed: {e2}")

    @staticmethod
    async def create_user(user_data: UserCreate) -> User:
        hashed_password = pwd_context.hash(user_data.password)
        user_doc = {
            "_id": str(uuid.uuid4()),
            "name": user_data.name,
            "email": user_data.email,
            "password": hashed_password,
            "role": "user",
            "created_at": datetime.utcnow(),
            "is_active": True
        }
        try:
            await db.users.insert_one(user_doc)
            user_doc.pop("password")
            return User(id=user_doc["_id"], **user_doc)
        except DuplicateKeyError:
            raise HTTPException(status_code=400, detail="Email already registered")

    @staticmethod
    async def authenticate_user(email: str, password: str) -> Optional[User]:
        user_doc = await db.users.find_one({"email": email, "is_active": True})
        if not user_doc or not pwd_context.verify(password, user_doc["password"]):
            return None
        user_doc.pop("password")
        return User(id=user_doc["_id"], **user_doc)

    @staticmethod
    async def get_user_by_id(user_id: str) -> Optional[User]:
        user_doc = await db.users.find_one({"_id": user_id, "is_active": True})
        if user_doc:
            user_doc.pop("password", None)
            return User(id=user_doc["_id"], **user_doc)
        return None

# --- Cache Manager ---
class CacheManager:
    @staticmethod
    async def init_redis():
        global redis_client
        try:
            redis_client = redis.from_url(settings.REDIS_URL, decode_responses=True)
            # Try a quick ping to see if it's alive
            await asyncio.wait_for(redis_client.ping(), timeout=1.0)
            logger.info("Redis initialized successfully")
        except Exception as e:
            logger.warning(f"Redis connection failed ({settings.REDIS_URL}): {e}. Caching will be disabled.")
            redis_client = None

    @staticmethod
    async def get(key: str) -> Optional[str]:
        try:
            return await redis_client.get(key)
        except Exception as e:
            logger.error(f"Redis get error: {e}")
            return None

    @staticmethod
    async def set(key: str, value: str, expire: int = 3600):
        try:
            await redis_client.set(key, value, ex=expire)
        except Exception as e:
            logger.error(f"Redis set error: {e}")

    @staticmethod
    async def delete(key: str):
        try:
            await redis_client.delete(key)
        except Exception as e:
            logger.error(f"Redis delete error: {e}")

    @staticmethod
    async def delete_pattern(pattern: str):
        try:
            keys = await redis_client.keys(pattern)
            if keys:
                await redis_client.delete(*keys)
        except Exception as e:
            logger.error(f"Redis delete pattern error: {e}")

# --- File Manager ---
class FileManager:
    @staticmethod
    def get_file_hash(file_content: bytes) -> str:
        return hashlib.md5(file_content).hexdigest()

    @staticmethod
    async def save_file(file: UploadFile, file_type: str) -> tuple[str, str]:
        content = await file.read()
        file_hash = FileManager.get_file_hash(content)
        filename = f"{file_hash}_{uuid.uuid4().hex[:8]}{os.path.splitext(file.filename)[1]}"
        file_path = os.path.join(settings.UPLOAD_DIR, file_type, filename)
        with open(file_path, "wb") as f:
            f.write(content)
        return file_path, filename

    @staticmethod
    def extract_audio_metadata(file_path: str) -> dict:
        try:
            audiofile = mutagen.File(file_path)
            if audiofile is None:
                return {}
            return {
                "duration": int(audiofile.info.length) if audiofile.info else 0,
                "bitrate": getattr(audiofile.info, 'bitrate', 0),
                "sample_rate": getattr(audiofile.info, 'sample_rate', 0)
            }
        except Exception as e:
            logger.error(f"Error extracting metadata: {e}")
            return {}

    @staticmethod
    async def process_cover_image(file_path: str) -> str:
        try:
            with Image.open(file_path) as img:
                img = img.resize((300, 300), Image.Resampling.LANCZOS)
                img = img.convert("RGB")
                optimized_path = file_path.replace(os.path.splitext(file_path)[1], "_opt.jpg")
                img.save(optimized_path, "JPEG", quality=85, optimize=True)
                return optimized_path
        except Exception as e:
            logger.error(f"Error processing image: {e}")
            return file_path

# --- Auth ---
def create_token(data: dict, expires_delta: timedelta) -> str:
    to_encode = data.copy()
    expire = datetime.utcnow() + expires_delta
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)

def verify_token(token: str) -> dict:
    try:
        payload = jwt.decode(token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])
        return payload
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid token")

async def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(security)) -> User:
    payload = verify_token(credentials.credentials)
    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid token")
    user = await DatabaseManager.get_user_by_id(user_id)
    if not user:
        raise HTTPException(status_code=401, detail="User not found")
    return user

def require_admin(current_user: User = Depends(get_current_user)) -> User:
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    return current_user

# --- WebSocket Manager ---
class ConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def send_personal_message(self, message: str, websocket: WebSocket):
        try:
            await websocket.send_text(message)
        except:
            self.disconnect(websocket)

    async def broadcast(self, message: str):
        for connection in self.active_connections[:]:
            try:
                await connection.send_text(message)
            except:
                self.disconnect(connection)

manager = ConnectionManager()

# --- Lifespan ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    await DatabaseManager.init_db()
    await CacheManager.init_redis()
    await seed_demo_data()
    logger.info("Application started successfully")
    yield
    if redis_client:
        await redis_client.close()
    logger.info("Application shutdown complete")

# --- FastAPI App ---
app = FastAPI(
    title="StreamSync Music API",
    description="Production-ready music streaming platform API",
    version="1.0.0",
    lifespan=lifespan
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:8000",
        "http://localhost:3000",
        "http://127.0.0.1:8000",
        "http://127.0.0.1:3000",
        "*"
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Demo Data Seeding ---
async def seed_demo_data():
    try:
        admin_exists = await db.users.find_one({"email": "admin@streamsync.com"})
        if not admin_exists:
            admin_data = UserCreate(name="Admin User", email="admin@streamsync.com", password="admin123")
            admin_user = await DatabaseManager.create_user(admin_data)
            await db.users.update_one({"_id": admin_user.id}, {"$set": {"role": "admin"}})
            logger.info("Admin user created")
        demo_exists = await db.users.find_one({"email": "demo@streamsync.com"})
        if not demo_exists:
            demo_data = UserCreate(name="Demo User", email="demo@streamsync.com", password="demo123")
            await DatabaseManager.create_user(demo_data)
            logger.info("Demo user created")
        categories = [
            {"id": "pop", "name": "Pop", "color": "#ff6b6b", "icon": "star"},
            {"id": "rock", "name": "Rock", "color": "#4ecdc4", "icon": "guitar"},
            {"id": "jazz", "name": "Jazz", "color": "#45b7d1", "icon": "saxophone"},
            {"id": "electronic", "name": "Electronic", "color": "#f9ca24", "icon": "bolt"},
            {"id": "classical", "name": "Classical", "color": "#6c5ce7", "icon": "piano"},
            {"id": "hip-hop", "name": "Hip Hop", "color": "#fd79a8", "icon": "microphone"},
            {"id": "country", "name": "Country", "color": "#fdcb6e", "icon": "hat-cowboy"},
            {"id": "blues", "name": "Blues", "color": "#74b9ff", "icon": "music"}
        ]
        for category in categories:
            await db.categories.update_one({"id": category["id"]}, {"$set": category}, upsert=True)
        logger.info("Demo data seeded successfully")
    except Exception as e:
        logger.error(f"Error seeding demo data: {e}")

# --- API Routes ---
@app.get("/")
async def root():
    return {"message": "StreamSync Music API", "version": "1.0.0", "status": "running"}

@app.get("/health")
async def health_check():
    return {"status": "healthy", "timestamp": datetime.utcnow()}

# Authentication endpoints
@app.post("/api/auth/register", response_model=User)
async def register(user_data: UserCreate):
    return await DatabaseManager.create_user(user_data)

@app.post("/api/auth/login", response_model=Token)
async def login(login_data: UserLogin):
    user = await DatabaseManager.authenticate_user(login_data.email, login_data.password)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid credentials")
    access_token = create_token(
        {"sub": user.id, "type": "access"},
        timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    )
    refresh_token = create_token(
        {"sub": user.id, "type": "refresh"},
        timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS)
    )
    return Token(access_token=access_token, refresh_token=refresh_token, user=user)

@app.get("/api/auth/me", response_model=User)
async def get_current_user_info(current_user: User = Depends(get_current_user)):
    return current_user

@app.post("/api/auth/refresh")
async def refresh_token(token_data: dict):
    refresh_token = token_data.get("refresh_token")
    if not refresh_token:
        raise HTTPException(status_code=400, detail="Refresh token required")
    payload = verify_token(refresh_token)
    if payload.get("type") != "refresh":
        raise HTTPException(status_code=401, detail="Invalid refresh token")
    user = await DatabaseManager.get_user_by_id(payload.get("sub"))
    if not user:
        raise HTTPException(status_code=401, detail="User not found")
    access_token = create_token(
        {"sub": user.id, "type": "access"},
        timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    )
    return {"access_token": access_token, "token_type": "bearer"}

# Song endpoints
@app.get("/api/songs", response_model=List[Song])
async def get_songs(skip: int = 0, limit: int = 50):
    cached_songs = await CacheManager.get(f"songs:{skip}:{limit}")
    if cached_songs:
        return json.loads(cached_songs)
    cursor = db.songs.find().skip(skip).limit(limit).sort("created_at", -1)
    songs = []
    async for song_doc in cursor:
        song_doc["id"] = song_doc.pop("_id")
        songs.append(Song(**song_doc))
    await CacheManager.set(f"songs:{skip}:{limit}", json.dumps([song.dict() for song in songs]), 300)
    return songs

@app.get("/api/songs/{song_id}", response_model=Song)
async def get_song(song_id: str):
    cached_song = await CacheManager.get(f"song:{song_id}")
    if cached_song:
        return Song(**json.loads(cached_song))
    song_doc = await db.songs.find_one({"_id": song_id})
    if not song_doc:
        raise HTTPException(status_code=404, detail="Song not found")
    song_doc["id"] = song_doc.pop("_id")
    song = Song(**song_doc)
    await CacheManager.set(f"song:{song_id}", song.json(), 600)
    return song

@app.post("/api/songs/{song_id}/play")
async def play_song(song_id: str, current_user: User = Depends(get_current_user)):
    await db.songs.update_one(
        {"_id": song_id},
        {"$inc": {"play_count": 1}}
    )
    await db.play_history.insert_one({
        "user_id": current_user.id,
        "song_id": song_id,
        "played_at": datetime.utcnow()
    })
    await CacheManager.delete(f"song:{song_id}")
    await CacheManager.delete_pattern("songs:*")
    await manager.broadcast(f"{{\"type\": \"play\", \"song_id\": \"{song_id}\", \"user\": \"{current_user.name}\"}}")
    return {"message": "Play recorded"}

@app.get("/api/stream/{song_id}")
async def stream_song(song_id: str, request: Request, range: Optional[str] = None):
    song_doc = await db.songs.find_one({"_id": song_id})
    if not song_doc:
        raise HTTPException(status_code=404, detail="Song not found")
    file_path = song_doc.get("file_path")
    if not file_path or not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="Audio file not found")
    file_size = os.path.getsize(file_path)
    start = 0
    end = file_size - 1
    if range:
        range_match = range.replace('bytes=', '').split('-')
        if range_match[0]:
            start = int(range_match[0])
        if range_match[1]:
            end = int(range_match[1])
    content_length = end - start + 1
    def iterfile(file_path: str, start: int, end: int, chunk_size: int = 8192):
        with open(file_path, 'rb') as file:
            file.seek(start)
            remaining = end - start + 1
            while remaining:
                chunk_size = min(chunk_size, remaining)
                chunk = file.read(chunk_size)
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk
    headers = {
        'Content-Range': f'bytes {start}-{end}/{file_size}',
        'Accept-Ranges': 'bytes',
        'Content-Length': str(content_length),
        'Content-Type': 'audio/mpeg'
    }
    return StreamingResponse(
        iterfile(file_path, start, end),
        status_code=206 if range else 200,
        headers=headers
    )

# Search endpoint
@app.get("/api/search", response_model=SearchResponse)
async def search(q: str, limit: int = 20):
    if not q.strip():
        raise HTTPException(status_code=400, detail="Search query required")
    try:
        re.compile(q)
    except re.error:
        raise HTTPException(status_code=400, detail="Invalid search query (bad regex)")
    cache_key = f"search:{hashlib.md5(q.encode()).hexdigest()}:{limit}"
    cached_result = await CacheManager.get(cache_key)
    if cached_result:
        return SearchResponse(**json.loads(cached_result))
    songs_cursor = db.songs.find({
        "$or": [
            {"title": {"$regex": q, "$options": "i"}},
            {"artist": {"$regex": q, "$options": "i"}},
            {"album": {"$regex": q, "$options": "i"}},
            {"genre": {"$regex": q, "$options": "i"}}
        ]
    }).limit(limit)
    songs = []
    async for song_doc in songs_cursor:
        song_doc["id"] = song_doc.pop("_id")
        songs.append(Song(**song_doc))
    artists_pipeline = [
        {"$match": {"artist": {"$regex": q, "$options": "i"}}},
        {"$group": {"_id": "$artist", "song_count": {"$sum": 1}}},
        {"$project": {"name": "$_id", "song_count": 1}},
        {"$limit": limit}
    ]
    artists_cursor = db.songs.aggregate(artists_pipeline)
    artists = []
    async for artist in artists_cursor:
        artists.append({"name": artist["name"], "song_count": artist["song_count"]})
    albums_pipeline = [
        {"$match": {"album": {"$regex": q, "$options": "i"}, "album": {"$ne": None}}},
        {"$group": {"_id": {"album": "$album", "artist": "$artist"}, "song_count": {"$sum": 1}}},
        {"$project": {"album": "$_id.album", "artist": "$_id.artist", "song_count": 1}},
        {"$limit": limit}
    ]
    albums_cursor = db.songs.aggregate(albums_pipeline)
    albums = []
    async for album in albums_cursor:
        albums.append({"album": album["album"], "artist": album["artist"], "song_count": album["song_count"]})
    result = SearchResponse(
        songs=songs,
        artists=artists,
        albums=albums
    )
    await CacheManager.set(cache_key, result.json(), 120)
    return result

# User endpoints
@app.get("/api/user/recent")
async def get_recent_songs(current_user: User = Depends(get_current_user)):
    recent_plays = []
    cursor = db.play_history.find(
        {"user_id": current_user.id}
    ).sort("played_at", -1).limit(20)
    song_ids = []
    async for play in cursor:
        if play["song_id"] not in song_ids:
            song_ids.append(play["song_id"])
    songs = []
    for song_id in song_ids[:6]:
        song_doc = await db.songs.find_one({"_id": song_id})
        if song_doc:
            song_doc["id"] = song_doc.pop("_id")
            songs.append(Song(**song_doc))
    if not songs:
        cursor = db.songs.find().sort("play_count", -1).limit(6)
        async for song_doc in cursor:
            song_doc["id"] = song_doc.pop("_id")
            songs.append(Song(**song_doc))
    return {"songs": songs}

@app.get("/api/user/recommendations")
async def get_recommendations(current_user: User = Depends(get_current_user)):
    cursor = db.songs.find().sort("created_at", -1).limit(6)
    songs = []
    async for song_doc in cursor:
        song_doc["id"] = song_doc.pop("_id")
        songs.append(Song(**song_doc))
    return {"songs": songs}

@app.get("/api/user/library")
async def get_user_library(current_user: User = Depends(get_current_user)):
    return await get_recent_songs(current_user)

# Admin endpoints
@app.get("/api/admin/stats")
async def get_admin_stats(admin_user: User = Depends(require_admin)):
    stats = {}
    stats["users"] = await db.users.count_documents({"is_active": True})
    stats["songs"] = await db.songs.count_documents({})
    stats["playlists"] = await db.playlists.count_documents({})
    artists_cursor = db.songs.aggregate([
        {"$group": {"_id": "$artist"}},
        {"$count": "total"}
    ])
    artists_result = await artists_cursor.to_list(None)
    stats["artists"] = artists_result[0]["total"] if artists_result else 0
    albums_cursor = db.songs.aggregate([
        {"$match": {"album": {"$ne": None}}},
        {"$group": {"_id": "$album"}},
        {"$count": "total"}
    ])
    albums_result = await albums_cursor.to_list(None)
    stats["albums"] = albums_result[0]["total"] if albums_result else 0
    total_plays_cursor = db.songs.aggregate([
        {"$group": {"_id": None, "total": {"$sum": "$play_count"}}}
    ])
    total_plays_result = await total_plays_cursor.to_list(None)
    stats["total_plays"] = total_plays_result[0]["total"] if total_plays_result else 0
    return stats

@app.get("/api/admin/songs")
async def get_admin_songs(
    skip: int = 0,
    limit: int = 50,
    admin_user: User = Depends(require_admin)
):
    cursor = db.songs.find().skip(skip).limit(limit).sort("created_at", -1)
    songs = []
    async for song_doc in cursor:
        song_doc["id"] = song_doc.pop("_id")
        songs.append(Song(**song_doc))
    return {"songs": songs}

@app.get("/api/admin/users")
async def get_admin_users(
    skip: int = 0,
    limit: int = 50,
    admin_user: User = Depends(require_admin)
):
    cursor = db.users.find({"is_active": True}).skip(skip).limit(limit).sort("created_at", -1)
    users = []
    async for user_doc in cursor:
        user_doc.pop("password", None)
        user_doc["id"] = user_doc.pop("_id")
        users.append(User(**user_doc))
    return {"users": users}

@app.post("/api/admin/upload")
async def upload_song(
    title: str = Form(...),
    artist: str = Form(...),
    album: Optional[str] = Form(None),
    genre: Optional[str] = Form(None),
    audio_url: str = Form(...),
    cover_url: Optional[str] = Form(None),
    admin_user: User = Depends(require_admin)
):
    song_id = str(uuid.uuid4())
    song_doc = {
        "_id": song_id,
        "title": title,
        "artist": artist,
        "album": album,
        "genre": genre,
        "duration": 0,
        "file_url": audio_url,
        "file_path": None,
        "cover_url": cover_url,
        "play_count": 0,
        "created_at": datetime.utcnow(),
        "uploaded_by": admin_user.id
    }
    await db.songs.insert_one(song_doc)
    await CacheManager.delete_pattern("songs:*")
    return {"message": "Song added successfully", "song_id": song_id}

@app.put("/api/admin/songs/{song_id}")
async def update_song(
    song_id: str,
    song_data: SongBase,
    admin_user: User = Depends(require_admin)
):
    song_doc = await db.songs.find_one({"_id": song_id})
    if not song_doc:
        raise HTTPException(status_code=404, detail="Song not found")
    update_data = song_data.dict(exclude_unset=True)
    await db.songs.update_one(
        {"_id": song_id},
        {"$set": update_data}
    )
    await CacheManager.delete(f"song:{song_id}")
    await CacheManager.delete_pattern("songs:*")
    return {"message": "Song updated successfully"}

@app.delete("/api/admin/songs/{song_id}")
async def delete_song(song_id: str, admin_user: User = Depends(require_admin)):
    song_doc = await db.songs.find_one({"_id": song_id})
    if not song_doc:
        raise HTTPException(status_code=404, detail="Song not found")
    if song_doc.get("file_path") and os.path.exists(song_doc["file_path"]):
        try:
            os.remove(song_doc["file_path"])
        except OSError as e:
            logger.error(f"Error deleting audio file: {e}")
    if song_doc.get("cover_url"):
        cover_filename = song_doc["cover_url"].split("/")[-1]
        cover_path = os.path.join(settings.UPLOAD_DIR, "images", cover_filename)
        if os.path.exists(cover_path):
            try:
                os.remove(cover_path)
            except OSError as e:
                logger.error(f"Error deleting cover image: {e}")
    await db.songs.delete_one({"_id": song_id})
    await db.playlists.update_many(
        {"songs": song_id},
        {"$pull": {"songs": song_id}, "$inc": {"track_count": -1}}
    )
    await db.play_history.delete_many({"song_id": song_id})
    await CacheManager.delete(f"song:{song_id}")
    await CacheManager.delete_pattern("songs:*")
    return {"message": "Song deleted successfully"}

@app.post("/api/admin/users/{user_id}/toggle")
async def toggle_user_status(
    user_id: str,
    admin_user: User = Depends(require_admin)
):
    if user_id == admin_user.id:
        raise HTTPException(status_code=400, detail="Cannot modify your own account")
    user = await db.users.find_one({"_id": user_id})
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    new_status = not user.get("is_active", True)
    await db.users.update_one(
        {"_id": user_id},
        {"$set": {"is_active": new_status}}
    )
    status_text = "activated" if new_status else "deactivated"
    return {"message": f"User {status_text} successfully"}

# Analytics endpoints
@app.get("/api/admin/analytics/plays")
async def get_play_analytics(
    days: int = 7,
    admin_user: User = Depends(require_admin)
):
    start_date = datetime.utcnow() - timedelta(days=days)
    pipeline = [
        {"$match": {"played_at": {"$gte": start_date}}},
        {"$group": {
            "_id": {"$dateToString": {"format": "%Y-%m-%d", "date": "$played_at"}},
            "count": {"$sum": 1}
        }},
        {"$sort": {"_id": 1}}
    ]
    cursor = db.play_history.aggregate(pipeline)
    daily_plays = []
    async for day_data in cursor:
        daily_plays.append({"date": day_data["_id"], "plays": day_data["count"]})
    return {"daily_plays": daily_plays}

@app.get("/api/admin/analytics/top-songs")
async def get_top_songs(
    limit: int = 10,
    admin_user: User = Depends(require_admin)
):
    cursor = db.songs.find().sort("play_count", -1).limit(limit)
    songs = []
    async for song_doc in cursor:
        song_doc["id"] = song_doc.pop("_id")
        songs.append({
            "title": song_doc["title"],
            "artist": song_doc["artist"],
            "play_count": song_doc["play_count"]
        })
    return {"top_songs": songs}

# Static file serving
@app.get("/api/files/images/{filename}")
async def serve_image(filename: str):
    file_path = os.path.join(settings.UPLOAD_DIR, "images", filename)
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="File not found")
    def iterfile():
        with open(file_path, mode="rb") as file_like:
            yield from file_like
    media_type, _ = mimetypes.guess_type(file_path)
    return StreamingResponse(iterfile(), media_type=media_type or "application/octet-stream")

# WebSocket endpoint
@app.websocket("/ws/{user_id}")
async def websocket_endpoint(websocket: WebSocket, user_id: str):
    await manager.connect(websocket)
    try:
        while True:
            data = await websocket.receive_text()
            await manager.send_personal_message(f"Message received: {data}", websocket)
    except WebSocketDisconnect:
        manager.disconnect(websocket)

# Health check with database
@app.get("/api/health/db")
async def database_health():
    try:
        await db.admin.command('ismaster')
        return {"status": "healthy", "database": "connected"}
    except Exception as e:
        logger.error(f"Database health check failed: {e}")
        raise HTTPException(status_code=503, detail="Database connection failed")

# Batch operations for admin
@app.post("/api/admin/batch/delete-songs")
async def batch_delete_songs(
    song_ids: List[str],
    admin_user: User = Depends(require_admin)
):
    deleted_count = 0
    errors = []
    for song_id in song_ids:
        try:
            song_doc = await db.songs.find_one({"_id": song_id})
            if song_doc:
                if song_doc.get("file_path") and os.path.exists(song_doc["file_path"]):
                    os.remove(song_doc["file_path"])
                await db.songs.delete_one({"_id": song_id})
                await db.playlists.update_many(
                    {"songs": song_id},
                    {"$pull": {"songs": song_id}, "$inc": {"track_count": -1}}
                )
                await db.play_history.delete_many({"song_id": song_id})
                deleted_count += 1
        except Exception as e:
            errors.append(f"Error deleting {song_id}: {str(e)}")
    await CacheManager.delete_pattern("songs:*")
    return {
        "message": f"Batch deletion completed",
        "deleted_count": deleted_count,
        "errors": errors
    }

# Export/Import endpoints for backup
@app.get("/api/admin/export/songs")
async def export_songs(admin_user: User = Depends(require_admin)):
    cursor = db.songs.find()
    songs = []
    async for song_doc in cursor:
        song_doc["_id"] = str(song_doc["_id"])
        songs.append(song_doc)
    return {"songs": songs, "exported_at": datetime.utcnow()}

@app.get("/api/admin/export/users")
async def export_users(admin_user: User = Depends(require_admin)):
    cursor = db.users.find()
    users = []
    async for user_doc in cursor:
        user_doc.pop("password", None)
        user_doc["_id"] = str(user_doc["_id"])
        users.append(user_doc)
    return {"users": users, "exported_at": datetime.utcnow()}

if __name__ == "__main__":
    frontend_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "index.html"))
    webbrowser.open(f"file://{frontend_path}")
    uvicorn.run(
        "backend:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
        log_level="info"
    )