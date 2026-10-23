function updateClock(){

    const now = new Date();

    document.getElementById("clock").innerHTML =
    now.toLocaleTimeString();

}

setInterval(updateClock,1000);

updateClock();

const text =
"Next Generation AI Proctoring Platform";

let index = 0;

function typeEffect(){

    if(index < text.length){

        document.getElementById("typing").innerHTML +=
        text.charAt(index);

        index++;

        setTimeout(typeEffect,50);
    }

}

typeEffect();


const loginForm = document.getElementById("loginForm");
const loginButton = loginForm.querySelector("button[type='submit']");
const candidateVideo = document.getElementById("cameraPreview");
const cameraStatus = document.getElementById("cameraStatus");
let cameraStream = null;

function updateCameraStatus(message, isError = false) {
    cameraStatus.textContent = message;
    cameraStatus.classList.toggle("is-error", isError);
}

function stopCamera() {
    if (cameraStream) {
        cameraStream.getTracks().forEach((track) => track.stop());
        cameraStream = null;
    }

    candidateVideo.pause();
    candidateVideo.srcObject = null;
    candidateVideo.hidden = true;
}

function cameraErrorMessage(error) {
    if (!window.isSecureContext) {
        return "Camera access requires HTTPS. Open the secure exam site and try again.";
    }

    if (error.name === "NotAllowedError" || error.name === "PermissionDeniedError") {
        return "Camera permission was denied. Allow camera access for this site, then try again.";
    }

    if (error.name === "NotFoundError" || error.name === "DevicesNotFoundError") {
        return "No camera was found. Connect a camera and try again.";
    }

    if (error.name === "NotReadableError" || error.name === "TrackStartError") {
        return "The camera is busy or unavailable. Close other camera apps and try again.";
    }

    return "The camera could not be started. Check its permission and try again.";
}

async function startCamera() {
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
        updateCameraStatus(
            "This browser cannot access a camera. Use a current browser over HTTPS.",
            true
        );
        return false;
    }

    try {
        cameraStream = await navigator.mediaDevices.getUserMedia({
            video: {
                facingMode: "user",
                width: { ideal: 640 },
                height: { ideal: 480 }
            },
            audio: false
        });
        candidateVideo.srcObject = cameraStream;
        candidateVideo.hidden = false;
        await candidateVideo.play();

        if (candidateVideo.readyState < 2) {
            await new Promise((resolve, reject) => {
                candidateVideo.addEventListener("loadeddata", resolve, { once: true });
                candidateVideo.addEventListener(
                    "error",
                    () => reject(new Error("The camera preview did not load.")),
                    { once: true }
                );
            });
        }

        if (!candidateVideo.videoWidth || !candidateVideo.videoHeight) {
            throw new Error("The camera did not provide a video frame.");
        }

        updateCameraStatus(
            "Camera ready. Center your face in the preview, then select Capture & Verify."
        );
        loginButton.textContent = "Capture & Verify";
        return true;
    } catch (error) {
        stopCamera();
        updateCameraStatus(cameraErrorMessage(error), true);
        loginButton.textContent = "Start Camera";
        return false;
    }
}

function captureCameraFrame() {
    const scale = Math.min(
        1,
        1280 / candidateVideo.videoWidth,
        960 / candidateVideo.videoHeight
    );
    const canvas = document.createElement("canvas");
    canvas.width = Math.round(candidateVideo.videoWidth * scale);
    canvas.height = Math.round(candidateVideo.videoHeight * scale);

    const context = canvas.getContext("2d");
    if (!context) {
        throw new Error("The camera frame could not be captured.");
    }

    context.drawImage(candidateVideo, 0, 0, canvas.width, canvas.height);
    return canvas.toDataURL("image/jpeg", 0.85).split(",", 2)[1];
}

loginForm.addEventListener("submit", async (event) => {
    event.preventDefault();

    const name = document.getElementById("candidateName").value.trim();
    const regNo = document.getElementById("regNumber").value.trim();
    if (!name || !regNo) {
        updateCameraStatus("Enter both your full name and registration number.", true);
        return;
    }

    if (!cameraStream) {
        loginButton.disabled = true;
        loginButton.textContent = "Starting Camera...";
        await startCamera();
        loginButton.disabled = false;
        return;
    }

    loginButton.disabled = true;
    loginButton.textContent = "Verifying Face...";

    try {
        const frame = captureCameraFrame();
        stopCamera();
        updateCameraStatus("Face captured. Verifying securely...");

        const response = await fetch("/verify", {
            method: "POST",
            headers: {
                "Content-Type": "application/json"
            },
            body: JSON.stringify({
                name,
                reg_no: regNo,
                frame
            })
        });

        let data;
        try {
            data = await response.json();
        } catch {
            throw new Error(`The verification service returned an invalid response (HTTP ${response.status}).`);
        }

        if (!response.ok || data.status !== "success") {
            updateCameraStatus(
                data.message || "Face verification failed. Please try again.",
                true
            );
            loginButton.textContent = "Start Camera";
            return;
        }

        updateCameraStatus(data.message || "Candidate verified successfully.");
        window.alert(data.message || "Candidate verified successfully.");
        window.location.href = "/instructions";
    } catch (error) {
        updateCameraStatus(
            `Verification could not be completed: ${error.message}`,
            true
        );
        loginButton.textContent = "Start Camera";
    } finally {
        stopCamera();
        loginButton.disabled = false;
    }
});

window.addEventListener("pagehide", stopCamera);