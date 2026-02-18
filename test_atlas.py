import motor.motor_asyncio
import asyncio
import certifi

async def test():
    uri = "mongodb+srv://streetsofahmedabad2_db_user:mAEtqTMGGmEOziVE@cluster0.9u0xk1w.mongodb.net/streamsync?retryWrites=true&w=majority"
    print(f"Testing connection to Atlas...")
    client = motor.motor_asyncio.AsyncIOMotorClient(uri, tlsCAFile=certifi.where())
    try:
        await asyncio.wait_for(client.admin.command('ping'), timeout=10.0)
        print("Connected successfully!")
        db = client["streamsync"]
        count = await db.songs.count_documents({})
        print(f"Found {count} songs.")
    except Exception as e:
        print(f"Connection failed: {e}")

if __name__ == "__main__":
    asyncio.run(test())
