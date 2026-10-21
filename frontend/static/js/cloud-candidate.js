(() => {
    "use strict";

    const readinessConfigElement = document.getElementById("cloudReadinessConfig");
    const attemptConfigElement = document.getElementById("cloudAttemptConfig");

    function showStatus(element, message, state) {
        if (!element) return;
        element.textContent = message;
        element.dataset.state = state || "info";
    }

    function cameraCanvas(video) {
        if (!video.videoWidth || !video.videoHeight) return null;
        const canvas = document.createElement("canvas");
        const scale = Math.min(1, 480 / video.videoWidth);
        canvas.width = Math.max(100, Math.round(video.videoWidth * scale));
        canvas.height = Math.max(100, Math.round(video.videoHeight * scale));
        const context = canvas.getContext("2d", {willReadFrequently: false});
        context.drawImage(video, 0, 0, canvas.width, canvas.height);
        return canvas.toDataURL("image/jpeg", 0.62);
    }

    async function postJson(url, csrfToken, payload) {
        const response = await fetch(url, {
            method: "POST",
            credentials: "same-origin",
            headers: {
                "Content-Type": "application/json",
                "X-CSRF-Token": csrfToken
            },
            body: JSON.stringify(payload)
        });
        let result;
        try {
            result = await response.json();
        } catch {
            throw new Error("The server returned an unreadable response.");
        }
        if (!response.ok) {
            throw new Error(result.error || "The request could not be completed.");
        }
        return result;
    }

    if (readinessConfigElement) {
        const config = JSON.parse(readinessConfigElement.textContent);
        const video = document.getElementById("cloudCameraPreview");
        const placeholder = document.getElementById("cameraPlaceholder");
        const status = document.getElementById("readinessStatus");
        const cameraButton = document.getElementById("startCloudCamera");
        const startButton = document.getElementById("startCloudExam");
        let stream = null;
        let checkInFlight = false;
        let ready = false;
        let pollTimer = null;

        const checkFrame = async () => {
            if (!stream || checkInFlight || video.readyState < HTMLMediaElement.HAVE_CURRENT_DATA) return;
            const frame = cameraCanvas(video);
            if (!frame) return;
            checkInFlight = true;
            try {
                const result = await postJson(config.readiness_url, config.csrf_token, {frame});
                ready = result.ready === true;
                startButton.disabled = !ready;
                showStatus(
                    status,
                    result.message || "Camera check complete.",
                    ready ? "success" : "warning"
                );
            } catch (error) {
                ready = false;
                startButton.disabled = true;
                showStatus(status, error.message, "error");
            } finally {
                checkInFlight = false;
            }
        };

        cameraButton.addEventListener("click", async () => {
            cameraButton.disabled = true;
            showStatus(status, "Requesting camera permission…", "info");
            try {
                stream = await navigator.mediaDevices.getUserMedia({
                    video: {facingMode: "user", width: {ideal: 640}, height: {ideal: 480}},
                    audio: false
                });
                video.srcObject = stream;
                await video.play();
                placeholder.hidden = true;
                cameraButton.textContent = "Camera active";
                showStatus(status, "Keep one face visible while the camera check runs.", "info");
                pollTimer = window.setInterval(checkFrame, 2500);
                await checkFrame();
            } catch (error) {
                cameraButton.disabled = false;
                showStatus(
                    status,
                    error.name === "NotAllowedError"
                        ? "Camera permission was denied. Allow camera access in the browser and retry."
                        : "The camera could not start. Close other apps using it and retry.",
                    "error"
                );
            }
        });

        startButton.addEventListener("click", async () => {
            if (!ready) return;
            startButton.disabled = true;
            showStatus(status, "Starting your secure attempt…", "info");
            try {
                const result = await postJson(config.start_url, config.csrf_token, {});
                if (!result.redirect) throw new Error("The exam could not be opened.");
                if (stream) stream.getTracks().forEach(track => track.stop());
                if (pollTimer) window.clearInterval(pollTimer);
                window.location.assign(result.redirect);
            } catch (error) {
                showStatus(status, error.message, "error");
                startButton.disabled = false;
                await checkFrame();
            }
        });

        window.addEventListener("pagehide", () => {
            if (pollTimer) window.clearInterval(pollTimer);
            if (stream) stream.getTracks().forEach(track => track.stop());
        });
        return;
    }

    if (!attemptConfigElement) return;

    const config = JSON.parse(attemptConfigElement.textContent);
    const attempt = config.attempt;
    const questions = attempt.questions || [];
    const answers = [...attempt.answers];
    const textElement = document.getElementById("cloudQuestionText");
    const numberElement = document.getElementById("cloudQuestionNumber");
    const optionsElement = document.getElementById("cloudOptions");
    const paletteElement = document.getElementById("cloudQuestionPalette");
    const saveStatus = document.getElementById("cloudSaveStatus");
    const monitorStatus = document.getElementById("cloudMonitorStatus");
    const timerElement = document.getElementById("cloudExamTimer");
    let currentQuestion = 0;
    let saveTimer = null;
    let saveInFlight = false;
    let saveRequested = false;
    let submitting = false;
    let stream = null;
    let monitorTimer = null;
    let monitorInFlight = false;
    const deadline = Date.parse(attempt.deadline_at);

    function renderPalette() {
        paletteElement.replaceChildren();
        questions.forEach((question, index) => {
            const button = document.createElement("button");
            button.type = "button";
            button.className = "palette-question";
            button.textContent = String(index + 1);
            button.setAttribute("aria-label", `Go to question ${index + 1}${answers[index] === null ? ", unanswered" : ", answered"}`);
            if (index === currentQuestion) button.classList.add("is-current");
            if (answers[index] !== null) button.classList.add("is-answered");
            button.addEventListener("click", () => {
                currentQuestion = index;
                renderQuestion();
            });
            paletteElement.append(button);
        });
    }

    function renderQuestion() {
        const question = questions[currentQuestion];
        numberElement.textContent = `Question ${currentQuestion + 1} of ${questions.length}`;
        textElement.textContent = question.question;
        optionsElement.replaceChildren();
        question.options.forEach((option, index) => {
            const label = document.createElement("label");
            label.className = "cloud-option";
            if (answers[currentQuestion] === index) label.classList.add("is-selected");
            const input = document.createElement("input");
            input.type = "radio";
            input.name = "cloud-answer";
            input.value = String(index);
            input.checked = answers[currentQuestion] === index;
            input.addEventListener("change", () => {
                answers[currentQuestion] = index;
                renderQuestion();
                scheduleAutosave();
            });
            const content = document.createElement("span");
            content.textContent = option;
            label.append(input, content);
            optionsElement.append(label);
        });
        document.getElementById("cloudPrevious").disabled = currentQuestion === 0;
        document.getElementById("cloudNext").textContent =
            currentQuestion === questions.length - 1 ? "Last question" : "Next";
        renderPalette();
    }

    function scheduleAutosave() {
        if (saveTimer) window.clearTimeout(saveTimer);
        saveTimer = window.setTimeout(() => flushAutosave(), 500);
        showStatus(saveStatus, "Answer changed — saving…", "info");
    }

    async function flushAutosave() {
        if (submitting) return;
        if (saveInFlight) {
            saveRequested = true;
            return;
        }
        saveInFlight = true;
        do {
            saveRequested = false;
            try {
                const result = await postJson(
                    config.autosave_url,
                    config.csrf_token,
                    {answers: [...answers]}
                );
                if (result.status === "submitted") {
                    submitting = true;
                    window.location.assign(config.result_url);
                    return;
                }
                showStatus(saveStatus, "All answers saved.", "success");
            } catch (error) {
                showStatus(saveStatus, `${error.message} Retrying when you change an answer.`, "error");
            }
        } while (saveRequested && !submitting);
        saveInFlight = false;
    }

    async function submitExam(reason = "manual") {
        if (submitting) return;
        if (reason === "manual" && !window.confirm("Submit this exam now? You cannot change answers after submission.")) {
            return;
        }
        submitting = true;
        document.getElementById("cloudSubmitExam").disabled = true;
        showStatus(saveStatus, "Submitting your answers…", "info");
        try {
            const result = await postJson(
                config.submit_url,
                config.csrf_token,
                {answers: [...answers], reason}
            );
            if (!result.redirect) throw new Error("The server did not confirm submission.");
            if (stream) stream.getTracks().forEach(track => track.stop());
            if (monitorTimer) window.clearInterval(monitorTimer);
            window.location.assign(result.redirect);
        } catch (error) {
            submitting = false;
            document.getElementById("cloudSubmitExam").disabled = false;
            showStatus(saveStatus, `${error.message} Your attempt remains open; retry submission.`, "error");
        }
    }

    function updateTimer() {
        const remaining = Math.max(0, Math.ceil((deadline - Date.now()) / 1000));
        const hours = Math.floor(remaining / 3600);
        const minutes = Math.floor((remaining % 3600) / 60);
        const seconds = remaining % 60;
        timerElement.textContent =
            `${String(hours).padStart(2, "0")}:${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
        if (remaining === 0) {
            submitExam("time_expired");
        }
    }

    async function checkProctorFrame() {
        if (!stream || monitorInFlight || submitting) return;
        const video = document.getElementById("cloudExamCamera");
        if (video.readyState < HTMLMediaElement.HAVE_CURRENT_DATA) return;
        const frame = cameraCanvas(video);
        if (!frame) return;
        monitorInFlight = true;
        try {
            const result = await postJson(config.proctor_url, config.csrf_token, {frame});
            showStatus(
                monitorStatus,
                result.face_count === 1
                    ? "One face detected. Camera monitoring is active."
                    : `Camera check detected ${result.face_count} faces. Keep only yourself in view.`,
                result.face_count === 1 ? "success" : "warning"
            );
        } catch (error) {
            showStatus(monitorStatus, `${error.message} Camera monitoring needs attention.`, "error");
        } finally {
            monitorInFlight = false;
        }
    }

    document.getElementById("cloudPrevious").addEventListener("click", () => {
        if (currentQuestion > 0) {
            currentQuestion -= 1;
            renderQuestion();
        }
    });
    document.getElementById("cloudNext").addEventListener("click", () => {
        if (currentQuestion < questions.length - 1) {
            currentQuestion += 1;
            renderQuestion();
        }
    });
    document.getElementById("cloudSubmitExam").addEventListener("click", () => submitExam());

    renderQuestion();
    showStatus(saveStatus, "Exam progress loaded.", "success");
    updateTimer();
    window.setInterval(updateTimer, 1000);
    window.setInterval(flushAutosave, 15000);

    const video = document.getElementById("cloudExamCamera");
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
        showStatus(monitorStatus, "This browser cannot access the camera. Use a modern browser on HTTPS.", "error");
    } else {
        navigator.mediaDevices.getUserMedia({
            video: {facingMode: "user", width: {ideal: 640}, height: {ideal: 480}},
            audio: false
        }).then(cameraStream => {
            stream = cameraStream;
            video.srcObject = stream;
            showStatus(monitorStatus, "Camera monitoring starting…", "info");
            checkProctorFrame();
            monitorTimer = window.setInterval(checkProctorFrame, 10000);
        }).catch(error => {
            showStatus(
                monitorStatus,
                error.name === "NotAllowedError"
                    ? "Camera permission is blocked. Allow camera access and reload the exam."
                    : "The camera could not start. Close other camera apps and reload.",
                "error"
            );
        });
    }

    window.addEventListener("pagehide", () => {
        if (stream) stream.getTracks().forEach(track => track.stop());
        if (monitorTimer) window.clearInterval(monitorTimer);
        if (saveTimer) window.clearTimeout(saveTimer);
    });
})();
