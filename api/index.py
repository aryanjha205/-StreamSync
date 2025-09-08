import os
import io
import uuid
import hashlib
import asyncio
import mimetypes
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any
from contextlib import asynccontextmanager
import re

from fastapi import FastAPI, HTTPException, Depends, UploadFile, File, Form, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import BaseModel, EmailStr
from passlib.context import CryptContext

import motor.motor_asyncio
from pymongo.errors import DuplicateKeyError
from bson import ObjectId

import redis.asyncio as redis
from PIL import Image
import mutagen
import json
import logging

try:
    from jose import jwt
except ImportError as e:
    import sys
    print("\nERROR: python-jose must be installed. Run 'pip install python-jose'.\nError: {}\n".format(e))
    sys.exit(1)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class Settings:
    MONGODB_URI = os.getenv("MONGODB_URI", "mongodb+srv://streetsofahmedabad1:E3UGzPvMTzGNAmkv@project.gqnzqur.mongodb.net/")
    DATABASE_NAME = os.getenv("DATABASE_NAME", "streamsync")
    REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
    JWT_SECRET = os.getenv("JWT_SECRET", "your-super-secret-jwt-key-change-in-production")
    JWT_ALGORITHM = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES = 30
    REFRESH_TOKEN_EXPIRE_DAYS = 7
    UPLOAD_DIR = os.getenv("UPLOAD_DIR", "/tmp/uploads")
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

class DatabaseManager:
    @staticmethod
    async def init_db():
        global db
        client = motor.motor_asyncio.AsyncIOMotorClient(settings.MONGODB_URI)
        db = client[settings.DATABASE_NAME]
        await db.users.create_index("email", unique=True)
        await db.songs.create_index("title")
        await db.songs.create_index("artist")
        await db.songs.create_index("genre")
        await db.playlists.create_index("owner_id")
        await db.play_history.create_index([("user_id", 1), ("played_at", -1)])
        logger.info("Database initialized successfully")

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

class CacheManager:
    @staticmethod
    async def init_redis():
        global redis_client
        redis_client = redis.from_url(settings.REDIS_URL, decode_responses=True)
        logger.info("Redis initialized successfully")

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

app = FastAPI(
    title="StreamSync Music API",
    description="Production-ready music streaming platform API",
    version="1.0.0",
    lifespan=lifespan
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

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
