const webcam = document.getElementById("webcam");
const cameraStatus = document.getElementById("cameraStatus");

webcam.addEventListener("load", () => {
    cameraStatus.textContent = "Camera feed active.";
    cameraStatus.classList.add("connected");
});

webcam.addEventListener("error", () => {
    cameraStatus.textContent =
        "Camera feed unavailable. Refresh the exam page or sign in again.";
    cameraStatus.classList.remove("connected");
});

// Backend handles camera
 function startCamera() {
      console.log("Backend face detection active");
}

startCamera();

    


// TIMER
let violations = 0;
const timer = document.getElementById("timer");
const saveStatus = document.getElementById("saveStatus");
const localExamKey = "examguard.activeExam";
let resumeToken = null;
let deadlineAt = null;
let examSessionReady = false;
let autosaveTimer = null;
let autosaveInterval = null;
let timerInterval = null;
let autosaveInFlight = false;
let autosaveRequested = false;
let examSubmitting = false;
let progressRevision = 0;
let lastSavedAt = 0;

// QUESTIONS (Dynamic from Database)

let questions = [
{
    id: "fallback-1",
    question:"Which protocol provides reliable transport in TCP/IP?",
    options:["TCP","UDP","ICMP","ARP"]
}
];


// VARIABLES

let currentQuestion = 0;

let answers = new Array(questions.length).fill(null);

let reviewed = new Array(questions.length).fill(false);

let visited = new Array(questions.length).fill(false);

visited[0] = true;


// ELEMENTS

const questionText =
document.getElementById("questionText");

const questionNumber =
document.getElementById("questionNumber");

const optionsContainer =
document.getElementById("optionsContainer");

const progressFill =
document.querySelector(".progress-fill");

const progressText =
document.getElementById("progressText");

const nextBtn =
document.getElementById("nextBtn");

const prevBtn =
document.getElementById("prevBtn");

const reviewBtn =
document.getElementById("reviewBtn");

const paletteGrid =
document.getElementById("paletteGrid");

const submitBtn =
document.getElementById("submitBtn");


// LOAD QUESTION

function loadQuestion(saveChanges = true){

    questionNumber.innerHTML =
    `Question ${currentQuestion + 1} of ${questions.length}`;

    questionText.innerHTML =
    questions[currentQuestion].question;

    optionsContainer.innerHTML = "";

    questions[currentQuestion].options.forEach((option,index)=>{

        const label =
        document.createElement("label");

        label.className = "option";

        label.innerHTML = `
        <input
        type="radio"
        name="answer"
        value="${index}"
        ${answers[currentQuestion] == index ? "checked" : ""}
        >
        ${option}
        `;

        optionsContainer.appendChild(label);

    });

    updatePalette();

    updateProgress();
    if (saveChanges) {
        progressChanged();
    }
}


// SAVE ANSWER

document.addEventListener("change",(e)=>{

    if(e.target.name==="answer"){

        answers[currentQuestion] =
        parseInt(e.target.value);

        reviewed[currentQuestion] = false;

        updatePalette();
        progressChanged();
    }

});


// NEXT

nextBtn.addEventListener("click",()=>{

    if(currentQuestion < questions.length-1){

        currentQuestion++;

        visited[currentQuestion] = true;

        loadQuestion();
    }

});


// PREVIOUS

prevBtn.addEventListener("click",()=>{

    if(currentQuestion > 0){

        currentQuestion--;

        loadQuestion();
    }

});


// MARK REVIEW

reviewBtn.addEventListener("click",()=>{

    reviewed[currentQuestion] = true;

    updatePalette();
    progressChanged();

    alert(
    `Question ${currentQuestion+1} marked for review`
    );

});


// PALETTE COLORS

function renderPalette(){

    paletteGrid.replaceChildren();

    questions.forEach((_,index)=>{
        const button = document.createElement("button");
        button.type = "button";
        button.className = "q-btn";
        button.textContent = String(index + 1);
        button.setAttribute("aria-label", `Go to question ${index + 1}`);
        button.addEventListener("click",()=>{
            currentQuestion = index;
            visited[index] = true;
            loadQuestion();
        });
        paletteGrid.appendChild(button);
    });
}

