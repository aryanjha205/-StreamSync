import motor.motor_asyncio
import asyncio
import certifi

async def test():
    uri = "mongodb://127.0.0.1:27017"
    print(f"Testing connection to {uri}")
    client = motor.motor_asyncio.AsyncIOMotorClient(uri, serverSelectionTimeoutMS=2000)
    try:
        await client.admin.command('ping')
        print("Ping success!")
    except Exception as e:
        print(f"Ping failed: {e}")

if __name__ == "__main__":
    asyncio.run(test())
