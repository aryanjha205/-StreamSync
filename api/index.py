import sys
import os

# Add the root directory to sys.path to allow importing backend.py
# This is necessary for Vercel deployment where the 'api' folder is a subdirectory
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Import the FastAPI app from backend.py
try:
    from backend import app
except ImportError as e:
    # Fallback for debugging if import fails on Vercel
    from fastapi import FastAPI
    app = FastAPI()
    @app.get("/api/debug")
    async def debug():
        return {"error": str(e), "path": sys.path, "cwd": os.getcwd()}

# Vercel requires the app object to be named 'app'
