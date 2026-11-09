"""Experimental, opt-in SFace matcher for local login testing."""

import hashlib
import os
import tempfile
import threading
import urllib.request
from pathlib import Path

import cv2
import numpy as np


_MODEL_SPECS = (
    (
        "face_detection_yunet_2023mar.onnx",
        "https://github.com/opencv/opencv_zoo/raw/refs/heads/main/"
        "models/face_detection_yunet/face_detection_yunet_2023mar.onnx",
        232589,
        "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4",
    ),
    (
        "face_recognition_sface_2021dec.onnx",
        "https://github.com/opencv/opencv_zoo/raw/refs/heads/main/"
        "models/face_recognition_sface/face_recognition_sface_2021dec.onnx",
        38696353,
        "0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79",
    ),
)
# OpenCV Zoo's example threshold; it is not calibrated for ExamGuard candidates.
_COSINE_MATCH_THRESHOLD = 0.363
_MODEL_LOCK = threading.Lock()
_MATCHER_LOCK = threading.Lock()
_INFERENCE_LOCK = threading.Lock()
_MATCHER = None


class SFaceMatcherPrototype:
    def __init__(self, yunet_model: Path, sface_model: Path):
        if not yunet_model.is_file():
            raise FileNotFoundError(f"YuNet model not found: {yunet_model}")
        if not sface_model.is_file():
            raise FileNotFoundError(f"SFace model not found: {sface_model}")

        self.detector = cv2.FaceDetectorYN_create(
            str(yunet_model), "", (320, 320), 0.9, 0.3, 500
        )
        self.recognizer = cv2.FaceRecognizerSF_create(str(sface_model), "")

    def embed(self, image: np.ndarray) -> np.ndarray:
        if (
            not isinstance(image, np.ndarray)
            or image.ndim != 3
            or image.shape[2] != 3
        ):
            raise ValueError("Expected a decoded BGR image with three color channels.")
        if image.shape[0] == 0 or image.shape[1] == 0:
            raise ValueError("Image must have non-zero width and height.")

        height, width = image.shape[:2]
        self.detector.setInputSize((width, height))
        _, faces = self.detector.detect(image)
        if faces is None or len(faces) != 1:
            face_count = 0 if faces is None else len(faces)
            raise ValueError(f"Expected exactly one face; detected {face_count}.")

        aligned_face = self.recognizer.alignCrop(image, faces[0])
        embedding = self.recognizer.feature(aligned_face).reshape(-1).astype(np.float32)
        norm = float(np.linalg.norm(embedding))
        if not np.isfinite(norm) or norm == 0:
            raise RuntimeError("SFace returned an invalid embedding.")
        return embedding / norm

    def compare(self, image_a: np.ndarray, image_b: np.ndarray) -> float:
        embedding_a = self.embed(image_a)
        embedding_b = self.embed(image_b)
        return float(np.dot(embedding_a, embedding_b))


def _has_expected_content(path: Path, expected_size: int, expected_sha256: str) -> bool:
    if not path.is_file() or path.stat().st_size != expected_size:
        return False
    digest = hashlib.sha256()
    with path.open("rb") as model_file:
        for chunk in iter(lambda: model_file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest() == expected_sha256


def ensure_model_files(directory: Path | None = None) -> tuple[Path, Path]:
    if directory is None:
        configured_dir = os.environ.get("EXAMGUARD_SFACE_MODEL_DIR")
        directory = (
            Path(configured_dir)
            if configured_dir
            else Path(tempfile.gettempdir()) / "examguard-sface-models"
        )
    directory.mkdir(parents=True, exist_ok=True)

    model_paths = []
    with _MODEL_LOCK:
        for name, url, expected_size, expected_sha256 in _MODEL_SPECS:
            path = directory / name
            if not _has_expected_content(path, expected_size, expected_sha256):
                request = urllib.request.Request(
                    url,
                    headers={"User-Agent": "ExamGuard-local-sface-test"},
                )
                temporary_path = None
                digest = hashlib.sha256()
                downloaded = 0
                try:
                    with tempfile.NamedTemporaryFile(
                        mode="wb",
                        prefix=f"{name}.",
                        suffix=".part",
                        dir=directory,
                        delete=False,
                    ) as model_file:
                        temporary_path = Path(model_file.name)
                        with urllib.request.urlopen(request, timeout=90) as response:
                            while chunk := response.read(1024 * 1024):
                                downloaded += len(chunk)
                                if downloaded > expected_size:
                                    raise ValueError(
                                        f"Unexpected model size for {name}."
                                    )
                                digest.update(chunk)
                                model_file.write(chunk)
                    if (
                        downloaded != expected_size
                        or digest.hexdigest() != expected_sha256
                    ):
                        raise ValueError(f"Model integrity check failed for {name}.")
                    temporary_path.replace(path)
                finally:
                    if temporary_path is not None and temporary_path.exists():
                        temporary_path.unlink()
            model_paths.append(path)
    return model_paths[0], model_paths[1]


def sface_matches(frame: np.ndarray, reference: np.ndarray | str) -> bool:
    if isinstance(reference, str):
        reference = cv2.imread(reference)
    if reference is None:
        raise ValueError("The saved face image could not be decoded.")

    global _MATCHER
    if _MATCHER is None:
        with _MATCHER_LOCK:
            if _MATCHER is None:
                _MATCHER = SFaceMatcherPrototype(*ensure_model_files())

    with _INFERENCE_LOCK:
        score = _MATCHER.compare(frame, reference)
    return score >= _COSINE_MATCH_THRESHOLD
