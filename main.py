# main.py
from dotenv import load_dotenv

load_dotenv()  # local dev reads .env; on Cloud Run the env comes from deploy.sh

from fastapi import FastAPI  # noqa: E402
import os  # noqa: E402

app = FastAPI()

@app.get("/")
def root():
    return {"status": "ok", "message": "SDOC hackathon Hello World!"}

@app.get("/health")
def health():
    return {"healthy": True}