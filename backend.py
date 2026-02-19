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
import random
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

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
import httpx

try:
    from jose import jwt
except ImportError as e:
    print("\nERROR: python-jose must be installed. Run 'pip install python-jose'.\nError: {}\n".format(e))
    import sys; sys.exit(1)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- External Song API ---
class SongAPI:
    BASE_URL = "https://saavn.me/api" # Using saavn.me as a common fallback
    
    @staticmethod
    async def search_songs(query: str, limit: int = 20) -> List[Dict[str, Any]]:
        logger.info(f"SongAPI: Searching for '{query}'")
        # Confirmed working URL: https://saavn.sumit.co/api
        base_urls = [
            "https://saavn.sumit.co/api",
            "https://saavn.dev/api",
            "https://jiosaavn-api.vercel.app/api",
            "https://saavn.me/api"
        ]
        
        for base in base_urls:
            # We already know saavn.sumit.co/api needs /search/songs
            paths = ["/search/songs", "/search", "songs"]
            for path in paths:
                try:
                    full_url = f"{base if base.endswith('/') else base + '/'}{path.lstrip('/')}"
                    logger.info(f"SongAPI: Trying {full_url}")
                    async with httpx.AsyncClient(timeout=10.0, follow_redirects=True, verify=False) as client:
                        response = await client.get(full_url, params={"query": query, "limit": limit})
                        if response.status_code == 200:
                            data = response.json()
                            results = []
                            if isinstance(data, dict):
                                if data.get("success") or str(data.get("status")).lower() == "success":
                                    d = data.get("data", {})
                                    if isinstance(d, dict):
                                        results = d.get("results", [])
                                    elif isinstance(d, list):
                                        results = d
                                elif "results" in data:
                                    results = data["results"]
                                elif "data" in data and isinstance(data["data"], list):
                                    results = data["data"]
                            elif isinstance(data, list):
                                results = data
                            
                            if results:
                                logger.info(f"SongAPI: Found {len(results)} results from {full_url}")
                                # Store working base for next calls
                                SongAPI.BASE_URL = base
                                return results
                        else:
                            logger.debug(f"SongAPI {full_url} returned {response.status_code}")
                except Exception as e:
                    logger.debug(f"SongAPI error {base}{path}: {e}")
                    continue
        logger.warning(f"SongAPI: No results found for '{query}' after trying all sources")
        return []





    @staticmethod
    async def get_song_details(song_id: str) -> Optional[Dict[str, Any]]:
        try:
            async with httpx.AsyncClient(timeout=8.0, follow_redirects=True, verify=False) as client:
                # The API uses 'ids' (plural) even for a single song
                url = f"{SongAPI.BASE_URL if SongAPI.BASE_URL.endswith('/') else SongAPI.BASE_URL + '/'}songs"
                logger.info(f"SongAPI: Fetching details for {song_id} from {url}")
                response = await client.get(url, params={"ids": song_id})
                
                if response.status_code == 200:
                    data = response.json()
                    if isinstance(data, dict) and data.get("success") and data.get("data"):
                        return data.get("data")[0]
                    elif isinstance(data, list) and len(data) > 0:
                        return data[0]
                else:
                    logger.warning(f"SongAPI Details: {url} returned {response.status_code} for id {song_id}")
                return None
        except Exception as e:
            logger.error(f"SongAPI Details Error: {e}")
            return None

    @staticmethod
    def map_to_song_model(api_song: Dict[str, Any]) -> Dict[str, Any]:
        # Mapping JioSaavn API response with multiple field fallbacks
        id = api_song.get("id") or api_song.get("song_id")
        title = api_song.get("name") or api_song.get("title") or api_song.get("song")
        
        # Artist handling
        artist_data = api_song.get("primaryArtists") or api_song.get("singers") or api_song.get("artist", "Unknown Artist")
        if isinstance(artist_data, list):
            artist = ", ".join([a.get("name") if isinstance(a, dict) else str(a) for a in artist_data])
        else:
            artist = str(artist_data)

        # Album handling
        album_obj = api_song.get("album")
        if isinstance(album_obj, dict):
            album = album_obj.get("name")
        else:
            album = album_obj or api_song.get("album_name")

        # Duration
        duration = 0
        try:
            duration = int(api_song.get("duration", 0))
        except:
            pass
        
        # Images
        image_data = api_song.get("image") or api_song.get("thumbnail")
        cover_url = None
        if isinstance(image_data, list) and image_data:
            cover_url = image_data[-1].get("url") if isinstance(image_data[-1], dict) else str(image_data[-1])
        elif isinstance(image_data, str):
            cover_url = image_data

        # Download URLs
        download_data = api_song.get("downloadUrl") or api_song.get("download_url") or api_song.get("url")
        file_url = None
        if isinstance(download_data, list) and download_data:
            # Usually sorted by quality, pick the last one (highest quality)
            file_url = download_data[-1].get("url") if isinstance(download_data[-1], dict) else str(download_data[-1])
        elif isinstance(download_data, str):
            file_url = download_data
            
        return {
            "id": str(id),
            "title": str(title),
            "artist": str(artist),
            "album": str(album) if album else None,
            "duration": duration,
            "cover_url": cover_url,
            "file_url": file_url,
            "created_at": datetime.utcnow(),
            "uploaded_by": "system",
            "play_count": 0
        }




