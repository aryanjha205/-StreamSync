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
import random
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import json
import logging
import certifi

# FastAPI & dependencies
from fastapi import FastAPI, HTTPException, Depends, UploadFile, File, Form, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import BaseModel, EmailStr
from passlib.context import CryptContext

# Database
import motor.motor_asyncio
from pymongo.errors import DuplicateKeyError
from bson import ObjectId

# Caching & utils
import redis.asyncio as redis
from jose import jwt

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Settings ---
class Settings:
    MONGODB_URI = os.getenv("MONGODB_URI", "mongodb+srv://streetsofahmedabad2_db_user:mAEtqTMGGmEOziVE@cluster0.9u0xk1w.mongodb.net/streamsync?retryWrites=true&w=majority")
    DATABASE_NAME = os.getenv("DATABASE_NAME", "streamsync")
    REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
    JWT_SECRET = os.getenv("JWT_SECRET", "your-super-secret-jwt-key-change-in-production")
    JWT_ALGORITHM = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES = 30
    REFRESH_TOKEN_EXPIRE_DAYS = 7
    UPLOAD_DIR = os.getenv("UPLOAD_DIR", "/tmp/uploads") # Use /tmp for Vercel
    MAX_FILE_SIZE = 50 * 1024 * 1024
    ALLOWED_AUDIO_TYPES = {".mp3", ".wav", ".flac", ".m4a", ".aac"}
    ALLOWED_IMAGE_TYPES = {".jpg", ".jpeg", ".png", ".webp"}
    # Email Settings
    SMTP_SERVER = "smtp.gmail.com"
    SMTP_PORT = 587
    SMTP_USERNAME = "bharatbyte.com@gmail.com"
    SMTP_PASSWORD = "xbbixxdzmecsvjto" 

settings = Settings()

class EmailManager:
    @staticmethod
    def send_email(to_email: str, subject: str, body: str):
        try:
            msg = MIMEMultipart()
            msg['From'] = settings.SMTP_USERNAME
            msg['To'] = to_email
            msg['Subject'] = subject
            msg.attach(MIMEText(body, 'html'))

            server = smtplib.SMTP(settings.SMTP_SERVER, settings.SMTP_PORT)
            server.starttls()
            server.login(settings.SMTP_USERNAME, settings.SMTP_PASSWORD)
            server.send_message(msg)
            server.quit()
            return True
        except Exception as e:
            logger.error(f"Failed to send email to {to_email}: {e}")
            return False

# Ensure /tmp directories exist for Vercel
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
    user: Optional[User] = None

class OTPSend(BaseModel):
    email: EmailStr

class OTPVerify(BaseModel):
    email: EmailStr
    otp: str
    device_id: str

class AdminLock(BaseModel):
    code: str

class BroadcastRequest(BaseModel):
    subject: str
    message: str

class ThemeUpdate(BaseModel):
    theme: str

class SongBase(BaseModel):
    title: str
    artist: str
    album: Optional[str] = None
    genre: Optional[str] = None
    duration: Optional[int] = None
    file_url: Optional[str] = None
    cover_url: Optional[str] = None

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
        if db is not None:
            return db

        uris_to_try = [settings.MONGODB_URI]
        # In Vercel, we only try the configured URI (usually Atlas)
        
        last_error = None
        for uri in uris_to_try:
            try:
                client_kwargs = {
                    "serverSelectionTimeoutMS": 5000,
                    "connectTimeoutMS": 10000
                }
                if "localhost" not in uri and "127.0.0.1" not in uri:
                    client_kwargs["tlsCAFile"] = certifi.where()
                    client_kwargs["tlsAllowInvalidCertificates"] = True
                    
                client = motor.motor_asyncio.AsyncIOMotorClient(uri, **client_kwargs)
                await client.admin.command('ping')
                db = client[settings.DATABASE_NAME]
                
                # Create indexes
                await db.users.create_index("email", unique=True)
                await db.subscribers.create_index("email", unique=True)
                await db.songs.create_index("title")
                await db.play_history.create_index([("user_id", 1), ("played_at", -1)])
                
                logger.info("Database initialized successfully")
                return db
            except Exception as e:
                logger.error(f"Failed to connect to MongoDB: {e}")
                last_error = e
                continue
        
        raise last_error

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
            if settings.REDIS_URL:
                redis_client = redis.from_url(settings.REDIS_URL, decode_responses=True)
                await asyncio.wait_for(redis_client.ping(), timeout=1.0)
                logger.info("Redis initialized successfully")
        except Exception as e:
            logger.warning(f"Redis connection failed: {e}. Caching will be disabled.")
            redis_client = None

    @staticmethod
    async def get(key: str) -> Optional[str]:
        try:
            if redis_client: return await redis_client.get(key)
        except: return None

    @staticmethod
    async def set(key: str, value: str, expire: int = 3600):
        try:
            if redis_client: await redis_client.set(key, value, ex=expire)
        except: pass

    @staticmethod
    async def delete_pattern(pattern: str):
        try:
            if redis_client:
                keys = await redis_client.keys(pattern)
                if keys: await redis_client.delete(*keys)
        except: pass

