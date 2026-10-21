(() => {
    "use strict";

    const createForm = document.getElementById("cloudExamCreateForm");
    if (createForm) {
        createForm.addEventListener("submit", event => {
            const selected = createForm.querySelectorAll(
                'input[name="question_ids"]:checked'
            );
            if (selected.length === 0) {
                event.preventDefault();
                window.alert("Choose at least one question for this exam.");
                return;
            }

            const opensLocal = document.getElementById("cloud-opens-local").value;
            const closesLocal = document.getElementById("cloud-closes-local").value;
            const opensDate = new Date(opensLocal);
            const closesDate = new Date(closesLocal);
            if (
                !opensLocal ||
                !closesLocal ||
                Number.isNaN(opensDate.getTime()) ||
                Number.isNaN(closesDate.getTime())
            ) {
                event.preventDefault();
                window.alert("Enter a valid exam start and end time.");
                return;
            }
            document.getElementById("cloud-opens-utc").value = opensDate.toISOString();
            document.getElementById("cloud-closes-utc").value = closesDate.toISOString();

            const submitButton = createForm.querySelector('button[type="submit"]');
            if (submitButton) {
                submitButton.disabled = true;
                submitButton.textContent = "Creating draft…";
            }
        });
    }

    document.querySelectorAll("form[data-confirm]").forEach(form => {
        form.addEventListener("submit", event => {
            if (!window.confirm(form.dataset.confirm)) {
                event.preventDefault();
                return;
            }
            const submitButton = form.querySelector('button[type="submit"]');
            if (submitButton) {
                submitButton.disabled = true;
                submitButton.textContent = "Please wait…";
            }
        });
    });

    document.querySelectorAll(".cloud-upload-form").forEach(form => {
        form.addEventListener("submit", event => {
            const fileInput = form.querySelector('input[type="file"]');
            if (!fileInput || !fileInput.files.length) {
                event.preventDefault();
                return;
            }
            if (!window.confirm(
                "Import this roster? The invitation-code CSV is downloaded only once. Save it securely and distribute each code privately."
            )) {
                event.preventDefault();
                return;
            }
            const submitButton = form.querySelector('button[type="submit"]');
            if (submitButton) {
                submitButton.disabled = true;
                submitButton.textContent = "Importing roster…";
            }
        });
    });
})();