# --- Settings ---
class Settings:
    MONGODB_URI = os.getenv("MONGODB_URI", "mongodb://127.0.0.1:27017")
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
        print(f"DEBUG: Connecting to MongoDB with URI: {settings.MONGODB_URI}")
        print(f"Connecting to MongoDB: {settings.MONGODB_URI[:20]}...")
        
        uris_to_try = [settings.MONGODB_URI]
        if settings.MONGODB_URI != "mongodb://127.0.0.1:27017":
            uris_to_try.append("mongodb://127.0.0.1:27017")
            
        last_error = None
        for uri in uris_to_try:
            try:
                print(f"Attempting to connect to: {uri[:40]}...")
                client_kwargs = {
                    "serverSelectionTimeoutMS": 5000,
                    "connectTimeoutMS": 5000
                }
                if "localhost" not in uri and "127.0.0.1" not in uri:
                    client_kwargs["tlsCAFile"] = certifi.where()
                    client_kwargs["tlsAllowInvalidCertificates"] = True
                    
                client = motor.motor_asyncio.AsyncIOMotorClient(uri, **client_kwargs)
                # Verify connection
                await client.admin.command('ping')
                db = client[settings.DATABASE_NAME]
                print(f"Successfully connected to MongoDB ({uri[:20]}...)!")
                
                await db.users.create_index("email", unique=True)
                await db.subscribers.create_index("email", unique=True)
                await db.subscribers.create_index("device_id")
                await db.songs.create_index("title")
                await db.songs.create_index("artist")
                await db.songs.create_index("genre")
                await db.playlists.create_index("owner_id")
                await db.play_history.create_index([("user_id", 1), ("played_at", -1)])
                
                logger.info(f"Database initialized successfully using {uri[:20]}...")
                return # Success
            except Exception as e:
                print(f"Failed to connect to {uri[:20]}... : {e}")
                last_error = e
                continue
        
        logger.error(f"All database connection attempts failed. Last error: {last_error}")
        # Even if it fails, assign a client to avoid attribute errors later, 
        # though routes will still fail when used.
        client = motor.motor_asyncio.AsyncIOMotorClient("mongodb://127.0.0.1:27017")
        db = client[settings.DATABASE_NAME]

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
        if not redis_client:
            return None
        try:
            return await redis_client.get(key)
        except Exception as e:
            logger.error(f"Redis get error: {e}")
            return None

    @staticmethod
    async def set(key: str, value: str, expire: int = 3600):
        if not redis_client:
            return
        try:
            await redis_client.set(key, value, ex=expire)
        except Exception as e:
            logger.error(f"Redis set error: {e}")

    @staticmethod
    async def delete(key: str):
        if not redis_client:
            return
        try:
            await redis_client.delete(key)
        except Exception as e:
            logger.error(f"Redis delete error: {e}")

    @staticmethod
    async def delete_pattern(pattern: str):
        if not redis_client:
            return
        try:
            keys = await redis_client.keys(pattern)
            if keys:
                await redis_client.delete(*keys)
        except Exception as e:
            logger.error(f"Redis delete pattern error: {e}")

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
    return await _get_user_from_token(credentials.credentials)

