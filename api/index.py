from fastapi import FastAPI, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from typing import Optional, List, Dict, Any
from datetime import datetime, timedelta
import motor.motor_asyncio
import os
import certifi
import random
import uuid
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from pydantic import BaseModel, EmailStr

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Configuration from environment variables
MONGODB_URI = os.getenv("MONGODB_URI", "mongodb+srv://streetsofahmedabad2_db_user:mAEtqTMGGmEOziVE@cluster0.9u0xk1w.mongodb.net/streamsync?retryWrites=true&w=majority")
DATABASE_NAME = "streamsync"
SMTP_USERNAME = "bharatbyte.com@gmail.com"
SMTP_PASSWORD = "xbbixxdzmecsvjto"

# Database client initialized at module level for Vercel persistence
client = motor.motor_asyncio.AsyncIOMotorClient(
    MONGODB_URI, 
    tlsCAFile=certifi.where(), 
    tlsAllowInvalidCertificates=True,
    serverSelectionTimeoutMS=5000
)
db = client[DATABASE_NAME]

# --- Models ---
class OTPSend(BaseModel):
    email: EmailStr

class OTPVerify(BaseModel):
    email: EmailStr
    otp: str
    device_id: str

class AdminLock(BaseModel):
    code: str

# --- Helper Functions ---
async def send_mail(to_email: str, subject: str, body: str):
    try:
        msg = MIMEMultipart()
        msg['From'] = SMTP_USERNAME
        msg['To'] = to_email
        msg['Subject'] = subject
        msg.attach(MIMEText(body, 'html'))

        # Using a context manager for SMTP
        with smtplib.SMTP("smtp.gmail.com", 587, timeout=10) as server:
            server.starttls()
            server.login(SMTP_USERNAME, SMTP_PASSWORD)
            server.send_message(msg)
        return True
    except Exception as e:
        print(f"Mail error: {e}")
        return False

# --- API Routes ---

@app.get("/api/health")
async def health():
    try:
        await client.admin.command('ping')
        return {"status": "ok", "db": "connected"}
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.get("/api/user/recent")
async def get_recent():
    try:
        # Fetch songs sorted by play count
        cursor = db.songs.find().sort("play_count", -1).limit(6)
        songs = []
        async for s in cursor:
            s["id"] = str(s["_id"])
            if "_id" in s: del s["_id"]
            songs.append(s)
        return {"songs": songs}
    except Exception as e:
        return {"songs": [], "error": str(e)}

@app.get("/api/user/recommendations")
async def get_recs():
    try:
        # Fetch songs sorted by creation date
        cursor = db.songs.find().sort("created_at", -1).limit(6)
        songs = []
        async for s in cursor:
            s["id"] = str(s["_id"])
            if "_id" in s: del s["_id"]
            songs.append(s)
        return {"songs": songs}
    except Exception as e:
        return {"songs": [], "error": str(e)}

# In-memory OTP storage (will reset on lambda cold start, but good for demo)
otp_store = {}

@app.post("/api/auth/otp/send")
async def handle_send_otp(data: OTPSend):
    otp = str(random.randint(100000, 999999))
    otp_store[data.email] = {"otp": otp, "expires": datetime.utcnow() + timedelta(minutes=10)}
    
    body = f"Your StreamSync Verification Code is: <b>{otp}</b>"
    success = await send_mail(data.email, "StreamSync OTP", body)
    
    return {
        "message": "OTP sent" if success else "Email delivery failed",
        "debug_otp": otp # Fallback for demo if mail server blocks
    }

@app.post("/api/auth/otp/verify")
async def handle_verify_otp(data: OTPVerify):
    if data.email not in otp_store or datetime.utcnow() > otp_store[data.email]["expires"]:
        raise HTTPException(status_code=400, detail="OTP expired")
    
    if data.otp != otp_store[data.email]["otp"]:
        raise HTTPException(status_code=400, detail="Invalid OTP")
    
    await db.subscribers.update_one(
        {"email": data.email}, 
        {"$set": {"status": "active", "device_id": data.device_id, "last_login": datetime.utcnow()}}, 
        upsert=True
    )
    
    # Just a dummy token for simplicity in this build
    token = f"sub_{data.email}_{uuid.uuid4().hex}"
    return {"access_token": token}

@app.post("/api/auth/admin/verify")
async def handle_admin_verify(data: AdminLock):
    if data.code == "70458":
        token = f"admin_{uuid.uuid4().hex}"
        return {"access_token": token}
    raise HTTPException(status_code=401, detail="Invalid admin code")

@app.get("/api/auth/me")
async def get_me():
    # Return a dummy admin for the hosted build to bypass role checks
    return {"id": "admin", "name": "Admin User", "role": "admin"}
