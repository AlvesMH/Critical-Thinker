import asyncio
import io
import os
from datetime import datetime
from typing import Any
from pathlib import Path

import httpx
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse, FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pypdf import PdfReader
from reportlab.lib import colors
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.platypus import ListFlowable, ListItem, Paragraph, SimpleDocTemplate, Spacer

# FIX 1: import prompts from the same directory (matches uploaded prompts.py)
from backend.app.prompts import AGENT_CONFIGS

MAX_TEXT_CHARS = 100_000
MAX_PDF_BYTES = 10 * 1024 * 1024

DEFAULT_MODEL = os.getenv("GEMMA_MODEL", "aisingapore/Gemma-SEA-LION-v4-27B-IT")
GEMMA_API_URL = os.getenv("GEMMA_API_URL", "").strip()
GEMMA_API_KEY = os.getenv("GEMMA_API_KEY", "").strip()

app = FastAPI(title="Critical Thinking Analysis API")

# FIX 2: safer CORS defaults; do NOT combine allow_credentials=True with "*"
cors_origins_raw = os.getenv("CORS_ORIGINS", "*")
cors_origins = [o.strip() for o in cors_origins_raw.split(",") if o.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_credentials=False,  # important for "*" compatibility
    allow_methods=["*"],
    allow_headers=["*"],
)


def _extract_pdf_text(file_bytes: bytes) -> str:
    reader = PdfReader(io.BytesIO(file_bytes))
    parts: list[str] = []
    for page in reader.pages:
        parts.append(page.extract_text() or "")
    return "\n".join(parts).strip()


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
    if not GEMMA_API_URL:
        raise RuntimeError("GEMMA_API_URL is not set.")
    if not GEMMA_API_KEY:
        raise RuntimeError("GEMMA_API_KEY is not set.")

    payload = {
        "model": DEFAULT_MODEL,
        "messages": _build_prompt(agent, text),
        "temperature": 0.3,
    }
    headers = {"Authorization": f"Bearer {GEMMA_API_KEY}"}

    timeout = httpx.Timeout(120.0, connect=10.0)
    try:
        resp = await client.post(GEMMA_API_URL, json=payload, headers=headers, timeout=timeout)
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise RuntimeError(f"Model API returned HTTP {exc.response.status_code}.") from exc
    except httpx.RequestError as exc:
        raise RuntimeError("Could not reach the model API.") from exc

    data = resp.json()
    if "choices" in data:
        return data["choices"][0]["message"]["content"].strip()
    if "generated_text" in data:
        return data["generated_text"].strip()
    raise RuntimeError("Unexpected response from model API.")


async def _run_agents(text: str) -> dict[str, Any]:
    results: dict[str, Any] = {}
    async with httpx.AsyncClient() as client:
        tasks = [_call_model(client, agent, text) for agent in AGENT_CONFIGS]
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


@app.get("/api/health")
async def health_check() -> dict[str, str]:
    return {"status": "ok"}

@app.get("/api/_buildinfo")
async def buildinfo():
    return {
        "file": __file__,
        "cwd": os.getcwd(),
        "FRONTEND_DIST": os.getenv("FRONTEND_DIST", "frontend_dist"),
        "dist_resolved": str(dist_dir),
        "index_exists": (dist_dir / "index.html").exists(),
    }

@app.head("/")
async def head_root():
    return Response(status_code=200)
    
# FIX 3: remove Body() from signature; manually parse JSON when needed
@app.post("/api/analyze")
async def analyze(
    request: Request,
    text: str | None = Form(None),
    file: UploadFile | None = File(None),
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
        # JSON path (frontend posts application/json) OR form field "text"
        content_type = request.headers.get("content-type", "")
        if text is None and content_type.startswith("application/json"):
            try:
                body = await request.json()
            except Exception:
                body = None
            if isinstance(body, dict):
                text = body.get("text")

        if text is None:
            raise HTTPException(status_code=400, detail="Provide text or upload a PDF.")
        cleaned = _validate_text(text)

    results = await _run_agents(cleaned)
    if all(v.get("status") == "error" for v in results.values()):
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
    list_items: list[Any] = []
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


@app.post("/api/generate-pdf")
async def generate_pdf(payload: dict[str, Any]) -> StreamingResponse:
    analysis = payload.get("analysis") if isinstance(payload, dict) else None
    if not isinstance(analysis, dict):
        raise HTTPException(status_code=400, detail="Analysis data missing.")
    pdf_bytes = _build_pdf(analysis)
    headers = {"Content-Disposition": "attachment; filename=CriticalThinkingReport.pdf"}
    return StreamingResponse(io.BytesIO(pdf_bytes), media_type="application/pdf", headers=headers)

dist_dir = Path(os.getenv("FRONTEND_DIST", "frontend_dist")).resolve()
index_html = dist_dir / "index.html"
assets_dir = dist_dir / "assets"

if assets_dir.exists():
    app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")

@app.get("/", include_in_schema=False)
async def spa_index():
    if index_html.exists():
        return FileResponse(index_html)
    # If you still see JSON at / after this, you're not running this file.
    return JSONResponse(
        {"status": "ok", "note": "Frontend index.html not found", "FRONTEND_DIST": str(dist_dir)},
        status_code=200,
    )

@app.get("/{full_path:path}", include_in_schema=False)
async def spa_fallback(full_path: str):
    if full_path == "api" or full_path.startswith("api/"):
        raise HTTPException(status_code=404, detail="Not Found")

    candidate = dist_dir / full_path
    if candidate.is_file():
        return FileResponse(candidate)

    if index_html.exists():
        return FileResponse(index_html)

    raise HTTPException(status_code=404, detail="Not Found")