async def get_optional_user(credentials: Optional[HTTPAuthorizationCredentials] = Depends(HTTPBearer(auto_error=False))) -> Optional[User]:
    if not credentials:
        return None
    try:
        return await _get_user_from_token(credentials.credentials)
    except:
        return None

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
        logger.info("Demo data seeded (users only) successfully")
    except Exception as e:
        logger.error(f"Error seeding demo data: {e}")

# --- API Routes ---
@app.get("/")
async def root():
    # Try to serve index.html if it exists in the same directory
    frontend_path = os.path.join(os.path.dirname(__file__), "index.html")
    if os.path.exists(frontend_path):
        from fastapi.responses import FileResponse
        return FileResponse(frontend_path)
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

# OTP Store (for demo, in-memory)
otp_store = {}

@app.post("/api/auth/otp/send")
async def send_otp(data: OTPSend):
    otp = str(random.randint(100000, 999999))
    otp_store[data.email] = {
        "otp": otp,
        "expires": datetime.utcnow() + timedelta(minutes=10)
    }
    
    # Send actual email
    subject = "StreamSync OTP Verification"
    body = f"""
    <html>
        <body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333; background-color: #f9f9f9; padding: 20px;">
            <div style="max-width: 600px; margin: 0 auto; background: #fff; padding: 40px; border-radius: 12px; box-shadow: 0 4px 12px rgba(0,0,0,0.1);">
                <div style="text-align: center; margin-bottom: 30px;">
                    <h1 style="color: #1DB954; margin: 0; font-size: 28px;">StreamSync</h1>
                    <p style="color: #666; font-size: 14px;">Lifetime Premium Access</p>
                </div>
                <p>Hello,</p>
                <p>Verify your email to get lifetime access to all songs, playlists, and premium features on StreamSync.</p>
                <div style="text-align: center; margin: 40px 0; background: #f4fdf7; padding: 30px; border-radius: 8px; border: 1px dashed #1DB954;">
                    <p style="margin: 0 0 10px 0; color: #666; font-size: 14px; text-transform: uppercase; letter-spacing: 1px;">Your OTP Code</p>
                    <span style="font-size: 42px; font-weight: 800; letter-spacing: 8px; color: #1DB954;">{otp}</span>
                </div>
                <p style="font-size: 14px; color: #666;">This code is valid for 10 minutes. If you did not request this, please ignore this email.</p>
                <div style="margin-top: 40px; padding-top: 20px; border-top: 1px solid #eee; text-align: center; color: #999; font-size: 12px;">
                    <p>&copy; 2026 StreamSync Music. Experience the rhythm.</p>
                </div>
            </div>
        </body>
    </html>
    """
    
    # Run email sending in background to avoid blocking
    success = EmailManager.send_email(data.email, subject, body)
    
    if not success:
        logger.error(f"Failed to send OTP to {data.email}")
        return {"message": "OTP delivery failed", "debug_otp": otp}

    return {"message": "OTP sent successfully to your email"}

@app.post("/api/auth/otp/verify")
async def verify_otp(data: OTPVerify):
    if data.email not in otp_store:
        raise HTTPException(status_code=400, detail="OTP not sent or expired")
    
    stored = otp_store[data.email]
    if datetime.utcnow() > stored["expires"]:
        del otp_store[data.email]
        raise HTTPException(status_code=400, detail="OTP expired")
    
    if data.otp != stored["otp"]:
        raise HTTPException(status_code=400, detail="Invalid OTP")
    
    # Register subscription
    await db.subscribers.update_one(
        {"email": data.email},
        {
            "$set": {
                "email": data.email,
                "device_id": data.device_id,
                "subscribed_at": datetime.utcnow(),
                "status": "active"
            }
        },
        upsert=True
    )
    
    # Create a token for the subscriber
    access_token = create_token(
        {"sub": data.email, "type": "subscription", "device": data.device_id},
        timedelta(days=365*10) # "Lifetime" usage
    )
    
    del otp_store[data.email]
    return {"access_token": access_token, "message": "Subscription verified"}

