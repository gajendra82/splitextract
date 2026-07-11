"""Centralized configuration loaded from environment variables."""

import os
from dataclasses import dataclass, field
from typing import Dict

from dotenv import load_dotenv

load_dotenv()


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


@dataclass
class PreprocessingConfig:
    enable_auto_rotation: bool = field(
        default_factory=lambda: _env_bool("PREPROC_AUTO_ROTATION", True)
    )
    enable_orientation_detection: bool = field(
        default_factory=lambda: _env_bool("PREPROC_ORIENTATION_DETECTION", True)
    )
    enable_deskew: bool = field(
        default_factory=lambda: _env_bool("PREPROC_DESKEW", True)
    )
    enable_grayscale: bool = field(
        default_factory=lambda: _env_bool("PREPROC_GRAYSCALE", True)
    )
    enable_adaptive_threshold: bool = field(
        default_factory=lambda: _env_bool("PREPROC_ADAPTIVE_THRESHOLD", True)
    )
    enable_noise_removal: bool = field(
        default_factory=lambda: _env_bool("PREPROC_NOISE_REMOVAL", True)
    )
    enable_dpi_normalization: bool = field(
        default_factory=lambda: _env_bool("PREPROC_DPI_NORMALIZATION", True)
    )
    target_dpi: int = field(
        default_factory=lambda: _env_int("PREPROC_TARGET_DPI", 300)
    )
    enable_contrast_enhancement: bool = field(
        default_factory=lambda: _env_bool("PREPROC_CONTRAST_ENHANCEMENT", True)
    )
    enable_sharpening: bool = field(
        default_factory=lambda: _env_bool("PREPROC_SHARPENING", True)
    )
    enable_shadow_removal: bool = field(
        default_factory=lambda: _env_bool("PREPROC_SHADOW_REMOVAL", True)
    )


@dataclass
class ExtractionConfig:
    # OCR quality thresholds
    ocr_confidence_threshold: float = field(
        default_factory=lambda: _env_float("OCR_CONFIDENCE_THRESHOLD", 80.0)
    )
    ocr_min_text_length: int = field(
        default_factory=lambda: _env_int("OCR_MIN_TEXT_LENGTH", 500)
    )
    ocr_min_readable_ratio: float = field(
        default_factory=lambda: _env_float("OCR_MIN_READABLE_RATIO", 0.85)
    )

    # LLM providers
    enable_text_model: bool = field(
        default_factory=lambda: _env_bool("ENABLE_TEXT_MODEL", True)
    )
    enable_vision_fallback: bool = field(
        default_factory=lambda: _env_bool("ENABLE_VISION_FALLBACK", True)
    )
    text_provider: str = field(
        default_factory=lambda: os.getenv("TEXT_PROVIDER", "openai").strip().lower()
    )
    text_model: str = field(
        default_factory=lambda: os.getenv("TEXT_MODEL", "gpt-5.5-mini").strip()
    )
    vision_provider: str = field(
        default_factory=lambda: os.getenv("VISION_PROVIDER", "gemini").strip().lower()
    )
    vision_model: str = field(
        default_factory=lambda: os.getenv("VISION_MODEL", "gemini-2.5-flash-lite").strip()
    )

    # Vertex AI Gemini
    google_genai_use_vertexai: bool = field(
        default_factory=lambda: _env_bool("GOOGLE_GENAI_USE_VERTEXAI", True)
    )
    google_cloud_project: str = field(
        default_factory=lambda: os.getenv(
            "GOOGLE_CLOUD_PROJECT", "pod-ocr-502015"
        ).strip()
    )
    google_cloud_location: str = field(
        default_factory=lambda: os.getenv(
            "GOOGLE_CLOUD_LOCATION", "global"
        ).strip()
    )
    google_application_credentials: str = field(
        default_factory=lambda: os.getenv(
            "GOOGLE_APPLICATION_CREDENTIALS", ""
        ).strip()
    )

    # GPT cost optimization (all default off — no behaviour change)
    enable_gpt_cache: bool = field(
        default_factory=lambda: _env_bool("ENABLE_GPT_CACHE", False)
    )
    enable_ocr_normalization: bool = field(
        default_factory=lambda: _env_bool("ENABLE_OCR_NORMALIZATION", False)
    )
    enable_gpt_token_logging: bool = field(
        default_factory=lambda: _env_bool("ENABLE_GPT_TOKEN_LOGGING", False)
    )
    gpt_cache_ttl_seconds: int = field(
        default_factory=lambda: _env_int("GPT_CACHE_TTL_SECONDS", 86400)
    )
    gpt_cache_dir: str = field(
        default_factory=lambda: os.getenv(
            "GPT_CACHE_DIR",
            os.path.join(os.getcwd(), ".gpt_cache"),
        ).strip()
    )

    openai_api_key: str = field(
        default_factory=lambda: os.getenv("OPENAI_API_KEY", "").strip()
    )

    # Retry / timeout
    text_retry_count: int = field(
        default_factory=lambda: _env_int("TEXT_RETRY_COUNT", 1)
    )
    text_timeout: int = field(
        default_factory=lambda: _env_int("TEXT_TIMEOUT", 120)
    )
    vision_timeout: int = field(
        default_factory=lambda: _env_int("VISION_TIMEOUT", 60)
    )

    # Cache
    enable_extraction_cache: bool = field(
        default_factory=lambda: _env_bool("ENABLE_EXTRACTION_CACHE", True)
    )
    cache_backend: str = field(
        default_factory=lambda: os.getenv("EXTRACTION_CACHE_BACKEND", "file").strip().lower()
    )
    cache_dir: str = field(
        default_factory=lambda: os.getenv(
            "EXTRACTION_CACHE_DIR", os.path.join(os.getcwd(), ".extraction_cache")
        ).strip()
    )
    cache_ttl_seconds: int = field(
        default_factory=lambda: _env_int("EXTRACTION_CACHE_TTL_SECONDS", 0)
    )

    preprocessing: PreprocessingConfig = field(default_factory=PreprocessingConfig)


# Singleton used across services
EXTRACTION_CONFIG = ExtractionConfig()
