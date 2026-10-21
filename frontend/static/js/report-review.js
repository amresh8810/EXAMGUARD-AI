const csrfToken = document.querySelector('meta[name="admin-csrf-token"]')?.content || "";
const reviewLabels = {
    unreviewed: "Unreviewed",
    valid_alert: "Valid alert",
    false_positive: "False positive",
    needs_follow_up: "Needs follow-up"
};

document.querySelectorAll(".event-review-form").forEach(form => {
    form.addEventListener("submit", async event => {
        event.preventDefault();
        const eventId = form.dataset.eventId;
        const statusField = form.elements.status;
        const noteField = form.elements.note;
        const submitButton = form.querySelector("button[type='submit']");
        const message = form.querySelector(".review-save-message");
        const eventCard = form.closest(".review-event-card");
        const statusBadge = eventCard.querySelector(".review-status");
        const originalButtonText = submitButton.textContent;

        submitButton.disabled = true;
        submitButton.textContent = "Saving...";
        message.textContent = "";
        message.classList.remove("is-error");

        try {
            const response = await fetch(`/api/admin/evidence-review/${encodeURIComponent(eventId)}`, {
                method: "POST",
                headers: {
                    "Content-Type": "application/json",
                    "X-CSRF-Token": csrfToken
                },
                body: JSON.stringify({
                    status: statusField.value,
                    note: noteField.value
                })
            });
            const result = await response.json();
            if (!response.ok) {
                throw new Error(result.error || "The review could not be saved.");
            }

            statusBadge.textContent = reviewLabels[statusField.value];
            statusBadge.className = `review-status review-status-${statusField.value}`;
            let audit = eventCard.querySelector(".review-audit");
            if (result.reviewed_at) {
                if (!audit) {
                    audit = document.createElement("p");
                    audit.className = "review-audit";
                    form.insertAdjacentElement("afterend", audit);
                }
                audit.textContent =
                    `Last reviewed by ${result.reviewed_by} · ` +
                    new Date(result.reviewed_at * 1000).toLocaleString();
            } else if (audit) {
                audit.remove();
            }
            message.textContent = "Review saved.";
        } catch (error) {
            message.textContent = error.message || "The review could not be saved. Please retry.";
            message.classList.add("is-error");
        } finally {
            submitButton.disabled = false;
            submitButton.textContent = originalButtonText;
        }
    });
});