# --- Auth Helpers ---
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

async def _get_user_from_token(token: str) -> User:
    payload = verify_token(token)
    user_id = payload.get("sub")
    token_type = payload.get("type")
    
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid token")
        
    if token_type == "subscription":
        return User(
            id=user_id,
            name=user_id.split('@')[0],
            email=user_id,
            role="subscriber",
            created_at=datetime.utcnow(),
            is_active=True
        )
        
    user = await DatabaseManager.get_user_by_id(user_id)
    if not user:
        raise HTTPException(status_code=401, detail="User not found")
    return user

async def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(security)) -> User:
    return await _get_user_from_token(credentials.credentials)

async def get_optional_user(credentials: Optional[HTTPAuthorizationCredentials] = Depends(HTTPBearer(auto_error=False))) -> Optional[User]:
    if not credentials: return None
    try: return await _get_user_from_token(credentials.credentials)
    except: return None

def require_admin(current_user: User = Depends(get_current_user)) -> User:
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    return current_user

# --- App Instance ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    await DatabaseManager.init_db()
    await CacheManager.init_redis()
    await seed_demo_data()
    yield
    if redis_client: await redis_client.close()

app = FastAPI(title="StreamSync API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Seeding ---
async def seed_demo_data():
    try:
        admin_exists = await db.users.find_one({"email": "admin@streamsync.com"})
        if not admin_exists:
            admin_data = UserCreate(name="Admin User", email="admin@streamsync.com", password="admin123")
            admin_user = await DatabaseManager.create_user(admin_data)
            await db.users.update_one({"_id": admin_user.id}, {"$set": {"role": "admin"}})
        logger.info("Seeding complete")
    except Exception as e:
        logger.error(f"Seeding error: {e}")

# --- Routes ---
@app.get("/api/health")
async def health():
    return {"status": "healthy", "timestamp": datetime.utcnow()}

@app.post("/api/auth/register", response_model=User)
async def register(user_data: UserCreate):
    return await DatabaseManager.create_user(user_data)

@app.post("/api/auth/login", response_model=Token)
async def login(login_data: UserLogin):
    user = await DatabaseManager.authenticate_user(login_data.email, login_data.password)
    if not user: raise HTTPException(status_code=401, detail="Invalid credentials")
    access_token = create_token({"sub": user.id, "type": "access"}, timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES))
    refresh_token = create_token({"sub": user.id, "type": "refresh"}, timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS))
    return Token(access_token=access_token, refresh_token=refresh_token, user=user)

otp_store = {}

@app.post("/api/auth/otp/send")
async def send_otp(data: OTPSend):
    otp = str(random.randint(100000, 999999))
    otp_store[data.email] = {"otp": otp, "expires": datetime.utcnow() + timedelta(minutes=10)}
    body = f"Your StreamSync OTP is: {otp}"
    success = EmailManager.send_email(data.email, "StreamSync OTP", body)
    return {"message": "OTP sent", "debug_otp": otp if not success else None}

