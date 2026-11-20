"""
Speech-to-prompt for the AI Console: the dashboard records a short clip in the
browser and posts it here; we return the transcript as plain text which the
composer drops into the input box for the user to edit before sending.

    POST /api/v1/transcribe        multipart/form-data: file=<audio blob>
      -> 200 {"text": "..."}

Reuses attachment_extractor.transcribe_audio (Groq Whisper, language
auto-detected so mixed Indonesian/English works). Nothing is stored: the
audio lives in memory for the duration of the request.
"""

import logging
import os
from typing import Optional

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile

from app.routes.agent_actions import get_current_user_payload
from app.services import attachment_extractor as ax
from app.utils.cache import check_rate_limit

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["transcribe"])

# Short dictation clips are frequently misdetected by Whisper's auto-detect
# (e.g. Indonesian heard as Croatian), so default to Indonesian; English words
# inside Indonesian speech are still transcribed. Set STT_CONSOLE_LANGUAGE to
# "" to go back to auto-detect.
_LANGUAGE = os.getenv("STT_CONSOLE_LANGUAGE", "id") or None
_PROMPT = os.getenv(
    "STT_CONSOLE_PROMPT",
    "Percakapan campuran Bahasa Indonesia dan English dengan agent AI di Aivory: "
    "workflow, lead, CRM, pipeline, Aira, Lex.",
)

_RATE_LIMIT_PER_WINDOW = 60
_RATE_LIMIT_WINDOW_SECONDS = 3600


@router.post("/transcribe")
async def transcribe(
    file: UploadFile = File(...),
    payload: dict = Depends(get_current_user_payload),
):
    user_id = str(payload.get("user_id"))

    try:
        within_limit = check_rate_limit(
            f"transcribe:{user_id}",
            limit=_RATE_LIMIT_PER_WINDOW,
            window=_RATE_LIMIT_WINDOW_SECONDS,
        )
    except Exception as e:  # Redis down must not block dictation
        logger.error(f"transcribe rate limit check failed for {user_id}: {e}")
        within_limit = True
    if not within_limit:
        raise HTTPException(
            status_code=429,
            detail=f"Rate limit exceeded ({_RATE_LIMIT_PER_WINDOW} voice inputs/hour).",
        )

    # Read one byte past the cap so an oversized upload is rejected without
    # buffering all of it.
    data = await file.read(ax.MAX_AUDIO_BYTES + 1)
    if not data:
        raise HTTPException(status_code=400, detail="Empty audio")
    if len(data) > ax.MAX_AUDIO_BYTES:
        raise HTTPException(status_code=413, detail="Audio too large")

    mime: Optional[str] = (file.content_type or "").split(";")[0] or None
    text = ax.transcribe_audio(
        data, file.filename or "voice.webm", mime, language=_LANGUAGE, prompt=_PROMPT
    )
    if text is None:
        raise HTTPException(status_code=502, detail="Transcription failed")
    return {"text": text}
