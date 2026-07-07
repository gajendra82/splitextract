"""Configurable image preprocessing pipeline for OCR."""

import logging
from typing import Optional

import cv2
import numpy as np

from services.config import EXTRACTION_CONFIG, PreprocessingConfig

logger = logging.getLogger(__name__)

try:
    import pytesseract
    from PIL import Image as PILImage
    _OSD_AVAILABLE = True
except ImportError:
    _OSD_AVAILABLE = False


class ImagePreprocessor:
    """Apply configurable preprocessing steps before Tesseract OCR."""

    def __init__(self, config: Optional[PreprocessingConfig] = None):
        self.config = config or EXTRACTION_CONFIG.preprocessing

    def preprocess(self, image_bgr: np.ndarray) -> np.ndarray:
        """Run enabled preprocessing steps and return processed BGR image."""
        if image_bgr is None or image_bgr.size == 0:
            return image_bgr

        result = image_bgr.copy()

        if self.config.enable_dpi_normalization:
            result = self._normalize_dpi(result)

        if self.config.enable_auto_rotation or self.config.enable_orientation_detection:
            result = self._auto_orient(result)

        if self.config.enable_deskew:
            result = self._deskew(result)

        if self.config.enable_shadow_removal:
            result = self._remove_shadows(result)

        if self.config.enable_grayscale:
            gray = cv2.cvtColor(result, cv2.COLOR_BGR2GRAY)
        else:
            gray = cv2.cvtColor(result, cv2.COLOR_BGR2GRAY)

        if self.config.enable_noise_removal:
            gray = cv2.fastNlMeansDenoising(gray, None, 10, 7, 21)

        if self.config.enable_contrast_enhancement:
            gray = cv2.equalizeHist(gray)

        if self.config.enable_adaptive_threshold:
            gray = cv2.adaptiveThreshold(
                gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 31, 11
            )
        else:
            _, gray = cv2.threshold(gray, 150, 255, cv2.THRESH_BINARY)

        if self.config.enable_sharpening:
            kernel = np.array([[0, -1, 0], [-1, 5, -1], [0, -1, 0]])
            gray = cv2.filter2D(gray, -1, kernel)

        return cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)

    def _normalize_dpi(self, image_bgr: np.ndarray) -> np.ndarray:
        """Upscale small images toward target DPI equivalent."""
        target = max(self.config.target_dpi, 72)
        h, w = image_bgr.shape[:2]
        # Assume source ~72 DPI when rendered from PDF at 2.5x (~180 DPI effective)
        scale = target / 180.0
        if scale <= 1.05:
            return image_bgr
        new_w = int(w * scale)
        new_h = int(h * scale)
        return cv2.resize(image_bgr, (new_w, new_h), interpolation=cv2.INTER_CUBIC)

    def _auto_orient(self, image_bgr: np.ndarray) -> np.ndarray:
        if not _OSD_AVAILABLE:
            return image_bgr
        try:
            rgb = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2RGB)
            pil_img = PILImage.fromarray(rgb)
            osd = pytesseract.image_to_osd(pil_img)
            rotation = 0
            for line in osd.splitlines():
                if line.startswith("Rotate:"):
                    rotation = int(line.split(":", 1)[1].strip())
                    break
            if rotation in {90, 180, 270}:
                rot_map = {
                    90: cv2.ROTATE_90_CLOCKWISE,
                    180: cv2.ROTATE_180,
                    270: cv2.ROTATE_90_COUNTERCLOCKWISE,
                }
                return cv2.rotate(image_bgr, rot_map[rotation])
        except Exception as exc:
            logger.debug(f"Orientation detection skipped: {exc}")
        return image_bgr

    def _deskew(self, image_bgr: np.ndarray) -> np.ndarray:
        try:
            gray = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2GRAY)
            gray = cv2.bitwise_not(gray)
            thresh = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)[1]
            coords = np.column_stack(np.where(thresh > 0))
            if coords.size == 0:
                return image_bgr
            angle = cv2.minAreaRect(coords)[-1]
            if angle < -45:
                angle = -(90 + angle)
            else:
                angle = -angle
            if abs(angle) < 0.5 or abs(angle) > 15:
                return image_bgr
            h, w = image_bgr.shape[:2]
            matrix = cv2.getRotationMatrix2D((w // 2, h // 2), angle, 1.0)
            return cv2.warpAffine(
                image_bgr, matrix, (w, h), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE
            )
        except Exception as exc:
            logger.debug(f"Deskew skipped: {exc}")
            return image_bgr

    def _remove_shadows(self, image_bgr: np.ndarray) -> np.ndarray:
        try:
            rgb_planes = cv2.split(image_bgr)
            result_planes = []
            for plane in rgb_planes:
                dilated = cv2.dilate(plane, np.ones((7, 7), np.uint8))
                bg = cv2.medianBlur(dilated, 21)
                diff = 255 - cv2.absdiff(plane, bg)
                norm = cv2.normalize(diff, None, alpha=0, beta=255, norm_type=cv2.NORM_MINMAX)
                result_planes.append(norm)
            return cv2.merge(result_planes)
        except Exception as exc:
            logger.debug(f"Shadow removal skipped: {exc}")
            return image_bgr
