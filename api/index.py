from fastapi import FastAPI

app = FastAPI()

@app.get("/api/health")
@app.get("/health")
async def health():
    return {"status": "ok", "runtime": "python3.9"}

@app.get("/api/user/recent")
async def recent():
    return {"songs": []}

@app.get("/api/user/recommendations")
async def recs():
    return {"songs": []}