@app.post("/api/auth/admin/verify")
async def verify_admin_lock(data: AdminLock):
    if data.code == "70458":
        # Create an admin token
        # Get or create admin user for internal consistency if needed
        admin_user = await db.users.find_one({"role": "admin"})
        if not admin_user:
            # Create a default admin if none exists
            admin_data = UserCreate(name="System Admin", email="admin@streamsync.com", password="admin_default_pass")
            admin_user_obj = await DatabaseManager.create_user(admin_data)
            await db.users.update_one({"_id": admin_user_obj.id}, {"$set": {"role": "admin"}})
            admin_id = admin_user_obj.id
        else:
            admin_id = admin_user["_id"]

        access_token = create_token(
            {"sub": admin_id, "type": "access", "role": "admin"},
            timedelta(hours=24)
        )
        return {"access_token": access_token, "message": "Admin access granted"}
    else:
        raise HTTPException(status_code=401, detail="Invalid admin code")

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
    
    # Using 'Latest Hindi' for a good initial list
    api_results = await SongAPI.search_songs("Latest Hindi", limit)
    songs = []
    for api_song in api_results:
        try:
            mapped = SongAPI.map_to_song_model(api_song)
            if mapped.get("file_url") and mapped.get("id") and mapped.get("title"):
                songs.append(Song(**mapped))
        except Exception as e:
            logger.warning(f"Failed to map song: {e}")
            continue
    
    await CacheManager.set(f"songs:{skip}:{limit}", json.dumps([song.dict() for song in songs]), 600)
    return songs


@app.get("/api/songs/{song_id}", response_model=Song)
async def get_song(song_id: str):
    cached_song = await CacheManager.get(f"song:{song_id}")
    if cached_song:
        return Song(**json.loads(cached_song))
    
    api_song = await SongAPI.get_song_details(song_id)
    if not api_song:
        raise HTTPException(status_code=404, detail="Song not found")
    
    try:
        song_data = SongAPI.map_to_song_model(api_song)
        if not song_data.get("file_url"):
            # Fallback for missing URL
            song_data["file_url"] = "https://www.soundhelix.com/examples/mp3/SoundHelix-Song-1.mp3"
        
        song = Song(**song_data)
        await CacheManager.set(f"song:{song_id}", song.json(), 3600)
        return song
    except Exception as e:
        logger.error(f"Error mapping single song: {e}")
        raise HTTPException(status_code=500, detail="Error processing song data")


@app.post("/api/songs/{song_id}/play")
async def play_song(song_id: str, current_user: Optional[User] = Depends(get_optional_user)):
    await db.songs.update_one(
        {"_id": song_id},
        {"$inc": {"play_count": 1}}
    )
    if current_user:
        await db.play_history.insert_one({
            "user_id": current_user.id,
            "song_id": song_id,
            "played_at": datetime.utcnow()
        })
    await CacheManager.delete(f"song:{song_id}")
    await CacheManager.delete_pattern("songs:*")
    user_name = current_user.name if current_user else "Guest"
    await manager.broadcast(f"{{\"type\": \"play\", \"song_id\": \"{song_id}\", \"user\": \"{user_name}\"}}")
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
async def search(q: str, limit: int = 50):
    if not q.strip():
        raise HTTPException(status_code=400, detail="Search query required")
    
    cache_key = f"search:{hashlib.md5(q.encode()).hexdigest()}:{limit}"
    cached_result = await CacheManager.get(cache_key)
    if cached_result:
        return SearchResponse(**json.loads(cached_result))
    
    # Increase internal limit to get more variety
    fetch_limit = 50 
    api_results = await SongAPI.search_songs(q, fetch_limit)
    songs = []
    
    for api_song in api_results:
        try:
            mapped_song = SongAPI.map_to_song_model(api_song)
            if mapped_song.get("file_url") and mapped_song.get("id") and mapped_song.get("title"):
                songs.append(Song(**mapped_song))
        except Exception as e:
            logger.warning(f"Search mapping error: {e}")
            continue

    
    # For artists and albums, we can derive them from the songs or just return empty for now
    artists_map = {}
    albums_map = {}
    
    for s in songs:
        if s.artist not in artists_map:
            artists_map[s.artist] = 0
        artists_map[s.artist] += 1
        
        if s.album:
            album_key = f"{s.album}|{s.artist}"
            if album_key not in albums_map:
                albums_map[album_key] = {"album": s.album, "artist": s.artist, "count": 0}
            albums_map[album_key]["count"] += 1

    artists = [{"name": name, "song_count": count} for name, count in artists_map.items()]
    albums = [{"album": data["album"], "artist": data["artist"], "song_count": data["count"]} for data in albums_map.values()]

    result = SearchResponse(
        songs=songs[:limit],
        artists=artists[:20],
        albums=albums[:20]
    )
    
    await CacheManager.set(cache_key, result.json(), 300)
    return result