function updatePalette(){

    paletteGrid.querySelectorAll(".q-btn").forEach((btn,index)=>{

        btn.style.background = "#ffffff";
        btn.style.color = "#000000";

        if(visited[index]){

            btn.style.background = "#ef4444";
            btn.style.color = "#ffffff";
        }

        if(answers[index] !== null){

            btn.style.background = "#22c55e";
            btn.style.color = "#ffffff";
        }

        if(reviewed[index]){

            btn.style.background = "#f59e0b";
            btn.style.color = "#ffffff";
        }

        if(index === currentQuestion){

            btn.style.outline =
            "3px solid #60a5fa";
        }
        else{

            btn.style.outline = "none";
        }

    });

}


// PROGRESS

function updateProgress(){

    let answeredCount = 0;

    answers.forEach(ans=>{

        if(ans !== null){

            answeredCount++;
        }

    });

    const percent =
    (answeredCount / questions.length) * 100;

    progressFill.style.width =
    `${percent}%`;

    progressText.innerHTML =
    `${Math.round(percent)}% Completed`;
}


// SUBMIT

submitBtn.addEventListener("click",()=>{
    submitExam("manual", true);
});


function setSaveStatus(message, statusClass = ""){
    saveStatus.textContent = message;
    saveStatus.className = `save-status ${statusClass}`.trim();
}


function getProgressSnapshot(){
    return {
        question_ids: questions.map(question => question.id),
        answers: [...answers],
        reviewed: [...reviewed],
        visited: [...visited],
        current_question: currentQuestion
    };
}


function persistLocalDraft(savedAt = Date.now()){
    if(!resumeToken){
        return;
    }

    const snapshot = {
        resume_token: resumeToken,
        saved_at: savedAt,
        ...getProgressSnapshot()
    };

    try{
        localStorage.setItem(localExamKey, JSON.stringify(snapshot));
    }catch(error){
        console.error("Could not store the local exam recovery copy.", error);
        setSaveStatus(
            "Browser storage is unavailable; keep this page open while progress syncs.",
            "error"
        );
    }
}


function progressChanged(){
    progressRevision++;
    persistLocalDraft();

    if(!examSessionReady){
        return;
    }

    setSaveStatus("Unsaved change — saving...", "");
    clearTimeout(autosaveTimer);
    autosaveTimer = setTimeout(flushAutosave, 350);
}


async function flushAutosave(){
    if(!examSessionReady || !resumeToken){
        return;
    }

    if(autosaveInFlight){
        autosaveRequested = true;
        return;
    }

    autosaveInFlight = true;
    do{
        autosaveRequested = false;
        const revision = progressRevision;
        const payload = {
            resume_token: resumeToken,
            ...getProgressSnapshot()
        };

        try{
            const response = await fetch("/api/exam/autosave", {
                method: "POST",
                headers: {"Content-Type": "application/json"},
                body: JSON.stringify(payload)
            });
            const result = await response.json();
            if(!response.ok){
                throw new Error(result.error || "Progress could not be saved.");
            }
            if(result.status === "submitted"){
                finishSubmittedExam();
                return;
            }

            lastSavedAt = result.last_saved_at * 1000;
            if(revision === progressRevision){
                persistLocalDraft(lastSavedAt);
                setSaveStatus("All answers saved", "saved");
            }else{
                persistLocalDraft();
                autosaveRequested = true;
            }
        }catch(error){
            console.error("Exam autosave failed.", error);
            persistLocalDraft();
            setSaveStatus(
                "Server save failed — your browser has a recovery copy. Retrying...",
                "error"
            );
        }
    }while(autosaveRequested && examSessionReady);

    autosaveInFlight = false;
}


