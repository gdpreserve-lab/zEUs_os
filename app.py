"""
Reesporty — single-service deploy: serves the dashboard UI + Square analytics
+ agent execute endpoint. Real data only.
"""
import json
import os

import requests
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

from square_analytics import router as analytics_router, live_financials

GEMINI_MODEL = "gemini-2.5-flash"
GEMINI_URL = (
    f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
)

app = FastAPI(title="Reesporty — Executive Agent Command")

_origins = [
    o.strip() for o in os.environ.get("CORS_ORIGINS", "*").split(",") if o.strip()
]
app.add_middleware(CORSMiddleware, allow_origins=_origins, allow_methods=["*"], allow_headers=["*"])
app.include_router(analytics_router)

_HERE = os.path.dirname(os.path.abspath(__file__))


@app.get("/")
def home():
    return FileResponse(os.path.join(_HERE, "index.html"))


@app.post("/v1/agent/execute")
async def agent_execute(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    task = (body.get("task") or "").strip()
    agent_id = body.get("agent_id") or "executive_controller"

    if not task:
        return JSONResponse({"error": "task is required"}, status_code=400)

    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key:
        return {
            "configured": False,
            "response": (
                "Agent brain not configured yet.\n\n"
                "To enable real directives:\n"
                "1. Get a free Gemini API key at aistudio.google.com/apikey\n"
                "2. In Railway: your service > Variables > add GEMINI_API_KEY\n"
                "3. The service redeploys automatically — run this directive again."
            ),
        }

    # Give the model ONLY real settled data as context — never invented numbers.
    try:
        m = live_financials(start=None, end=None)
        metrics_ctx = json.dumps(m)
    except Exception:
        metrics_ctx = "{}"

    payload = {
        "system_instruction": {
            "parts": [
                {
                    "text": (
                        "You are the executive controller agent of Reesporty, an autonomous "
                        "multi-pillar business system. Answer using ONLY the real settled "
                        "Square financial data provided in the user message. Never invent or "
                        "simulate numbers. If the data shows no transactions yet, say so "
                        "plainly. Be concise, plain English, executive tone."
                    )
                }
            ]
        },
        "contents": [
            {
                "role": "user",
                "parts": [
                    {
                        "text": (
                            f"REAL CURRENT METRICS (Square settled payments): {metrics_ctx}\n\n"
                            f"EXECUTIVE DIRECTIVE: {task}"
                        )
                    }
                ],
            }
        ],
    }

    try:
        r = requests.post(f"{GEMINI_URL}?key={key}", json=payload, timeout=60)
    except Exception as e:
        return {"configured": True, "response": f"Gemini request failed: {e}"}

    if r.status_code != 200:
        return {"configured": True, "response": f"Gemini error HTTP {r.status_code}: {r.text[:300]}"}

    try:
        text = r.json()["candidates"][0]["content"]["parts"][0]["text"]
    except Exception:
        text = json.dumps(r.json())[:800]
    return {"configured": True, "agent_id": agent_id, "response": text}
