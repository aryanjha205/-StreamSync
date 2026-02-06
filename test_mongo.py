import asyncio
import motor.motor_asyncio
import certifi
import os

async def test_mongo():
    uri = "mongodb+srv://streetsofahmedabad2_db_user:mAEtqTMGGmEOziVE@cluster0.9u0xk1w.mongodb.net/"
    print(f"Testing connection to: {uri}")
    try:
        client = motor.motor_asyncio.AsyncIOMotorClient(
            uri, 
            tlsCAFile=certifi.where(),
            tlsAllowInvalidCertificates=True,
            serverSelectionTimeoutMS=5000
        )
        await client.admin.command('ping')
        print("Success: Connected with SSL/Certifi")
        return
    except Exception as e:
        print(f"Failed with SSL/Certifi: {e}")

    try:
        print("Retrying without SSL...")
        client = motor.motor_asyncio.AsyncIOMotorClient(
            uri,
            tls=False,
            serverSelectionTimeoutMS=5000
        )
        await client.admin.command('ping')
        print("Success: Connected without SSL")
    except Exception as e:
        print(f"Failed without SSL: {e}")

if __name__ == "__main__":
    asyncio.run(test_mongo())