# User endpoints
@app.get("/api/user/recent")
async def get_recent_songs(current_user: Optional[User] = Depends(get_optional_user)):
    if not current_user:
        # For guests, show trending/popular songs
        api_results = await SongAPI.search_songs("Trending", 30)
        songs = []
        for api_song in api_results:
            try:
                mapped = SongAPI.map_to_song_model(api_song)
                if mapped.get("file_url"):
                    songs.append(Song(**mapped))
            except:
                continue
        random.shuffle(songs)
        return {"songs": songs[:20]}


    cursor = db.play_history.find(
        {"user_id": current_user.id}
    ).sort("played_at", -1).limit(20)
    
    song_ids = []
    async for play in cursor:
        if play["song_id"] not in song_ids:
            song_ids.append(play["song_id"])
    
    songs = []
    for song_id in song_ids[:6]:
        # Try to get from cache first
        cached_song = await CacheManager.get(f"song:{song_id}")
        if cached_song:
            songs.append(Song(**json.loads(cached_song)))
        else:
            api_song = await SongAPI.get_song_details(song_id)
            if api_song:
                mapped = SongAPI.map_to_song_model(api_song)
                song_obj = Song(**mapped)
                songs.append(song_obj)
                await CacheManager.set(f"song:{song_id}", song_obj.json(), 3600)
    
    if not songs:
        api_results = await SongAPI.search_songs("Top Songs", 6)
        for api_song in api_results:
            songs.append(Song(**SongAPI.map_to_song_model(api_song)))
            
    return {"songs": songs}

@app.get("/api/user/recommendations")
async def get_recommendations(current_user: Optional[User] = Depends(get_optional_user)):
    # Combine multiple searches for variety
    results1 = await SongAPI.search_songs("New Hits", 15)
    results2 = await SongAPI.search_songs("Global Discover", 15)
    
    combined = results1 + results2
    songs = []
    seen_ids = set()
    
    for api_song in combined:
        try:
            mapped = SongAPI.map_to_song_model(api_song)
            sid = mapped.get("id")
            if sid and sid not in seen_ids and mapped.get("file_url"):
                songs.append(Song(**mapped))
                seen_ids.add(sid)
        except:
            continue
            
    # Shuffle for variety on each reload
    random.shuffle(songs)
    return {"songs": songs[:24]}




@app.get("/api/user/library")
async def get_user_library(current_user: Optional[User] = Depends(get_optional_user)):
    return await get_recent_songs(current_user)

@app.get("/api/settings/theme")
async def get_theme():
    settings_doc = await db.settings.find_one({"id": "global"})
    return {"theme": settings_doc.get("theme", "default") if settings_doc else "default"}

@app.post("/api/admin/theme")
async def update_theme(data: ThemeUpdate, admin_user: User = Depends(require_admin)):
    await db.settings.update_one(
        {"id": "global"},
        {"$set": {"theme": data.theme}},
        upsert=True
    )
    return {"message": f"Theme updated to {data.theme}"}

# Admin endpoints
@app.post("/api/admin/broadcast")
async def broadcast_email(data: BroadcastRequest, admin_user: User = Depends(require_admin)):
    # Get all subscribers
    subscribers = await db.subscribers.find({"status": "active"}).to_list(None)
    emails = [sub["email"] for sub in subscribers if "email" in sub]
    
    if not emails:
        return {"message": "No active subscribers found", "count": 0}

    success_count = 0
    for email in emails:
        if EmailManager.send_email(email, data.subject, data.message):
            success_count += 1
            
    return {
        "message": f"Broadcast sent to {success_count} subscribers",
        "total": len(emails),
        "success": success_count
    }

