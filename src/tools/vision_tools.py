"""Vision tools for diagram analysis in the VisionInspector detective node.

Covers rubric dimension: swarm_visual.

Each function exists in two forms:
- The raw function (e.g. extract_images_from_pdf, analyze_diagram) — deterministic.
- The @tool wrapper (extract_and_analyze_diagrams) — LangChain tool for LLM agent loops.
"""

import base64
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Optional


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------

DiagramClass = Literal[
    "accurate_stategraph",
    "sequence_diagram",
    "generic_flowchart",
    "linear_pipeline",
    "other",
]


@dataclass
class ExtractedImage:
    image_bytes: bytes
    page_number: int
    image_index: int


@dataclass
class DiagramAnalysisResult:
    classification: DiagramClass
    description: str
    has_parallel_branches: bool
    confidence: float = 0.0
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# extract_images_from_pdf
# ---------------------------------------------------------------------------


def extract_images_from_pdf(path: str | Path) -> list[ExtractedImage]:
    """Extract embedded images from a PDF using PyMuPDF.

    Returns an empty list (not an error) when no images are found.
    """
    try:
        import fitz  # PyMuPDF
    except ImportError:
        return []

    pdf_path = Path(path)
    if not pdf_path.exists():
        return []

    try:
        doc = fitz.open(str(pdf_path))
    except Exception:
        return []

    images: list[ExtractedImage] = []
    for page_num in range(doc.page_count):
        page = doc[page_num]
        for img_idx, img_info in enumerate(page.get_images(full=True)):
            xref = img_info[0]
            try:
                base_image = doc.extract_image(xref)
                img_bytes = base_image["image"]
                images.append(
                    ExtractedImage(
                        image_bytes=img_bytes,
                        page_number=page_num + 1,
                        image_index=img_idx,
                    )
                )
            except Exception:
                continue

    return images


# ---------------------------------------------------------------------------
# analyze_diagram — multimodal LLM call
# ---------------------------------------------------------------------------

_DIAGRAM_PROMPT = """You are an expert LangGraph architecture reviewer.

Examine this diagram and answer:
1. Does it show a LangGraph StateGraph with parallel branches?
2. Does it show the expected flow: START → [Detectives in parallel] → Evidence Aggregation → [Judges in parallel] → Chief Justice → END?
3. Or is it a simple linear flowchart / sequence diagram / other?

Classify as exactly one of:
- accurate_stategraph   (shows parallel fan-out/fan-in StateGraph nodes)
- sequence_diagram      (UML sequence / step-by-step arrows)
- generic_flowchart     (flowchart but no parallel branches shown)
- linear_pipeline       (strictly linear, no parallelism)
- other

Respond in this exact JSON format:
{
  "classification": "<one of the five classes>",
  "description": "<one sentence describing what you see>",
  "has_parallel_branches": <true|false>,
  "confidence": <0.0-1.0>
}"""


def analyze_diagram(
    image_bytes: bytes,
    *,
    enabled: bool = True,
) -> DiagramAnalysisResult:
    """Send an image to a multimodal LLM for diagram classification.

    Falls back gracefully if the model is not configured or `enabled=False`.
    """
    if not enabled:
        return DiagramAnalysisResult(
            classification="other",
            description="Vision analysis disabled.",
            has_parallel_branches=False,
            confidence=0.0,
        )

    # Prefer Anthropic Claude (already a dep); fall back to OpenAI GPT-4o
    anthropic_key = os.getenv("ANTHROPIC_API_KEY") or os.getenv("AI_MODEL_PROVIDER_API_KEY")
    openai_key = os.getenv("OPENAI_API_KEY")

    if anthropic_key:
        return _analyze_with_anthropic(image_bytes, anthropic_key)
    elif openai_key:
        return _analyze_with_openai(image_bytes, openai_key)
    else:
        return DiagramAnalysisResult(
            classification="other",
            description="No vision model API key configured.",
            has_parallel_branches=False,
            confidence=0.0,
            error="no_api_key",
        )


