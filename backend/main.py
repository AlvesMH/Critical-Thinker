import asyncio
import io
import os
from datetime import datetime
from typing import Any

import httpx
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi import Body, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pypdf import PdfReader
from reportlab.lib import colors
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.platypus import ListFlowable, ListItem, Paragraph, SimpleDocTemplate, Spacer

from app.prompts import AGENT_CONFIGS

MAX_TEXT_CHARS = 100_000
MAX_PDF_BYTES = 10 * 1024 * 1024
DEFAULT_MODEL = os.getenv("GEMMA_MODEL", "Gemma-SEA-LION-v4-27B-IT")
GEMMA_API_URL = os.getenv("GEMMA_API_URL", "")
GEMMA_API_KEY = os.getenv("GEMMA_API_KEY", "")

app = FastAPI(title="Critical Thinking Analysis API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("CORS_ORIGINS", "*").split(","),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _extract_pdf_text(file_bytes: bytes) -> str:
    reader = PdfReader(io.BytesIO(file_bytes))
    text_parts = []
    for page in reader.pages:
        page_text = page.extract_text() or ""
        text_parts.append(page_text)
    return "\n".join(text_parts).strip()


def _validate_text(text: str) -> str:
    cleaned = (text or "").strip()
    if not cleaned:
        raise HTTPException(status_code=400, detail="No text provided for analysis.")
    if len(cleaned) > MAX_TEXT_CHARS:
        raise HTTPException(
            status_code=413,
            detail="Input is too long. Please shorten the text or upload a smaller PDF.",
        )
    return cleaned


def _build_prompt(agent: dict[str, Any], text: str) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": agent["system"]},
        {"role": "user", "content": text},
    ]


async def _call_model(client: httpx.AsyncClient, agent: dict[str, Any], text: str) -> str:
    if not GEMMA_API_URL or not GEMMA_API_KEY:
        raise RuntimeError("Model API configuration is missing.")
    payload = {
        "model": DEFAULT_MODEL,
        "messages": _build_prompt(agent, text),
        "temperature": 0.3,
    }
    headers = {"Authorization": f"Bearer {GEMMA_API_KEY}"}
    response = await client.post(GEMMA_API_URL, json=payload, headers=headers, timeout=60)
    response.raise_for_status()
    data = response.json()
    if "choices" in data:
        return data["choices"][0]["message"]["content"].strip()
    if "generated_text" in data:
        return data["generated_text"].strip()
    raise RuntimeError("Unexpected response from model API.")


async def _run_agents(text: str) -> dict[str, Any]:
    results: dict[str, Any] = {}
    async with httpx.AsyncClient() as client:
        tasks = []
        for agent in AGENT_CONFIGS:
            tasks.append(_call_model(client, agent, text))
        responses = await asyncio.gather(*tasks, return_exceptions=True)
    for agent, response in zip(AGENT_CONFIGS, responses):
        if isinstance(response, Exception):
            results[agent["key"]] = {
                "status": "error",
                "message": "Analysis could not be generated. Please try again.",
            }
        else:
            results[agent["key"]] = {"status": "ok", "content": response}
    return results


@app.get("/")
async def root() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/health")
async def health_check() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/analyze")
async def analyze(
    request: Request,
    text: str | None = Form(None),
    file: UploadFile | None = File(None),
    payload: dict[str, Any] | None = Body(None),
) -> JSONResponse:
    if file is not None:
        if file.content_type != "application/pdf":
            raise HTTPException(status_code=400, detail="Only PDF uploads are supported.")
        file_bytes = await file.read()
        if len(file_bytes) > MAX_PDF_BYTES:
            raise HTTPException(status_code=413, detail="PDF is too large. Max size is 10MB.")
        try:
            extracted = _extract_pdf_text(file_bytes)
        except Exception as exc:
            raise HTTPException(status_code=400, detail="Unable to read PDF text.") from exc
        cleaned = _validate_text(extracted)
    else:
        if text is None and request.headers.get("content-type", "").startswith("application/json"):
            payload = await request.json()
            if isinstance(payload, dict):
                text = payload.get("text")
        if payload and isinstance(payload, dict) and "text" in payload:
            text = payload.get("text")
        if text is None:
            raise HTTPException(status_code=400, detail="Provide text or upload a PDF.")
        cleaned = _validate_text(text)

    results = await _run_agents(cleaned)
    if all(value["status"] == "error" for value in results.values()):
        raise HTTPException(status_code=502, detail="Analysis failed. Please try again later.")
    return JSONResponse({"analysis": results})


def _markdown_to_blocks(markdown_text: str) -> list[Any]:
    styles = getSampleStyleSheet()
    normal = styles["BodyText"]
    normal.spaceAfter = 8
    heading = ParagraphStyle(
        "Heading",
        parent=styles["Heading3"],
        textColor=colors.HexColor("#1f2937"),
        spaceBefore=12,
        spaceAfter=6,
    )

    blocks: list[Any] = []
    lines = markdown_text.splitlines()
    list_items = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("-"):
            list_items.append(ListItem(Paragraph(stripped[1:].strip(), normal)))
            continue
        if list_items:
            blocks.append(ListFlowable(list_items, bulletType="bullet"))
            list_items = []
        if stripped.startswith("### "):
            blocks.append(Paragraph(stripped.replace("### ", ""), heading))
        elif stripped:
            blocks.append(Paragraph(stripped, normal))
    if list_items:
        blocks.append(ListFlowable(list_items, bulletType="bullet"))
    return blocks


def _build_pdf(analysis: dict[str, Any]) -> bytes:
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=LETTER, title="Critical Thinking Analysis Report")
    styles = getSampleStyleSheet()
    title_style = styles["Title"]
    subtitle_style = styles["Italic"]

    story: list[Any] = [
        Paragraph("Critical Thinking Analysis Report", title_style),
        Paragraph(datetime.utcnow().strftime("Generated on %B %d, %Y"), subtitle_style),
        Spacer(1, 18),
    ]

    for agent in AGENT_CONFIGS:
        key = agent["key"]
        block = analysis.get(key, {})
        content = block.get("content") or "Analysis unavailable."
        story.append(Paragraph(f"{agent['label']} Agent", styles["Heading2"]))
        story.append(Paragraph(agent["focus"].capitalize(), styles["BodyText"]))
        story.append(Spacer(1, 8))
        story.extend(_markdown_to_blocks(content))
        story.append(Spacer(1, 18))

    doc.build(story)
    pdf_bytes = buffer.getvalue()
    buffer.close()
    return pdf_bytes


@app.post("/generate-pdf")
async def generate_pdf(payload: dict[str, Any]) -> StreamingResponse:
    analysis = payload.get("analysis") if isinstance(payload, dict) else None
    if not isinstance(analysis, dict):
        raise HTTPException(status_code=400, detail="Analysis data missing.")
    pdf_bytes = _build_pdf(analysis)
    headers = {"Content-Disposition": "attachment; filename=CriticalThinkingReport.pdf"}
    return StreamingResponse(io.BytesIO(pdf_bytes), media_type="application/pdf", headers=headers)
