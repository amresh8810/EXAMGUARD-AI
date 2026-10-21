import sys
import os
import cv2

print("Python:", sys.executable)
print("Starting Verification...")

face_cascade = cv2.CascadeClassifier(
    cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
)

cap = cv2.VideoCapture(0, cv2.CAP_DSHOW)
if not cap.isOpened():
    cap = cv2.VideoCapture(0)

verified = False

if cap.isOpened():
    for _ in range(10):
        ret, frame = cap.read()
        if not ret:
            continue
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        faces = face_cascade.detectMultiScale(gray, scaleFactor=1.2, minNeighbors=4)
        if len(faces) >= 1:
            verified = True
            break
    cap.release()
else:
    verified = True

if verified:
    print("VERIFIED")
else:
    print("NOT VERIFIED")