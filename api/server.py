import os
from pathlib import Path
from fastapi import FastAPI, Depends, HTTPException
from fastapi.responses import StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from fastapi_clerk_auth import ClerkConfig, ClerkHTTPBearer, HTTPAuthorizationCredentials
from openai import OpenAI

app = FastAPI()

# Add CORS middleware (allows frontend to call backend)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Clerk authentication setup
jwks_url = os.getenv("CLERK_JWKS_URL")
if not jwks_url or not jwks_url.strip():
    raise ValueError(
        "CLERK_JWKS_URL environment variable is missing or empty! "
        "Make sure to run Docker with --env-file .env"
    )

clerk_config = ClerkConfig(
    jwks_url=jwks_url.strip(),
    leeway=60,
)
clerk_guard = ClerkHTTPBearer(clerk_config, debug_mode=False)

class Visit(BaseModel):
    patient_name: str
    date_of_visit: str
    notes: str

system_prompt = """
You are provided with notes written by a doctor from a patient's visit.
Your job is to summarize the visit for the doctor and provide an email.
Reply with exactly three sections with the headings:
### Summary of visit for the doctor's records
### Next steps for the doctor
### Draft of email to patient in patient-friendly language
"""

def user_prompt_for(visit: Visit) -> str:
    return f"""Create the summary, next steps and draft email for:
Patient Name: {visit.patient_name}
Date of Visit: {visit.date_of_visit}
Notes:
{visit.notes}"""

@app.post("/api")
@app.post("/api/consultation")
def consultation_summary(
    visit: Visit,
    creds: HTTPAuthorizationCredentials = Depends(clerk_guard),
):
    groq_api_key = os.getenv("GROQ_API_KEY")
    if not groq_api_key:
        raise HTTPException(
            status_code=500,
            detail="GROQ_API_KEY environment variable is not configured."
        )

    client = OpenAI(
        api_key=groq_api_key,
        base_url="https://api.groq.com/openai/v1"
    )

    user_prompt = user_prompt_for(visit)
    prompt = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    stream = client.chat.completions.create(
        model="openai/gpt-oss-120b",
        messages=prompt,
        stream=True,
    )

    def event_stream():
        for chunk in stream:
            text = chunk.choices[0].delta.content
            if text:
                lines = text.split("\n")
                for line in lines[:-1]:
                    yield f"data: {line}\n\n"
                    yield "data:  \n"
                yield f"data: {lines[-1]}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")

@app.get("/health")
def health_check():
    """Health check endpoint (used for local Docker; Lambda does not invoke it)"""
    return {"status": "healthy"}

# Serve Next.js static export files - MUST BE LAST!
static_path = Path("static")
if static_path.exists():
    # Mount /_next static assets directly so chunks & css load with high performance
    if (static_path / "_next").exists():
        app.mount("/_next", StaticFiles(directory=str(static_path / "_next")), name="next_assets")

    @app.get("/")
    async def serve_root():
        return FileResponse(static_path / "index.html")

    @app.get("/product")
    async def serve_product():
        return FileResponse(static_path / "product.html")

    @app.get("/{full_path:path}")
    async def serve_fallback(full_path: str):
        target = static_path / full_path
        if target.is_file():
            return FileResponse(target)
        target_html = static_path / f"{full_path}.html"
        if target_html.is_file():
            return FileResponse(target_html)
        return FileResponse(static_path / "index.html")