function startExamTimer(){
    clearInterval(timerInterval);

    const updateTimer = () => {
        const remaining = Math.max(
            0,
            Math.ceil(deadlineAt - Date.now() / 1000)
        );
        const minutes = Math.floor(remaining / 60);
        const seconds = remaining % 60;
        timer.textContent =
            `${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;

        if(remaining === 0){
            clearInterval(timerInterval);
            submitExam("time_expired", false);
        }
    };

    updateTimer();
    timerInterval = setInterval(updateTimer, 1000);
}


function restoreLocalDraft(draft, serverSavedAt){
    if(
        !draft ||
        !Array.isArray(draft.answers) ||
        !Array.isArray(draft.reviewed) ||
        !Array.isArray(draft.visited) ||
        !Array.isArray(draft.question_ids) ||
        draft.answers.length !== questions.length ||
        draft.reviewed.length !== questions.length ||
        draft.visited.length !== questions.length ||
        JSON.stringify(draft.question_ids.map(String)) !==
            JSON.stringify(questions.map(question => String(question.id))) ||
        !Number.isInteger(draft.current_question) ||
        draft.current_question < 0 ||
        draft.current_question >= questions.length ||
        typeof draft.saved_at !== "number" ||
        draft.saved_at <= serverSavedAt * 1000
    ){
        return false;
    }

    if(draft.answers.some((answer, index) =>
        answer !== null &&
        (!Number.isInteger(answer) || answer < 0 || answer >= questions[index].options.length)
    )){
        return false;
    }
    if(draft.reviewed.some(flag => typeof flag !== "boolean") ||
       draft.visited.some(flag => typeof flag !== "boolean")){
        return false;
    }

    answers = [...draft.answers];
    reviewed = [...draft.reviewed];
    visited = [...draft.visited];
    currentQuestion = draft.current_question;
    return true;
}


function disableExamControls(){
    document.querySelectorAll(
        ".question-card input, .question-card button, #submitBtn"
    ).forEach(control => {
        control.disabled = true;
    });
}


function finishSubmittedExam(){
    examSubmitting = true;
    examSessionReady = false;
    clearInterval(timerInterval);
    clearInterval(autosaveInterval);
    clearTimeout(autosaveTimer);
    try{
        localStorage.removeItem(localExamKey);
    }catch(error){
        console.error("Could not clear the completed local exam copy.", error);
    }
    window.location.assign("/result");
}


async function submitExam(reason = "manual", confirmSubmission = false){
    if(!examSessionReady || examSubmitting){
        return;
    }

    if(confirmSubmission){
        const answeredCount = answers.filter(answer => answer !== null).length;
        const confirmed = confirm(
            `You have answered ${answeredCount} out of ${questions.length} questions.\n\nSubmit Exam?`
        );
        if(!confirmed){
            return;
        }
    }

    examSubmitting = true;
    clearTimeout(autosaveTimer);
    clearInterval(timerInterval);
    submitBtn.disabled = true;
    persistLocalDraft();
    setSaveStatus("Saving your final answers and submitting exam...", "");

    try{
        const response = await fetch("/api/exam/submit", {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({
                resume_token: resumeToken,
                reason,
                ...getProgressSnapshot()
            })
        });
        const result = await response.json();
        if(!response.ok){
            throw new Error(result.error || "Exam could not be submitted.");
        }
        finishSubmittedExam();
    }catch(error){
        console.error("Exam submission failed.", error);
        examSubmitting = false;
        submitBtn.disabled = false;
        persistLocalDraft();
        setSaveStatus(
            "Submission could not reach the server. Your answers are saved in this browser; retrying...",
            "error"
        );
        if(reason === "time_expired"){
            setTimeout(() => submitExam("time_expired", false), 5000);
        }
    }
}


async function loadDynamicQuestions(){
    let localDraft = null;
    try{
        localDraft = JSON.parse(localStorage.getItem(localExamKey) || "null");
    }catch(error){
        console.error("The local exam recovery copy could not be read.", error);
    }

    try{
        const response = await fetch("/api/exam/session", {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({
                resume_token: localDraft?.resume_token || null
            })
        });
        const data = await response.json();
        if(!response.ok){
            throw new Error(data.error || "Could not open the exam session.");
        }
        if(data.status === "submitted"){
            finishSubmittedExam();
            return;
        }
        if(!Array.isArray(data.questions) || data.questions.length === 0){
            throw new Error("No exam questions are available.");
        }
        if(typeof data.exam_id !== "string" || !data.exam_id){
            throw new Error("Could not identify the secure exam session.");
        }

        resumeToken = data.resume_token;
        deadlineAt = data.deadline_at;
        questions = data.questions;
        answers = [...data.answers];
        reviewed = [...data.reviewed];
        visited = [...data.visited];
        currentQuestion = data.current_question;
        lastSavedAt = data.last_saved_at * 1000;
        cameraStatus.textContent = "Connecting to camera feed...";
        webcam.src = `/video_feed?exam_id=${encodeURIComponent(data.exam_id)}`;

        const restoredLocalDraft =
            localDraft?.resume_token === resumeToken &&
            restoreLocalDraft(localDraft, data.last_saved_at);

        renderPalette();
        examSessionReady = true;
        loadQuestion(false);
        persistLocalDraft(
            restoredLocalDraft ? localDraft.saved_at : lastSavedAt
        );
        setSaveStatus(
            restoredLocalDraft
                ? "Recovered your latest answers from this browser; syncing..."
                : data.last_saved_at > data.started_at
                    ? "Saved exam restored"
                    : "Answers save automatically",
            "saved"
        );
        startExamTimer();
        if(restoredLocalDraft){
            progressChanged();
        }
        autosaveInterval = setInterval(flushAutosave, 10000);
    }catch(error){
        console.error("Could not initialize the exam session.", error);
        setSaveStatus(error.message, "error");
        disableExamControls();
    }
}


window.addEventListener("beforeunload", persistLocalDraft);
loadDynamicQuestions();


document.addEventListener("visibilitychange", () => {

    if(document.hidden){

       persistLocalDraft();
       flushAutosave();

       violations = violations + 1;
       fetch("/update_violation", {
    method: "POST",
    headers: {
        "Content-Type": "application/json"
    },
    body: JSON.stringify({
        violations: violations
    })
});

document.getElementById(
    "violationCount"
).innerText =
    "Violations : " + violations;

        alert(
            "Warning!\nTab Switching Detected.\nViolation Count: "
            + violations
        );
    }

});


// -----------------------------
// FULLSCREEN BUTTON SUPPORT
// -----------------------------

function enterFullscreen() {

    if (!document.fullscreenElement) {

        document.documentElement
            .requestFullscreen()
            .catch(err => {
                console.log(err);
            });

    }
}

// -----------------------------
// FULLSCREEN EXIT DETECTION
// -----------------------------

document.addEventListener("fullscreenchange", () => {

    if (!document.fullscreenElement) {

        violations = violations + 1;

        document.getElementById(
            "violationCount"
        ).innerText =
            "Violations : " + violations;

        alert(
            "Warning!\nFullscreen Exit Detected.\nViolation Count: "
            + violations
        );

    }

});
async function updateMonitorData() {

    try {

        const response =
            await fetch("/monitor");

        const data =
            await response.json();

        document.getElementById(
            "faceCount"
        ).innerText =
            "Faces Detected : " + data.faces;

        document.getElementById(
            "violationCount"
        ).innerText =
            "Violations : " + data.violations;
            violations = data.violations;

        if (data.faces === 0) {
            console.log("No Face Detected");
}

if (data.violations >= 25 && examSessionReady && !examSubmitting) {

    alert(
        "Maximum violations exceeded.\nExam will be submitted automatically."
    );

    submitExam("violation_limit", false);
}


    }

    catch(error){

        console.log(
            "Monitor fetch failed",
            error
        );

    }

}

setInterval(
    updateMonitorData,
    1000
);

updateMonitorData();