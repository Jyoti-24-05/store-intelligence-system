"""Main application module."""
from fastapi import FastAPI

app = FastAPI(title="Store Intelligence API")

@app.get("/health")
async def health():
    return {"status": "healthy", "service": "store-intelligence-api"}