import asyncio
import io
import os
from datetime import datetime
from typing import Any
from pathlib import Path

import re
from reportlab.lib.units import inch
from reportlab.platypus import (
    PageBreak,
    Table,
    TableStyle,
    HRFlowable,
    KeepTogether,
)

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

MAX_TEXT_CHARS = 200_000
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

_BACK_MATTER_MARKERS = [
    "references",
    "bibliography",
    "reference list",
    "works cited",
    "literature cited",
    "citations",
    "appendix",
    "appendices",
    "supplementary material",
    "supplementary materials",
    "supplement",
    "acknowledgement",
    "acknowledgements",
]

def _reduce_academic_pdf_text(text: str) -> str:
    """
    Heuristically remove common academic back-matter (References/Bibliography/Appendix/etc.)
    to reduce token/character load before LLM analysis.

    Heuristic safeguards:
    - Only cut if the marker occurs after a threshold of the document (to avoid TOC hits).
    - Prefer the earliest qualifying marker among candidates.
    """
    if not text:
        return ""

    t = text

    # Normalize whitespace (helps pattern matching and reduces noise)
    t = t.replace("\r\n", "\n").replace("\r", "\n")
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\n{3,}", "\n\n", t).strip()

    lower = t.lower()
    n = len(lower)
    if n < 5000:
        # very short docs: do not cut
        return t

    # Only consider cut points that appear in the latter part of the document.
    # This avoids false positives from "References" in Table of Contents.
    threshold = int(0.45 * n)

    best_cut = None

    # Look for headings on their own line: \nReferences\n, \nAppendix\n, etc.
    for marker in _BACK_MATTER_MARKERS:
        # Match marker as a standalone heading line (case-insensitive via lower)
        # Examples matched:
        #   "\nreferences\n"
        #   "\nreferences:\n"
        #   "\nappendix a\n"
        pattern = re.compile(rf"\n{re.escape(marker)}(\b|[ :])")
        m = pattern.search(lower)
        if m:
            cut_idx = m.start()
            if cut_idx >= threshold:
                if best_cut is None or cut_idx < best_cut:
                    best_cut = cut_idx

    if best_cut is not None:
        t = t[:best_cut].strip()

    # Final cleanup
    t = re.sub(r"\n{3,}", "\n\n", t).strip()
    return t


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
        extracted = _reduce_academic_pdf_text(extracted)
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


_MD_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_MD_BULLET_RE = re.compile(r"^(\s*)([-*])\s+(.*)$")

def _md_inline_to_rl(text: str) -> str:
    """
    Convert a small, safe subset of Markdown inline formatting to ReportLab's
    Paragraph markup (HTML-ish): **bold**, *italic*, `code`.
    """
    if not text:
        return ""

    # Escape basic XML chars first
    text = (
        text.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
    )

    # Inline code
    text = re.sub(r"`([^`]+)`", r'<font face="Courier">\1</font>', text)

    # Bold **...**
    text = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", text)

    # Italic *...* (avoid clobbering bullet markers and already-converted tags)
    text = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"<i>\1</i>", text)

    return text


