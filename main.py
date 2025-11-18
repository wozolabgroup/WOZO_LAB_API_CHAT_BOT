# main.py
import os
import json
from typing import Optional, List, Dict, Any
from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import httpx
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()  # charge .env en local

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set")

# Endpoints Supabase REST
FAQ_ENDPOINT = f"{SUPABASE_URL}/rest/v1/faq"
CONV_ENDPOINT = f"{SUPABASE_URL}/rest/v1/conversations"

# header pour Supabase (service role key)
SUPABASE_HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
    "Accept": "application/json"
}

app = FastAPI(title="Wozo Chatbot API (FastAPI)")

# autoriser ton frontend (ajoute l'origin React)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],  # ajuste
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class MessageIn(BaseModel):
    user_id: Optional[str] = None
    message: str

# util: simple matching (adapté pour Python)
def simple_match(query: str, faq_rows: List[Dict[str, Any]]):
    q = query.lower()
    best = {"score": 0, "row": None}
    for row in faq_rows:
        # question_examples from supabase come as list
        keywords = " ".join(row.get("question_examples") or []).lower()
        tags = " ".join(row.get("tags") or []).lower()
        score = 0
        if keywords:
            # split to words to give small scoring
            for k in keywords.split():
                if k and k in q:
                    score += 1
        if tags:
            for t in tags.split():
                if t and t in q:
                    score += 2
        intent = (row.get("intent") or "").lower()
        if intent and intent in q:
            score += 2
        if score > best["score"]:
            best = {"score": score, "row": row}
    return best

async def fetch_faq():
    # récupère toutes les lignes de la table faq
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(FAQ_ENDPOINT, headers=SUPABASE_HEADERS, params={"select":"*"})
        resp.raise_for_status()
        return resp.json()

async def insert_conversation(user_id: Optional[str], messages: List[Dict[str, Any]]):
    payload = {
        "user_id": user_id,
        "messages": messages,
        "metadata": {}
    }
    async with httpx.AsyncClient(timeout=10.0) as client:
        # Supabase REST: POST to insert record
        resp = await client.post(CONV_ENDPOINT, headers=SUPABASE_HEADERS, content=json.dumps(payload))
        resp.raise_for_status()
        return resp.json()

@app.post("/api/message")
async def handle_message(payload: MessageIn):
    if not payload.message or not payload.message.strip():
        raise HTTPException(status_code=400, detail="Empty message")
    message_text = payload.message.strip()
    # 1) fetch FAQ
    try:
        faq_rows = await fetch_faq()
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"Error fetching faq: {str(e)}")

    # 2) match
    match = simple_match(message_text, faq_rows)
    if match["row"] and match["score"] > 0:
        answer = match["row"].get("answer", "Désolé, je n'ai pas de réponse prête.")
    else:
        answer = ("Je n'ai pas trouvé une réponse précise. "
                  "Souhaites-tu que je te mette en contact avec le support ou que je consulte la documentation API ?")

    # 3) log conversation (création simple)
    now_iso = datetime.utcnow().isoformat() + "Z"
    messages = [
        {"from": "user", "text": message_text, "timestamp": now_iso},
        {"from": "bot", "text": answer, "timestamp": datetime.utcnow().isoformat() + "Z"}
    ]
    try:
        await insert_conversation(payload.user_id, messages)
    except httpx.HTTPError:
        # ne bloque pas la réponse si l'insertion échoue, mais log (ici on ignore)
        pass

    return {"answer": answer}
