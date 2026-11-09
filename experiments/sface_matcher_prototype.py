"""Isolated SFace/YuNet prototype; not used by the production login route.

The model files are available from OpenCV Zoo:
https://github.com/opencv/opencv_zoo/tree/main/models/face_recognition_sface
https://github.com/opencv/opencv_zoo/tree/main/models/face_detection_yunet

The SFace and YuNet directories declare Apache-2.0 and MIT licenses,
respectively. This prototype reports cosine similarity only; its score is not
calibrated for ExamGuard and must not be used as an identity decision threshold.
"""

import argparse
import sys
from pathlib import Path

import cv2

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.sface_matcher import SFaceMatcherPrototype


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Prototype-only SFace cosine similarity for two local images."
    )
    parser.add_argument("--yunet-model", required=True, type=Path)
    parser.add_argument("--sface-model", required=True, type=Path)
    parser.add_argument("--image-a", required=True, type=Path)
    parser.add_argument("--image-b", required=True, type=Path)
    args = parser.parse_args()

    image_a = cv2.imread(str(args.image_a))
    image_b = cv2.imread(str(args.image_b))
    if image_a is None:
        raise ValueError(f"Could not decode image: {args.image_a}")
    if image_b is None:
        raise ValueError(f"Could not decode image: {args.image_b}")

    matcher = SFaceMatcherPrototype(args.yunet_model, args.sface_model)
    score = matcher.compare(image_a, image_b)
    print(f"cosine_similarity={score:.6f}")
    print("Prototype score only; no identity threshold has been calibrated.")


if __name__ == "__main__":
    main()
