from fastapi import FastAPI
import os
import motor.motor_asyncio
import asyncio

app = FastAPI()

# Minimal config
MONGODB_URI = "mongodb+srv://streetsofahmedabad2_db_user:mAEtqTMGGmEOziVE@cluster0.9u0xk1w.mongodb.net/streamsync?retryWrites=true&w=majority"
client = motor.motor_asyncio.AsyncIOMotorClient(MONGODB_URI)
db = client["streamsync"]

@app.get("/api/health")
async def health():
    try:
        # Just check if we can get collection names
        # Use a short timeout
        cols = await asyncio.wait_for(db.list_collection_names(), timeout=2.0)
        return {"status": "ok", "collections": cols}
    except Exception as e:
        return {"status": "partial_ok", "error": str(e)}

@app.get("/api/user/recent")
async def recent():
    try:
        cursor = db.songs.find().sort("play_count", -1).limit(6)
        songs = []
        async for s in cursor:
            s["id"] = str(s["_id"])
            del s["_id"]
            songs.append(s)
        return {"songs": songs}
    except Exception as e:
        return {"songs": [], "error": str(e)}

@app.get("/api/user/recommendations")
async def recs():
    try:
        cursor = db.songs.find().sort("created_at", -1).limit(6)
        songs = []
        async for s in cursor:
            s["id"] = str(s["_id"])
            del s["_id"]
            songs.append(s)
        return {"songs": songs}
    except Exception as e:
        return {"songs": [], "error": str(e)}
