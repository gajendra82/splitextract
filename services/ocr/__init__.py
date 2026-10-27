"""OCR adapters. Engines produce evidence; they do not decide business values."""

from services.ocr.base import OCREngine, OCRResult, OCRWord
from services.ocr.contract import OCRDocumentEvidence, OCRPageEvidence
from services.ocr.orchestrator import OCROrchestrator, ocr_settings
from services.ocr.paddle_vl_engine import PaddleVLEngine
from services.ocr.runtime import get_orchestrator, ocr_image_text, recognize_image
from services.ocr.surya_engine import SuryaEngine

__all__ = [
    "OCREngine",
    "OCRResult",
    "OCRWord",
    "OCRDocumentEvidence",
    "OCRPageEvidence",
    "OCROrchestrator",
    "ocr_settings",
    "PaddleVLEngine",
    "SuryaEngine",
    "get_orchestrator",
    "ocr_image_text",
    "recognize_image",
]
