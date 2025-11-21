# main.py
import os
import json
import re
from typing import Optional, List, Dict, Any
from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import httpx
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()  # charge .env local

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError("Supabase config missing in .env")

FAQ_ENDPOINT = f"{SUPABASE_URL}/rest/v1/faq"
CONV_ENDPOINT = f"{SUPABASE_URL}/rest/v1/conversations"

SUPABASE_HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
    "Accept": "application/json"
}

app = FastAPI(title="Wozo Chatbot API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class MessageIn(BaseModel):
    user_id: Optional[str] = None
    message: str

# ======================================================
#  NOUVEAU MOTEUR DE MATCHING AMÉLIORÉ
# ======================================================

MATCH_THRESHOLD = 2        # plus haut = plus strict
SUGGEST_COUNT = 3          # nombre de suggestions à proposer

def tokenize(text: str) -> List[str]:
    text = (text or "").lower()
    text = re.sub(r"[^\w\sàâéèêîôûçëüï'-]", " ", text)
    return [t for t in text.split() if len(t) > 1]

def score_row_by_overlap(query_tokens: List[str], row: Dict[str, Any]) -> float:
    text = ""
    if row.get("question_examples"):
        text += " ".join(row["question_examples"]) + " "
    if row.get("tags"):
        text += " ".join(row["tags"]) + " "
    if row.get("intent"):
        text += row["intent"]

    tokens = tokenize(text)
    score = 0.0

    for t in query_tokens:
        if t in tokens:
            score += 1.0
        else:
            # matching approximate subtokens
            for tk in tokens:
                if t in tk or tk in t:
                    score += 0.5
                    break
    return score

def simple_match(query: str, faq_rows: List[Dict[str, Any]]):
    qtokens = tokenize(query)
    best = {"score": 0.0, "row": None}
    scored = []

    for row in faq_rows:
        s = score_row_by_overlap(qtokens, row)
        scored.append((s, row))
        if s > best["score"]:
            best = {"score": s, "row": row}

    scored.sort(key=lambda x: x[0], reverse=True)
    return best, scored

# ======================================================
#  SUPABASE
# ======================================================

async def fetch_faq():
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(FAQ_ENDPOINT, headers=SUPABASE_HEADERS, params={"select": "*"})
        resp.raise_for_status()
        return resp.json()

async def insert_conversation(user_id: Optional[str], messages: List[Dict[str, Any]]):
    payload = {
        "user_id": user_id,
        "messages": messages,
        "metadata": {}
    }
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(CONV_ENDPOINT, headers=SUPABASE_HEADERS, json=payload)
        resp.raise_for_status()
        return resp.json()

# ======================================================
#  ROUTE PRINCIPALE /api/message
# ======================================================

@app.post("/api/message")
async def handle_message(payload: MessageIn):
    if not payload.message or not payload.message.strip():
        raise HTTPException(status_code=400, detail="Empty message")

    message_text = payload.message.strip()

    try:
        faq_rows = await fetch_faq()
    except Exception as e:
        raise HTTPException(status_code=502, detail="Error fetching FAQ data")

    best, ranked = simple_match(message_text, faq_rows)
    match_score = best["score"] or 0.0

    # ----- Bonne correspondance
    if best["row"] and match_score >= MATCH_THRESHOLD:
        answer = best["row"].get("answer", "Désolé, je n'ai pas de réponse prête.")
        response_payload = {
            "answer": answer,
            "found": True,
            "intent": best["row"].get("intent"),
            "match_score": match_score
        }

    # ----- Mauvaise correspondance : fallback intelligent
    else:
        suggestions = []
        for score, row in ranked[:SUGGEST_COUNT]:
            examples = row.get("question_examples") or []
            if examples:
                suggestions.append(examples[0])
            else:
                suggestions.append(row.get("intent") or "")

        response_payload = {
            "answer": "Désolé — je n’ai pas trouvé de réponse précise à ta question.",
            "found": False,
            "suggested_questions": suggestions,
            "note": "Tu peux reformuler ou choisir une question suggérée.",
            "match_score": match_score
        }

    # ----- Log dans Supabase
    now_iso = datetime.utcnow().isoformat() + "Z"
    messages = [
        {"from": "user", "text": message_text, "timestamp": now_iso},
        {"from": "bot", "text": response_payload["answer"], "timestamp": datetime.utcnow().isoformat() + "Z"}
    ]

    try:
        await insert_conversation(payload.user_id, messages)
    except Exception:
        pass  # on ignore l'erreur mais le bot répond quand même

    return response_payload
