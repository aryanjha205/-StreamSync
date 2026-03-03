import sys
import os

# Add the parent directory to sys.path to import the main backend
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend import app

# This is the entry point for Vercel
# No need for extra code here as 'app' is the FastAPI instance from backend.py
