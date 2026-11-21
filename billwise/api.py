"""FastAPI service exposing /redact and /parse.

Privacy notes:
  * Request bodies are never logged or stored by this app.
  * /redact returns placeholders, types and offsets only - never the original
    values. The reversible mapping exists only inside the request handler and
    is discarded when the request ends.
"""
from __future__ import annotations

from fastapi import FastAPI
from pydantic import BaseModel, Field, field_validator
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from . import __version__
from .models import ParseResult
from .parse import parse as parse_text
from .redact import PII_TYPES, Redactor

MAX_TEXT_CHARS = 100_000          # ~ 30+ pages of bill text
MAX_BODY_BYTES = 512 * 1024       # hard cap on raw request body


class BodySizeLimit:
    """Reject request bodies over ``max_bytes`` with 413, even without Content-Length."""

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        for k, v in scope.get("headers", []):
            if k == b"content-length" and v.isdigit() and int(v) > self.max_bytes:
                await JSONResponse({"detail": "request body too large"}, status_code=413)(scope, receive, send)
                return
        seen = 0

        async def limited_receive() -> Message:
            nonlocal seen
            msg = await receive()
            if msg["type"] == "http.request":
                seen += len(msg.get("body", b""))
                if seen > self.max_bytes:
                    raise _TooLarge()
            return msg

        try:
            await self.app(scope, limited_receive, send)
        except _TooLarge:
            await JSONResponse({"detail": "request body too large"}, status_code=413)(scope, receive, send)


class _TooLarge(Exception):
    pass


class TextIn(BaseModel):
    text: str = Field(..., min_length=1, max_length=MAX_TEXT_CHARS,
                      description="Plain text of a bill or EOB (synthetic data only in phase 1)")

    @field_validator("text")
    @classmethod
    def not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("text must not be blank")
        if "\x00" in v:
            raise ValueError("text must not contain NUL bytes")
        return v


class EntityOut(BaseModel):
    type: str
    start: int
    end: int
    placeholder: str


class RedactOut(BaseModel):
    redacted_text: str
    entities: list[EntityOut]
    counts: dict[str, int]


app = FastAPI(title="BillWise (phase 1)", version=__version__,
              description="Parse and redact synthetic medical bills / EOBs. No LLM calls.")
app.add_middleware(BodySizeLimit, max_bytes=MAX_BODY_BYTES)
_redactor = Redactor()


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "version": __version__}


@app.post("/redact", response_model=RedactOut)
def redact_endpoint(body: TextIn) -> RedactOut:
    result = _redactor.redact(body.text)
    counts = {t: 0 for t in PII_TYPES}
    for s in result.spans:
        counts[s.type] += 1
    return RedactOut(
        redacted_text=result.redacted_text,
        entities=[EntityOut(type=s.type, start=s.start, end=s.end, placeholder=s.placeholder) for s in result.spans],
        counts=counts,
    )


@app.post("/parse", response_model=ParseResult)
def parse_endpoint(body: TextIn) -> ParseResult:
    return parse_text(body.text)
