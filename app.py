import os
import json
from fastapi import FastAPI, Request, HTTPException
from pydantic import BaseModel
import asyncio
from solver import solve_quiz

print("DEBUG: starting app.py - project_2 envvar =", repr(os.environ.get("project_2")))


app = FastAPI(title="LLM Analysis Quiz Solver")


QUIZ_SECRET = os.environ.get("QUIZ_SECRET")
FALLBACK_SECRET = os.environ.get("FALLBACK_SECRET", "project_2")  
ALLOW_INSECURE_DEV = os.environ.get("ALLOW_INSECURE_DEV", "0") == "1"

if not QUIZ_SECRET and not ALLOW_INSECURE_DEV:
    print("WARNING: QUIZ_SECRET not set and ALLOW_INSECURE_DEV != 1. Requests will be rejected until QUIZ_SECRET is set.")

class SolvePayload(BaseModel):
    email: str
    secret: str
    url: str

@app.post("/solve")
async def solve_endpoint(payload: SolvePayload):

    expected = QUIZ_SECRET if QUIZ_SECRET else (FALLBACK_SECRET if ALLOW_INSECURE_DEV else None)

    if expected is None or payload.secret != expected:

        raise HTTPException(status_code=403, detail="Invalid secret")

    try:
        result = await solve_quiz(payload.url, payload.email, payload.secret)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Solver error: {str(e)}")

    return {"ok": True, "solver_result": result}
