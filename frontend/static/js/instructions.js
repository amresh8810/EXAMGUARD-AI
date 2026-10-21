const readinessChecks = {
    candidate_session: document.getElementById("candidateSessionCheck"),
    camera: document.getElementById("cameraCheck"),
    face: document.getElementById("faceCheck"),
    questions: document.getElementById("questionsCheck"),
    autosave: document.getElementById("autosaveCheck")
};
const readinessCamera = document.getElementById("readinessCamera");
const readinessMessage = document.getElementById("readinessMessage");
const readinessError = document.getElementById("readinessError");
const runReadinessBtn = document.getElementById("runReadinessBtn");
const startExamBtn = document.getElementById("startExamBtn");
let cameraStream = null;
let readinessRunning = false;

const readinessUiMissing = (
    !readinessCamera ||
    !runReadinessBtn ||
    !startExamBtn ||
    Object.values(readinessChecks).some(check => !check)
);

if (readinessUiMissing) {
    if (startExamBtn && !startExamBtn.disabled) {
        startExamBtn.addEventListener("click", () => {
            document.documentElement.requestFullscreen()
                .then(() => window.location.assign("/exam"))
                .catch(() => alert("Please allow fullscreen mode before starting the exam."));
        });
    } else if (startExamBtn) {
        startExamBtn.disabled = true;
    }
}

