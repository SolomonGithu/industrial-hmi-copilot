// Industrial HMI Copilot

const socket = io(`http://${window.location.host}`);

const MAX_MOTION_LOG = 12;

let motionEvents = [];

const $ = (id) => document.getElementById(id);

document.addEventListener("DOMContentLoaded", () => {
    bindSocketEvents();
    bindChatForm();

    // Give the operator immediate context.
    updatePhaseBadge("WAITING");
});


function bindSocketEvents() {
    socket.on("connect", () => {
        $("connectionBanner").classList.add("hidden");
    });

    socket.on("disconnect", () => {
        $("connectionBanner").classList.remove("hidden");
    });

    socket.on("assembly_state", (state) => {
        updateAssemblyState(state);
    });

    socket.on("motion_event", (event) => {
        addMotionEvent(event);
    });

    socket.on("ai_feedback", (data) => {
        showAIFeedback(data);
    });

    socket.on("assembly_result", (data) => {
        showAssemblyResult(data);
    });

    socket.on("chat_done", (data) => {
        addChatMessage("assistant", data.answer);
    });
}


function showAssemblyResult(data) {
    const result = $("assemblyResult");
    const title = $("assemblyResultTitle");
    const reason = $("assemblyResultReason");

    result.classList.remove("hidden");
    result.classList.remove("success");
    result.classList.remove("bad");
    result.classList.add(data.status === "success" ? "success" : "bad");

    title.textContent =
        data.status === "success"
            ? `Assembly ${data.assembly_id} successful`
            : `Assembly ${data.assembly_id} requires attention`;

    reason.textContent = data.reason;

    window.clearTimeout(window.assemblyResultTimer);
    window.assemblyResultTimer = window.setTimeout(() => {
        result.classList.add("hidden");
    }, 5000);
}


function updateAssemblyState(state) {
    $("assemblyId").textContent = state.assembly_id;
    $("tightenedCount").textContent = state.tightened;
    $("totalScrews").textContent = state.total_screws;
    $("successfulAssemblies").textContent = state.successful_assemblies;
    $("badAssemblies").textContent = state.bad_assemblies;
    $("lastMotion").textContent = state.last_motion;

    updatePhaseBadge(state.phase);
    updateScrews(state.screws);
    updateProgress(state.tightened, state.total_screws);
    updateWorkflowHint(state);
}


function updatePhaseBadge(phase) {
    const badge = $("phaseBadge");

    const labels = {
        "WAITING": "IDLE",
        "MOVING_TO_TIGHTEN": "MOVING",
        "TIGHTENED": "TIGHTENED",
        "COMPLETE_WAIT_IDLE": "COMPLETE",
    };

    badge.textContent = labels[phase] || phase;

    badge.className = "status-badge";

    if (phase === "COMPLETE_WAIT_IDLE") {
        badge.classList.add("complete");
    } else if (phase === "TIGHTENED") {
        badge.classList.add("progress");
    } else if (phase === "MOVING_TO_TIGHTEN") {
        badge.classList.add("moving");
    } else {
        badge.classList.add("idle");
    }
}


function updateScrews(screws) {
    document.querySelectorAll(".screw").forEach((element) => {
        const index = Number(element.dataset.index);

        element.classList.toggle(
            "tightened",
            Boolean(screws[index])
        );
    });
}


function updateProgress(tightened, total) {
    const percentage = total > 0
        ? Math.round((tightened / total) * 100)
        : 0;

    $("progressBar").style.width = `${percentage}%`;
    $("progressText").textContent = `${percentage}%`;
}


function updateWorkflowHint(state) {
    const hint = $("workflowHint");

    if (state.phase === "WAITING") {
        hint.textContent =
            "Move the tool to begin the assembly sequence.";
        return;
    }

    if (state.phase === "MOVING_TO_TIGHTEN") {
        hint.textContent =
            "Tool movement detected. Position the tool for the next screw.";
        return;
    }

    if (state.phase === "TIGHTENED") {
        hint.textContent =
            "Tightening detected. Move to the next screw.";
        return;
    }

    if (state.phase === "COMPLETE_WAIT_IDLE") {
        hint.textContent =
            "All four screws detected. Return the tool to idle to complete.";
        return;
    }

    hint.textContent = "Monitoring assembly activity.";
}


function addMotionEvent(event) {
    motionEvents.unshift(event);

    if (motionEvents.length > MAX_MOTION_LOG) {
        motionEvents.pop();
    }

    renderMotionLog();
}


function renderMotionLog() {
    const list = $("motionLog");

    if (motionEvents.length === 0) {
        list.innerHTML =
            `<li class="empty-log">Waiting for motion events...</li>`;
        return;
    }

    list.innerHTML = "";

    motionEvents.forEach((event) => {
        const item = document.createElement("li");

        const time = new Date(event.timestamp)
            .toLocaleTimeString([], {
                hour: "2-digit",
                minute: "2-digit",
                second: "2-digit"
            });

        item.innerHTML = `
            <div class="motion-name">
                <span class="motion-dot ${event.label}"></span>
                <strong>${escapeHTML(event.label)}</strong>
            </div>
            <span class="motion-time">${time}</span>
        `;

        list.appendChild(item);
    });
}


function showAIFeedback(data) {
    const container = $("aiFeedback");

    container.classList.remove("empty");
    container.classList.remove("warning");

    if (data.kind === "warning") {
        container.classList.add("warning");
    }

    const label = data.kind === "warning"
        ? "CHECK REQUIRED"
        : "AI ASSEMBLY REVIEW";

    container.innerHTML = `
        <div class="ai-mark">${data.kind === "warning" ? "!" : "AI"}</div>
        <div>
            <span class="ai-label">${label}</span>
            <p>${escapeHTML(data.text)}</p>
        </div>
    `;
}


function bindChatForm() {
    $("chatForm").addEventListener("submit", (event) => {
        event.preventDefault();

        const input = $("chatInput");
        const question = input.value.trim();

        if (!question) {
            return;
        }

        addChatMessage("operator", question);

        socket.emit("chat_query", {
            question: question
        });

        input.value = "";
    });
}


function addChatMessage(role, text) {
    const history = $("chatHistory");

    const placeholder = history.querySelector(".chat-placeholder");

    if (placeholder) {
        placeholder.remove();
    }

    const message = document.createElement("div");

    message.className =
        role === "operator"
            ? "chat-message operator"
            : "chat-message assistant";

    message.innerHTML = `
        <span class="chat-role">
            ${role === "operator" ? "OPERATOR" : "AI ASSISTANT"}
        </span>
        <p>${escapeHTML(text)}</p>
    `;

    history.appendChild(message);

    history.scrollTop = history.scrollHeight;
}


function escapeHTML(value) {
    return String(value)
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
        .replaceAll("'", "&#039;");
}