@app.post("/api/auth/otp/verify")
async def verify_otp(data: OTPVerify):
    if data.email not in otp_store or datetime.utcnow() > otp_store[data.email]["expires"]:
        raise HTTPException(status_code=400, detail="Invalid or expired OTP")
    if data.otp != otp_store[data.email]["otp"]:
        raise HTTPException(status_code=400, detail="Invalid OTP")
    
    await db.subscribers.update_one({"email": data.email}, {"$set": {"email": data.email, "device_id": data.device_id, "status": "active"}}, upsert=True)
    access_token = create_token({"sub": data.email, "type": "subscription"}, timedelta(days=3650))
    return {"access_token": access_token}

@app.post("/api/auth/admin/verify")
async def verify_admin(data: AdminLock):
    if data.code == "70458":
        admin = await db.users.find_one({"role": "admin"})
        access_token = create_token({"sub": admin["_id"], "type": "access", "role": "admin"}, timedelta(hours=24))
        return {"access_token": access_token}
    raise HTTPException(status_code=401, detail="Invalid code")

@app.get("/api/songs", response_model=List[Song])
async def get_songs(skip: int = 0, limit: int = 50):
    cursor = db.songs.find().skip(skip).limit(limit).sort("created_at", -1)
    songs = []
    async for s in cursor:
        s["id"] = s.pop("_id")
        songs.append(Song(**s))
    return songs

@app.get("/api/songs/{song_id}", response_model=Song)
async def get_song(song_id: str):
    s = await db.songs.find_one({"_id": song_id})
    if not s: raise HTTPException(status_code=404, detail="Not found")
    s["id"] = s.pop("_id")
    return Song(**s)

@app.get("/api/search", response_model=SearchResponse)
async def search(q: str, limit: int = 20):
    songs_cursor = db.songs.find({"$or": [{"title": {"$regex": q, "$options": "i"}}, {"artist": {"$regex": q, "$options": "i"}}]}).limit(limit)
    songs = []
    async for s in songs_cursor:
        s["id"] = s.pop("_id")
        songs.append(Song(**s))
    return SearchResponse(songs=songs, artists=[], albums=[])

@app.get("/api/user/recent")
async def get_recent(current_user: Optional[User] = Depends(get_optional_user)):
    cursor = db.songs.find().sort("play_count", -1).limit(6)
    songs = []
    async for s in cursor:
        s["id"] = s.pop("_id")
        songs.append(Song(**s))
    return {"songs": songs}

@app.get("/api/user/recommendations")
async def get_recs(current_user: Optional[User] = Depends(get_optional_user)):
    cursor = db.songs.find().sort("created_at", -1).limit(6)
    songs = []
    async for s in cursor:
        s["id"] = s.pop("_id")
        songs.append(Song(**s))
    return {"songs": songs}

@app.get("/api/settings/theme")
async def get_theme():
    settings_doc = await db.settings.find_one({"id": "global"})
    return {"theme": settings_doc.get("theme", "default") if settings_doc else "default"}

@app.post("/api/admin/theme")
async def update_theme(data: ThemeUpdate, admin = Depends(require_admin)):
    await db.settings.update_one({"id": "global"}, {"$set": {"theme": data.theme}}, upsert=True)
    return {"message": "Updated"}

@app.get("/api/admin/stats")
async def get_stats(admin = Depends(require_admin)):
    return {
        "users": await db.users.count_documents({}),
        "songs": await db.songs.count_documents({}),
        "artists": 0, "albums": 0, "total_plays": 0
    }

@app.get("/api/admin/songs")
async def admin_songs(admin = Depends(require_admin)):
    return {"songs": await get_songs()}

@app.post("/api/admin/upload")
async def admin_upload(
    title: str = Form(...), artist: str = Form(...), 
    audio_url: str = Form(...), cover_url: str = Form(None),
    admin = Depends(require_admin)
):
    song_id = str(uuid.uuid4())
    await db.songs.insert_one({
        "_id": song_id, "title": title, "artist": artist, 
        "file_url": audio_url, "cover_url": cover_url,
        "play_count": 0, "created_at": datetime.utcnow(), "uploaded_by": admin.id
    })
    return {"song_id": song_id}

@app.delete("/api/admin/songs/{song_id}")
async def admin_delete(song_id: str, admin = Depends(require_admin)):
    await db.songs.delete_one({"_id": song_id})
    return {"message": "Deleted"}
