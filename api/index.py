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
try:
    from jose import jwt
except ImportError:
    import jwt

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Settings ---
class Settings:
    # TRY TO GET URI FROM ENV FIRST (Vercel Setting)
    MONGODB_URI = os.getenv("MONGODB_URI", "mongodb+srv://streetsofahmedabad2_db_user:mAEtqTMGGmEOziVE@cluster0.9u0xk1w.mongodb.net/streamsync?retryWrites=true&w=majority")
    DATABASE_NAME = os.getenv("DATABASE_NAME", "streamsync")
    JWT_SECRET = os.getenv("JWT_SECRET", "your-super-secret-jwt-key-change-in-production")
    JWT_ALGORITHM = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES = 30
    
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

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
security = HTTPBearer()

db = None
db_client = None

# --- Models ---
class UserBase(BaseModel):
    name: str
    email: EmailStr

class UserCreate(UserBase):
    password: str

class User(UserBase):
    id: str
    role: str = "user"
    created_at: datetime
    is_active: bool = True

class Token(BaseModel):
    access_token: str
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

# --- Database Manager ---
async def get_db():
    global db, db_client
    if db is not None:
        return db
    try:
        client_kwargs = {
            "serverSelectionTimeoutMS": 5000,
            "connectTimeoutMS": 5000,
            "tlsCAFile": certifi.where(),
            "tlsAllowInvalidCertificates": True
        }
        db_client = motor.motor_asyncio.AsyncIOMotorClient(settings.MONGODB_URI, **client_kwargs)
        await db_client.admin.command('ping')
        db = db_client[settings.DATABASE_NAME]
        return db
    except Exception as e:
        logger.error(f"MongoDB Connection Error: {e}")
        raise HTTPException(status_code=503, detail="Database connection failed")

def create_token(data: dict, expires_delta: timedelta) -> str:
    to_encode = data.copy()
    expire = datetime.utcnow() + expires_delta
    to_encode.update({"exp": expire})
    try:
        # Compatibility between python-jose and pyjwt
        if hasattr(jwt, 'encode_payload'): # Usually jose
            return jwt.encode(to_encode, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)
        else: # Usually pyjwt
            return jwt.encode(to_encode, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)
    except:
        return jwt.encode(to_encode, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)

async def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(security)) -> User:
    try:
        payload = jwt.decode(credentials.credentials, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])
        user_id = payload.get("sub")
        if not user_id: raise HTTPException(status_code=401)
        
        database = await get_db()
        user_doc = await database.users.find_one({"_id": user_id})
        if not user_doc: raise HTTPException(status_code=401)
        
        user_doc["id"] = user_doc.pop("_id")
        return User(**user_doc)
    except:
        raise HTTPException(status_code=401, detail="Invalid token")

# --- App Instance ---
app = FastAPI(title="StreamSync API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Routes ---
@app.get("/api/health")
async def health():
    try:
        d = await get_db()
        return {"status": "ok", "db": "connected"}
    except:
        return {"status": "partial", "db": "disconnected"}

@app.get("/api/user/recent")
async def get_recent():
    d = await get_db()
    cursor = d.songs.find().sort("play_count", -1).limit(6)
    songs = []
    async for s in cursor:
        s["id"] = str(s["_id"])
        del s["_id"]
        songs.append(s)
    return {"songs": songs}

@app.get("/api/user/recommendations")
async def get_recs():
    d = await get_db()
    cursor = d.songs.find().sort("created_at", -1).limit(6)
    songs = []
    async for s in cursor:
        s["id"] = str(s["_id"])
        del s["_id"]
        songs.append(s)
    return {"songs": songs}

otp_store = {}

@app.post("/api/auth/otp/send")
async def send_otp(data: OTPSend):
    otp = str(random.randint(100000, 999999))
    otp_store[data.email] = {"otp": otp, "expires": datetime.utcnow() + timedelta(minutes=10)}
    body = f"Your StreamSync OTP is: <b>{otp}</b>"
    success = EmailManager.send_email(data.email, "StreamSync OTP", body)
    if not success:
        return {"message": "Email delivery failed", "debug_otp": otp}
    return {"message": "OTP sent"}

@app.post("/api/auth/otp/verify")
async def verify_otp(data: OTPVerify):
    if data.email not in otp_store or datetime.utcnow() > otp_store[data.email]["expires"]:
        raise HTTPException(status_code=400, detail="Expired")
    if data.otp != otp_store[data.email]["otp"]:
        raise HTTPException(status_code=400, detail="Invalid")
    
    d = await get_db()
    await d.subscribers.update_one({"email": data.email}, {"$set": {"status": "active", "device_id": data.device_id}}, upsert=True)
    access_token = create_token({"sub": data.email, "type": "subscription"}, timedelta(days=3650))
    return {"access_token": access_token}

@app.post("/api/auth/admin/verify")
async def verify_admin(data: AdminLock):
    if data.code == "70458":
        d = await get_db()
        admin = await d.users.find_one({"role": "admin"})
        if not admin:
            # Create a fallback admin if missing
            admin_id = str(uuid.uuid4())
            await d.users.insert_one({"_id": admin_id, "name": "Admin", "email": "admin@streamsync.com", "role": "admin", "created_at": datetime.utcnow()})
        else:
            admin_id = admin["_id"]
            
        access_token = create_token({"sub": admin_id, "role": "admin"}, timedelta(hours=24))
        return {"access_token": access_token}
    raise HTTPException(status_code=401, detail="Invalid code")

@app.get("/api/auth/me")
async def get_me(user: User = Depends(get_current_user)):
    return user
