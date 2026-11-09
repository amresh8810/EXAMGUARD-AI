"""Local-only SFace webcam smoke check; never saves or uploads camera frames."""

import sys
import tempfile
from pathlib import Path

import cv2
import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.sface_matcher import SFaceMatcherPrototype, ensure_model_files


def capture_samples(camera: cv2.VideoCapture, include_second_person: bool) -> list[np.ndarray]:
    samples = []
    labels = [
        "Participant 1 - capture 1/2",
        "Participant 1 - change angle, capture 2/2",
    ]
    if include_second_person:
        labels.extend(
            (
                "Participant 2 - capture 1/2",
                "Participant 2 - change angle, capture 2/2",
            )
        )
    window = "ExamGuard local SFace test"
    cv2.namedWindow(window)
    try:
        for label in labels:
            while True:
                ok, frame = camera.read()
                if not ok or frame is None:
                    raise RuntimeError("Could not read a frame from the local webcam.")
                preview = frame.copy()
                cv2.putText(
                    preview,
                    f"{label} | C: capture, Q: quit",
                    (12, 30),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.65,
                    (0, 255, 0),
                    2,
                    cv2.LINE_AA,
                )
                cv2.imshow(window, preview)
                key = cv2.waitKey(1) & 0xFF
                if key == ord("q"):
                    raise RuntimeError("Test cancelled.")
                if key == ord("c"):
                    samples.append(frame.copy())
                    break
    finally:
        cv2.destroyWindow(window)
    return samples


def main() -> None:
    print("Local-only test: webcam frames stay in memory and are not saved or uploaded.")
    print("Use only with consent from each participant; one person at a time in frame.")
    choice = input(
        "Type 1 for a one-person consistency test, 2 for two consenting people, "
        "or anything else to cancel: "
    ).strip()
    if choice not in {"1", "2"}:
        print("Test cancelled before opening the webcam.")
        return
    if input("Type YES to open the webcam, or anything else to cancel: ").strip() != "YES":
        print("Test cancelled before opening the webcam.")
        return

    include_second_person = choice == "2"
    camera = cv2.VideoCapture(0)
    if not camera.isOpened():
        camera.release()
        raise RuntimeError("Could not open the default webcam.")

    samples = []
    try:
        samples = capture_samples(camera, include_second_person)
    finally:
        camera.release()
        cv2.destroyAllWindows()

    try:
        with tempfile.TemporaryDirectory(prefix="examguard-sface-") as temp_dir:
            model_paths = ensure_model_files(Path(temp_dir))
            matcher = SFaceMatcherPrototype(*model_paths)
            embeddings = [matcher.embed(sample) for sample in samples]

        same_person_scores = [float(np.dot(embeddings[0], embeddings[1]))]
        print("Same-person cosine scores:", ", ".join(f"{s:.4f}" for s in same_person_scores))
        if include_second_person:
            different_person_scores = [
                float(np.dot(embeddings[a], embeddings[b]))
                for a in (0, 1)
                for b in (2, 3)
            ]
            print(
                "Different-person cosine scores:",
                ", ".join(f"{s:.4f}" for s in different_person_scores),
            )
        else:
            print("No different-person comparison was performed.")
        print("Scores only: no pass/fail threshold or accuracy claim is calibrated.")
    finally:
        samples.clear()
        if "embeddings" in locals():
            embeddings.clear()


if __name__ == "__main__":
    main()