def _markdown_to_flowables(markdown_text: str) -> list[Any]:
    """
    Convert agent markdown-ish output into a clean flowable list:
    - headings (#, ##, ###)
    - paragraphs (reflow wrapped lines)
    - bullet lists (-, *)
    """
    styles = getSampleStyleSheet()

    body = ParagraphStyle(
        "CTBody",
        parent=styles["BodyText"],
        fontName="Helvetica",
        fontSize=10.5,
        leading=14,
        spaceAfter=8,
    )
    h1 = ParagraphStyle(
        "CTH1",
        parent=styles["Heading1"],
        fontName="Helvetica-Bold",
        fontSize=16,
        leading=20,
        spaceBefore=12,
        spaceAfter=8,
        textColor=colors.HexColor("#111827"),
    )
    h2 = ParagraphStyle(
        "CTH2",
        parent=styles["Heading2"],
        fontName="Helvetica-Bold",
        fontSize=13,
        leading=16,
        spaceBefore=12,
        spaceAfter=6,
        textColor=colors.HexColor("#111827"),
    )
    h3 = ParagraphStyle(
        "CTH3",
        parent=styles["Heading3"],
        fontName="Helvetica-Bold",
        fontSize=11.5,
        leading=14,
        spaceBefore=10,
        spaceAfter=4,
        textColor=colors.HexColor("#111827"),
    )
    bullet_style = ParagraphStyle(
        "CTBullet",
        parent=body,
        leftIndent=18,
        firstLineIndent=-8,
        spaceAfter=4,
    )

    def flush_paragraph(buf: list[str], out: list[Any]) -> None:
        if not buf:
            return
        text = " ".join(s.strip() for s in buf).strip()
        if text:
            out.append(Paragraph(_md_inline_to_rl(text), body))
        buf.clear()

    out: list[Any] = []
    lines = (markdown_text or "").splitlines()

    paragraph_buf: list[str] = []
    list_items: list[ListItem] = []

    def flush_list() -> None:
        nonlocal list_items
        if list_items:
            out.append(ListFlowable(list_items, bulletType="bullet", leftIndent=14))
            list_items = []

    for raw in lines:
        line = raw.rstrip()
        stripped = line.strip()

        # blank line -> flush paragraph + list
        if not stripped:
            flush_paragraph(paragraph_buf, out)
            flush_list()
            continue

        # heading?
        m = _MD_HEADING_RE.match(stripped)
        if m:
            flush_paragraph(paragraph_buf, out)
            flush_list()
            level = len(m.group(1))
            text = _md_inline_to_rl(m.group(2).strip())
            if level <= 1:
                out.append(Paragraph(text, h1))
            elif level == 2:
                out.append(Paragraph(text, h2))
            else:
                out.append(Paragraph(text, h3))
            continue

        # bullet?
        mb = _MD_BULLET_RE.match(line)
        if mb:
            flush_paragraph(paragraph_buf, out)
            indent_spaces = len(mb.group(1) or "")
            item_text = _md_inline_to_rl(mb.group(3).strip())

            # simple nesting by indent
            left_indent = 18 + (indent_spaces // 2) * 10
            nested_style = ParagraphStyle(
                f"CTBullet_{left_indent}",
                parent=bullet_style,
                leftIndent=left_indent,
            )
            list_items.append(ListItem(Paragraph(item_text, nested_style)))
            continue

        # normal text -> accumulate into reflowed paragraph
        paragraph_buf.append(stripped)

    flush_paragraph(paragraph_buf, out)
    flush_list()

    return out


def _draw_header_footer(canvas, doc):
    canvas.saveState()
    canvas.setFont("Helvetica", 9)
    canvas.setFillColor(colors.HexColor("#6B7280"))

    # Footer: page number
    page_num = canvas.getPageNumber()
    canvas.drawRightString(doc.pagesize[0] - doc.rightMargin, 0.55 * inch, f"Page {page_num}")

    # Subtle footer line
    canvas.setStrokeColor(colors.HexColor("#E5E7EB"))
    canvas.setLineWidth(0.5)
    canvas.line(doc.leftMargin, 0.75 * inch, doc.pagesize[0] - doc.rightMargin, 0.75 * inch)

    canvas.restoreState()


def _agent_header_card(agent: dict[str, Any]) -> Table:
    """
    A simple 'card' with agent label + focus.
    """
    label = agent.get("label", "Agent")
    focus = agent.get("focus", "")

    data = [
        [Paragraph(f"<b>{_md_inline_to_rl(label)} Agent</b>", getSampleStyleSheet()["BodyText"]),
         Paragraph(_md_inline_to_rl(focus), getSampleStyleSheet()["BodyText"])]
    ]
    t = Table(data, colWidths=[2.1 * inch, 4.9 * inch])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#F3F4F6")),
        ("BOX", (0, 0), (-1, -1), 0.6, colors.HexColor("#E5E7EB")),
        ("INNERPADDING", (0, 0), (-1, -1), 10),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ]))
    return t


def _build_pdf(analysis: dict[str, Any]) -> bytes:
    buffer = io.BytesIO()

    doc = SimpleDocTemplate(
        buffer,
        pagesize=LETTER,
        leftMargin=0.85 * inch,
        rightMargin=0.85 * inch,
        topMargin=0.9 * inch,
        bottomMargin=0.9 * inch,
        title="Critical Thinking Analysis Report",
        author="Critical Thinker",
    )

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "CTTitle",
        parent=styles["Title"],
        fontName="Helvetica-Bold",
        fontSize=22,
        leading=26,
        textColor=colors.HexColor("#111827"),
        spaceAfter=10,
    )
    subtitle_style = ParagraphStyle(
        "CTSubtitle",
        parent=styles["BodyText"],
        fontName="Helvetica",
        fontSize=11,
        leading=14,
        textColor=colors.HexColor("#374151"),
        spaceAfter=18,
    )
    meta_style = ParagraphStyle(
        "CTMeta",
        parent=styles["BodyText"],
        fontName="Helvetica",
        fontSize=9.5,
        leading=12,
        textColor=colors.HexColor("#6B7280"),
        spaceAfter=6,
    )

    story: list[Any] = []

    # --- Cover page ---
    story.append(Paragraph("Critical Thinking Analysis Report", title_style))
    story.append(Paragraph(datetime.utcnow().strftime("Generated on %B %d, %Y"), subtitle_style))

    # quick index of sections
    section_list = "<br/>".join([f"• {a.get('label','Agent')} Agent" for a in AGENT_CONFIGS])
    story.append(Paragraph(f"<b>Sections</b><br/>{section_list}", meta_style))

    story.append(Spacer(1, 16))
    story.append(Paragraph(
        "This report compiles independent analyses from multiple agents using Paul & Elder’s critical thinking framework. "
        "Each section is self-contained and may be read independently.",
        subtitle_style
    ))

    story.append(PageBreak())

    # --- Agent sections ---
    for idx, agent in enumerate(AGENT_CONFIGS):
        key = agent["key"]
        block = analysis.get(key, {}) if isinstance(analysis, dict) else {}
        content = block.get("content") or "Analysis unavailable."

        # section header card + divider
        story.append(KeepTogether([
            _agent_header_card(agent),
            Spacer(1, 10),
            HRFlowable(width="100%", thickness=0.7, color=colors.HexColor("#E5E7EB")),
            Spacer(1, 10),
        ]))

        # render markdown nicely
        story.extend(_markdown_to_flowables(content))

        # page break between agents (but not after last)
        if idx < len(AGENT_CONFIGS) - 1:
            story.append(PageBreak())

    doc.build(story, onFirstPage=_draw_header_footer, onLaterPages=_draw_header_footer)
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
