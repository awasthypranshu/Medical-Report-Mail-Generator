import os
from typing import Optional
from fastapi import FastAPI, Depends, HTTPException, Request  # type: ignore
from fastapi.responses import StreamingResponse, JSONResponse  # type: ignore
from fastapi.security.utils import get_authorization_scheme_param  # type: ignore
from pydantic import BaseModel  # type: ignore
import jwt  # type: ignore
from fastapi.encoders import jsonable_encoder  # type: ignore
from fastapi_clerk_auth import ClerkConfig, ClerkHTTPBearer, HTTPAuthorizationCredentials  # type: ignore
from jwt.exceptions import ExpiredSignatureError, PyJWTError  # type: ignore
from openai import OpenAI  # type: ignore

app = FastAPI()

@app.exception_handler(ExpiredSignatureError)
async def expired_signature_handler(request: Request, exc: ExpiredSignatureError):
    return JSONResponse(
        status_code=401,
        content={"detail": "Token signature has expired. Please refresh the page."},
    )

@app.exception_handler(PyJWTError)
async def jwt_error_handler(request: Request, exc: PyJWTError):
    return JSONResponse(
        status_code=401,
        content={"detail": f"Invalid authentication token: {str(exc)}"},
    )

class CustomClerkHTTPBearer(ClerkHTTPBearer):
    def _decode_token(self, token: str) -> dict | None:
        try:
            signing_key = self.jwks_client.get_signing_key_from_jwt(token)
            decoded_token = jwt.decode(
                token,
                key=signing_key.key,
                audience=self.audience,
                issuer=self.issuer,
                algorithms=["RS256"],
                options={
                    "verify_exp": self.config.verify_exp,
                    "verify_aud": self.config.verify_aud,
                    "verify_iss": self.config.verify_iss,
                    "verify_iat": self.config.verify_iat,
                },
                leeway=self.config.leeway,
            )
            return dict(jsonable_encoder(decoded_token))
        except Exception as e:
            print(f"--> [Clerk Auth Error]: {type(e).__name__}: {e}", flush=True)
            return None

    async def __call__(self, request: Request) -> Optional[HTTPAuthorizationCredentials]:
        authorization = request.headers.get("Authorization")
        if not authorization:
            print("--> [Clerk Auth Error]: No Authorization header sent by client!", flush=True)
            raise HTTPException(status_code=401, detail="Missing Authorization header")
        scheme, credentials = get_authorization_scheme_param(authorization)
        if scheme.lower() != "bearer":
            print(f"--> [Clerk Auth Error]: Invalid scheme '{scheme}'", flush=True)
            raise HTTPException(status_code=401, detail="Invalid auth scheme, Bearer required")

        decoded_token = self._decode_token(token=credentials)
        if not decoded_token:
            raise HTTPException(status_code=401, detail="Invalid or expired authentication token. Please refresh the page.")

        return HTTPAuthorizationCredentials(scheme=scheme, credentials=credentials, decoded=decoded_token)

clerk_config = ClerkConfig(
    jwks_url=os.getenv("CLERK_JWKS_URL", "").strip(),
    leeway=60,
)
clerk_guard = CustomClerkHTTPBearer(clerk_config, debug_mode=False)


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
    user_id = creds.decoded["sub"]

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