def _analyze_with_anthropic(image_bytes: bytes, api_key: str) -> DiagramAnalysisResult:
    import json

    try:
        import anthropic

        client = anthropic.Anthropic(api_key=api_key)
        b64 = base64.standard_b64encode(image_bytes).decode()
        message = client.messages.create(
            model="claude-3-5-haiku-latest",
            max_tokens=512,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/png",
                                "data": b64,
                            },
                        },
                        {"type": "text", "text": _DIAGRAM_PROMPT},
                    ],
                }
            ],
        )
        raw = message.content[0].text.strip()
        # Extract JSON even if model adds surrounding text
        json_match = re.search(r"\{.*\}", raw, re.DOTALL)
        if json_match:
            data = json.loads(json_match.group(0))
            return DiagramAnalysisResult(
                classification=data.get("classification", "other"),
                description=data.get("description", ""),
                has_parallel_branches=bool(data.get("has_parallel_branches", False)),
                confidence=float(data.get("confidence", 0.5)),
            )
    except Exception as exc:
        return DiagramAnalysisResult(
            classification="other",
            description="",
            has_parallel_branches=False,
            confidence=0.0,
            error=f"anthropic_error: {exc}",
        )
    return DiagramAnalysisResult(
        classification="other",
        description="Parse error",
        has_parallel_branches=False,
        confidence=0.0,
    )


def _analyze_with_openai(image_bytes: bytes, api_key: str) -> DiagramAnalysisResult:
    import json
    import re

    try:
        from openai import OpenAI

        client = OpenAI(api_key=api_key)
        b64 = base64.standard_b64encode(image_bytes).decode()
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            max_tokens=512,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/png;base64,{b64}"},
                        },
                        {"type": "text", "text": _DIAGRAM_PROMPT},
                    ],
                }
            ],
        )
        raw = response.choices[0].message.content or ""
        json_match = re.search(r"\{.*\}", raw, re.DOTALL)
        if json_match:
            data = json.loads(json_match.group(0))
            return DiagramAnalysisResult(
                classification=data.get("classification", "other"),
                description=data.get("description", ""),
                has_parallel_branches=bool(data.get("has_parallel_branches", False)),
                confidence=float(data.get("confidence", 0.5)),
            )
    except Exception as exc:
        return DiagramAnalysisResult(
            classification="other",
            description="",
            has_parallel_branches=False,
            confidence=0.0,
            error=f"openai_error: {exc}",
        )
    return DiagramAnalysisResult(
        classification="other",
        description="Parse error",
        has_parallel_branches=False,
        confidence=0.0,
    )


import re  # noqa: E402  (used inside _analyze_with_anthropic above)


# ---------------------------------------------------------------------------
# LangChain tool wrapper — used by LLM detective agents
# ---------------------------------------------------------------------------

from langchain_core.tools import tool  # noqa: E402


@tool
def extract_and_analyze_diagrams(pdf_path: str) -> dict:
    """Extract images from a PDF and analyze each for architectural diagram accuracy.

    Classifies diagrams and checks whether they accurately show a parallel LangGraph
    StateGraph (fan-out detectives, fan-in aggregator, fan-out judges, chief justice).
    Returns the best-matching classification and confidence across all diagrams found.
    """
    images = extract_images_from_pdf(pdf_path)
    if not images:
        return {"ok": False, "images_found": 0, "error": "No images found in PDF"}

    analyzed = []
    best_confidence = -1.0
    best: DiagramAnalysisResult | None = None

    for img in images[:5]:
        result = analyze_diagram(img.image_bytes)
        analyzed.append({
            "page": img.page_number,
            "classification": result.classification,
            "description": result.description,
            "has_parallel_branches": result.has_parallel_branches,
            "confidence": result.confidence,
            "error": result.error,
        })
        if result.confidence > best_confidence:
            best_confidence = result.confidence
            best = result

    return {
        "ok": True,
        "images_found": len(images),
        "diagrams_analyzed": len(analyzed),
        "best_classification": best.classification if best else None,
        "best_description": best.description if best else None,
        "best_confidence": best_confidence,
        "has_parallel_branches": best.has_parallel_branches if best else False,
        "all_results": analyzed,
    }