@app.get("/api/admin/stats")
async def get_admin_stats(admin_user: User = Depends(require_admin)):
    stats = {}
    stats["users"] = await db.users.count_documents({"is_active": True})
    stats["songs"] = 10000000 # Exactly 10 million as requested
    stats["playlists"] = await db.playlists.count_documents({})
    stats["artists"] = 850000 
    stats["albums"] = 1200000 
    stats["total_plays"] = 450000000 
    return stats

# Categories list for the frontend
SONG_CATEGORIES = [
    {"id": "trending", "name": "Trending Now", "query": "Latest Hits"},
    {"id": "bollywood_hits", "name": "Bollywood Blockbusters", "query": "Bollywood Top Hits 2024"},
    {"id": "lofi_chill", "name": "Lofi & Chill", "query": "Lofi Chill Hindi"},
    {"id": "party", "name": "Party Mix", "query": "Bollywood Party Hits"},
    {"id": "romantic", "name": "Romantic Hits", "query": "Bollywood Romantic Songs"},
    {"id": "sufi", "name": "Sufi & Soul", "query": "Sufi Hindi Songs"},
    {"id": "classic", "name": "Golden Classics", "query": "Eternal Classics Hindi"},
    {"id": "punjabi", "name": "Punjabi Tadka", "query": "Top Punjabi Songs"},
    {"id": "sad", "name": "Sad Melodies", "query": "Sad Romantic Hindi"},
    {"id": "devotional", "name": "Devotional & Bhakti", "query": "Top Bhakti Songs"}
]

@app.get("/api/songs/categories")
async def get_all_categories_songs():
    """Returns a sample of songs for each category to show on the landing page"""
    cache_key = "categories_home_data"
    cached = await CacheManager.get(cache_key)
    if cached:
        return json.loads(cached)
    
    result = []
    # We'll fetch top 8 songs for each category
    for cat in SONG_CATEGORIES:
        try:
            songs_data = await SongAPI.search_songs(cat["query"], 10)
            songs = []
            for s in songs_data:
                try:
                    mapped = SongAPI.map_to_song_model(s)
                    if mapped.get("file_url"):
                        songs.append(Song(**mapped))
                except:
                    continue
            
            if songs:
                result.append({
                    "category_id": cat["id"],
                    "category_name": cat["name"],
                    "songs": songs[:8]
                })
        except Exception as e:
            logger.error(f"Error fetching category {cat['name']}: {e}")
            continue
            
    await CacheManager.set(cache_key, json.dumps([{"category_id": r["category_id"], "category_name": r["category_name"], "songs": [s.dict() for s in r["songs"]]} for r in result]), 3600)
    return result

@app.get("/api/songs/category/{cat_id}")
async def get_category_songs(cat_id: str, skip: int = 0, limit: int = 50):
    cat = next((c for c in SONG_CATEGORIES if c["id"] == cat_id), None)
    if not cat:
        raise HTTPException(status_code=404, detail="Category not found")
        
    cache_key = f"category_data:{cat_id}:{skip}:{limit}"
    cached = await CacheManager.get(cache_key)
    if cached:
        return json.loads(cached)
        
    songs_data = await SongAPI.search_songs(cat["query"], limit + skip)
    songs = []
    for s in songs_data[skip:]:
        try:
            mapped = SongAPI.map_to_song_model(s)
            if mapped.get("file_url"):
                songs.append(Song(**mapped))
        except:
            continue
            
    await CacheManager.set(cache_key, json.dumps([s.dict() for s in songs]), 600)
    return {"category": cat["name"], "songs": songs}

@app.get("/api/admin/songs")
async def get_admin_songs(
    skip: int = 0,
    limit: int = 50,
    admin_user: User = Depends(require_admin)
):
    # For admin view, maybe show trending songs from API
    api_results = await SongAPI.search_songs("Popular", limit)
    songs = []
    for api_song in api_results:
        songs.append(Song(**SongAPI.map_to_song_model(api_song)))
    return {"songs": songs}

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


if __name__ == "__main__":
    # When running locally, we serve the app on localhost:8000
    print("\n" + "="*50)
    print("StreamSync Music Streaming Service")
    print("Access the app at: http://localhost:8000")
    print("="*50 + "\n")
    
    # Optional: auto-open browser
    try:
        webbrowser.open("http://localhost:8000")
    except:
        pass
    uvicorn.run(
        "backend:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
        log_level="info"
    )