if (!readinessUiMissing) {
function setCheck(name, state, message) {
    const check = readinessChecks[name];
    check.dataset.state = state;
    check.querySelector("small").textContent = message;
}

function allChecksReady() {
    return Object.values(readinessChecks).every(
        check => check.dataset.state === "ready"
    );
}

function updateStartButton() {
    startExamBtn.disabled = !allChecksReady() || readinessRunning;
}

async function checkServerReadiness() {
    let response;
    let result;
    try {
        response = await fetch("/api/exam/readiness", {
            method: "POST",
            headers: {"Accept": "application/json"}
        });
        result = await response.json();
    } catch (error) {
        setCheck("candidate_session", "error", "Session could not be verified");
        setCheck("questions", "pending", "Not checked");
        setCheck("autosave", "pending", "Not checked");
        throw new Error("The readiness service could not be reached. Check your connection and retry.");
    }

    if (!response.ok) {
        if (result.check === "candidate_session") {
            setCheck("candidate_session", "error", "Sign-in session is missing");
            setCheck("questions", "pending", "Not checked");
            setCheck("autosave", "pending", "Not checked");
        } else if (result.check === "questions") {
            setCheck("candidate_session", "ready", "Signed-in session is active");
            setCheck("questions", "error", "Question bank is unavailable");
            setCheck("autosave", "pending", "Not checked");
        } else if (result.check === "autosave") {
            setCheck("candidate_session", "ready", "Signed-in session is active");
            setCheck("questions", "ready", `${result.question_count} question(s) available`);
            setCheck("autosave", "error", "Answer storage is not writable");
        } else {
            setCheck("candidate_session", "error", "Readiness service is unavailable");
            setCheck("questions", "pending", "Not checked");
            setCheck("autosave", "pending", "Not checked");
        }
        throw new Error(result.error || "The exam readiness check failed.");
    }

    if (
        result.status !== "ready" ||
        !result.checks?.candidate_session ||
        !result.checks?.questions ||
        !Number.isInteger(result.checks?.question_count) ||
        result.checks.question_count < 1 ||
        !result.checks?.autosave
    ) {
        setCheck("candidate_session", "error", "Session check was incomplete");
        setCheck("questions", "error", "Question check was incomplete");
        setCheck("autosave", "error", "Autosave check was incomplete");
        throw new Error("The server did not confirm every exam readiness check.");
    }

    setCheck("candidate_session", "ready", "Signed-in session is active");
    setCheck("questions", "ready", `${result.checks.question_count} question(s) available`);
    setCheck("autosave", "ready", "Answer storage is writable");
    return result;
}

async function getCameraFrame() {
    if (readinessCamera.readyState < HTMLMediaElement.HAVE_CURRENT_DATA) {
        throw new Error("The camera preview has not produced a video frame yet.");
    }

    const canvas = document.createElement("canvas");
    canvas.width = readinessCamera.videoWidth;
    canvas.height = readinessCamera.videoHeight;
    if (!canvas.width || !canvas.height) {
        throw new Error("The camera preview has no usable video frame.");
    }

    const context = canvas.getContext("2d");
    if (!context) {
        throw new Error("This browser could not prepare a camera frame.");
    }
    context.drawImage(readinessCamera, 0, 0, canvas.width, canvas.height);

    const blob = await new Promise(resolve => canvas.toBlob(resolve, "image/jpeg", 0.75));
    if (!blob) {
        throw new Error("This browser could not capture a camera frame.");
    }

    const form = new FormData();
    form.append("frame", blob, "readiness.jpg");
    const response = await fetch("/api/exam/readiness/face", {
        method: "POST",
        body: form
    });
    const result = await response.json();
    if (!response.ok) {
        throw new Error(result.error || "Face detection could not be checked.");
    }
    return result.faces;
}

async function runReadinessCheck() {
    if (readinessRunning) {
        return;
    }

    readinessRunning = true;
    runReadinessBtn.disabled = true;
    runReadinessBtn.textContent = "Checking...";
    startExamBtn.disabled = true;
    readinessError.textContent = "";
    readinessMessage.textContent = "Checking your candidate session, questions, and autosave...";
    setCheck("candidate_session", "checking", "Checking...");
    setCheck("questions", "pending", "Waiting for session check");
    setCheck("autosave", "pending", "Waiting for session check");
    setCheck("camera", "pending", "Not checked");
    setCheck("face", "pending", "Not checked");

    try {
        await checkServerReadiness();

        setCheck("camera", "checking", "Starting camera...");
        if (!navigator.mediaDevices?.getUserMedia) {
            throw new Error("Camera access requires HTTPS or localhost in a supported browser.");
        }

        if (!cameraStream || cameraStream.getVideoTracks().every(track => track.readyState !== "live")) {
            cameraStream = await navigator.mediaDevices.getUserMedia({
                audio: false,
                video: {
                    facingMode: "user",
                    width: {ideal: 640},
                    height: {ideal: 480}
                }
            });
            readinessCamera.srcObject = cameraStream;
            cameraStream.getVideoTracks().forEach(track => {
                track.addEventListener("ended", () => {
                    setCheck("camera", "error", "Camera connection stopped");
                    setCheck("face", "error", "Face check not passed");
                    readinessError.textContent =
                        "The camera stopped. Run the readiness check again.";
                    readinessMessage.textContent =
                        "The exam cannot start until the camera is live.";
                    updateStartButton();
                }, {once: true});
            });
        }
        await readinessCamera.play();
        if (readinessCamera.readyState < HTMLMediaElement.HAVE_CURRENT_DATA) {
            await new Promise((resolve, reject) => {
                readinessCamera.addEventListener("loadeddata", resolve, {once: true});
                readinessCamera.addEventListener(
                    "error",
                    () => reject(new Error("The camera preview could not start.")),
                    {once: true}
                );
            });
        }
        if (!cameraStream.getVideoTracks().some(track => track.readyState === "live")) {
            throw new Error("The camera connection stopped. Run the readiness check again.");
        }
        setCheck("camera", "ready", "Live camera preview is running");

        setCheck("face", "checking", "Detecting face...");
        const faceCount = await getCameraFrame();
        if (!cameraStream.getVideoTracks().some(track => track.readyState === "live")) {
            throw new Error("The camera connection stopped. Run the readiness check again.");
        }
        if (faceCount === 1) {
            setCheck("face", "ready", "Exactly one face detected");
        } else if (faceCount === 0) {
            setCheck("face", "error", "No face detected");
            throw new Error("Move into the camera view, improve the lighting, then check again.");
        } else {
            setCheck("face", "error", `${faceCount} faces detected`);
            throw new Error("Only the candidate should be visible. Ask others to leave the camera view.");
        }

        readinessMessage.textContent =
            "All checks passed. The exam is ready; a new timer starts or a saved exam resumes when you open it.";
    } catch (error) {
        const cameraIsLive = cameraStream?.getVideoTracks().some(
            track => track.readyState === "live"
        );
        Object.entries(readinessChecks).forEach(([name, check]) => {
            if (check.dataset.state === "checking") {
                const message = {
                    candidate_session: "Session could not be verified",
                    camera: error.name === "NotAllowedError"
                        ? "Camera permission was denied"
                        : "Camera could not start",
                    face: "Face check not passed",
                    questions: "Question check failed",
                    autosave: "Autosave check failed"
                }[name];
                setCheck(name, "error", message);
            }
        });
        readinessError.textContent = error.name === "NotAllowedError"
            ? "Allow camera access in your browser settings, then run the check again."
            : error.message || "Readiness could not be completed. Please try again.";
        readinessMessage.textContent = cameraIsLive ||
            readinessChecks.camera.dataset.state === "pending"
                ? "Fix the issue above and run the check again."
                : "The exam cannot start until the camera and face checks pass.";
    } finally {
        readinessRunning = false;
        runReadinessBtn.disabled = false;
        runReadinessBtn.textContent = allChecksReady() ? "Check again" : "Run check again";
        updateStartButton();
    }
}

function releaseCamera() {
    if (cameraStream) {
        cameraStream.getTracks().forEach(track => track.stop());
        cameraStream = null;
    }
    readinessCamera.srcObject = null;
}

runReadinessBtn.addEventListener("click", runReadinessCheck);
readinessCamera.addEventListener("error", () => {
    setCheck("camera", "error", "Camera preview stopped");
    setCheck("face", "error", "Face check not passed");
    readinessError.textContent = "The camera stopped. Run the readiness check again.";
    readinessMessage.textContent = "The exam cannot start until the camera is live.";
    updateStartButton();
});
window.addEventListener("pagehide", releaseCamera);

startExamBtn.addEventListener("click", async () => {
    if (!allChecksReady() || readinessRunning) {
        return;
    }

    startExamBtn.disabled = true;
    startExamBtn.textContent = "Starting exam...";
    readinessError.textContent = "";

    try {
        const fullscreenRequest = document.documentElement.requestFullscreen();
        const fullscreenReady = fullscreenRequest.then(
            () => true,
            () => false
        );
        setCheck("candidate_session", "checking", "Rechecking session...");
        setCheck("questions", "checking", "Rechecking questions...");
        setCheck("autosave", "checking", "Rechecking autosave...");
        await checkServerReadiness();
        const faceCount = await getCameraFrame();
        const cameraIsLive = cameraStream?.getVideoTracks().some(
            track => track.readyState === "live"
        );
        if (!cameraIsLive) {
            setCheck("camera", "error", "Camera connection stopped");
            setCheck("face", "error", "Face check not passed");
            throw new Error("The camera stopped. Run the readiness check again.");
        }
        if (faceCount !== 1) {
            setCheck(
                "face",
                "error",
                faceCount === 0
                    ? "No face detected"
                    : `${faceCount} faces detected`
            );
            throw new Error(
                faceCount === 0
                    ? "Keep your face in the camera view and run the readiness check again."
                    : "Only the candidate should be visible in the camera."
            );
        }
        if (!await fullscreenReady) {
            throw new Error("Allow fullscreen mode in your browser, then try again.");
        }
        releaseCamera();
        window.location.assign("/exam");
    } catch (error) {
        if (document.fullscreenElement) {
            await document.exitFullscreen().catch(() => {});
        }
        readinessError.textContent =
            error.message || "The camera or fullscreen check failed. Please try again.";
        startExamBtn.textContent = "Start Examination";
        updateStartButton();
    }
});
}