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


// LOGIN + FACE VERIFICATION

document.querySelector("form").addEventListener("submit", async (e) => {

    e.preventDefault();

    const nameInput = document.getElementById("candidateName");
    const regInput = document.getElementById("regNumber");
    const name = nameInput ? nameInput.value.trim() : "";
    const regNo = regInput ? regInput.value.trim() : "";

    if (!name || !regNo) {
        alert("Please enter both Full Name and Registration Number.");
        return;
    }

    const loginBtn =
    document.querySelector("button[type='submit']");

    loginBtn.innerHTML =
    "Verifying Face...";

    loginBtn.disabled = true;

    try{

        const response =
        await fetch("/verify", {
            method: "POST",
            headers: {
                "Content-Type": "application/json"
            },
            body: JSON.stringify({
                name: name,
                reg_no: regNo
            })
        });

        const data =
        await response.json();

        if(data.status === "success"){

            alert(
                data.message || "Candidate Verified Successfully!"
            );

            window.location.href =
            "/instructions";

        }else{

            alert(
                data.message || "Face Verification Failed!\nAccess Denied."
            );

            loginBtn.innerHTML =
            "Verify Identity";

            loginBtn.disabled = false;
        }

    }

    catch(error){

        console.log(error);

        alert(
            "Verification Server Error: " + error.message
        );

        loginBtn.innerHTML =
        "Verify Identity";

        loginBtn.disabled = false;
    }

});