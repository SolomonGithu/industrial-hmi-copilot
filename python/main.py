# SPDX-FileCopyrightText: Copyright (C) Arduino s.r.l. and/or its affiliated companies
# SPDX-License-Identifier: MPL-2.0

# ************* Industrial HMI Copilot *************

from collections import deque
from datetime import datetime, UTC
import re
import threading
import time

from arduino.app_utils import App, Bridge
from arduino.app_bricks.llm import LargeLanguageModel
from arduino.app_bricks.web_ui import WebUI


# ---------------------------------------------------------------------------
# Configs
# ---------------------------------------------------------------------------
SCREWS_PER_ASSEMBLY = 4
MAX_ASSEMBLY_HISTORY = 10
MAX_MOTION_LOG = 30

# Incomplete assembly detection.
# "idle" between screws is normal. An assembly with at least one screw is
# closed as defective only after five continuous minutes of confirmed idle.
# Set to 0 to disable.
ASSEMBLY_TIMEOUT_S = 10

# Prompt size / answer size controls
PROMPT_HISTORY_ITEMS = 3        # how many past assemblies go into the prompt
MAX_FEEDBACK_SENTENCES = 2      # hard cap applied to the LLM answer
LOG_DUPLICATE_MOTION = False    # True = also log repeated (ignored) classes

# Automatic LLM feedback is queued after five continuous minutes of idle.
# Set to 0 to disable periodic feedback (defect feedback still works).
PERIODIC_LLM_FEEDBACK_INTERVAL_S = 5 * 60

ui = WebUI()
llm = LargeLanguageModel()


# ---------------------------------------------------------------------------
# Shared application state
# ---------------------------------------------------------------------------
motion_log = deque(maxlen=MAX_MOTION_LOG)
assembly_history = deque(maxlen=MAX_ASSEMBLY_HISTORY)

assembly_number = 1
successful_assemblies = 0
bad_assemblies = 0

screws = [False] * SCREWS_PER_ASSEMBLY
tightening_count = 0
workflow_phase = "WAITING"

last_semantic_class = "unknown"
last_motion_time = time.monotonic()
# Start timing only after the classifier explicitly reports the idle class.
idle_since = None

# The Bridge callback and the main loop run in different threads and both
# change the assembly state, so every state change happens under this lock.
state_lock = threading.Lock()

llm_busy = False
pending_feedback_reason = None
last_feedback_time = time.monotonic()
last_idle_broadcast = 0.0
chat_requests = deque(maxlen=8)


# ---------------------------------------------------------------------------
# Utility functions
# ---------------------------------------------------------------------------
def utc_timestamp():
    return datetime.now(UTC).isoformat()


def log(tag, message):
    """Single place for all console logs."""
    print(f"[{datetime.now().strftime('%H:%M:%S')}] [{tag}] {message}", flush=True)


def reset_current_product():
    global screws
    global tightening_count
    global workflow_phase

    screws = [False] * SCREWS_PER_ASSEMBLY
    tightening_count = 0
    workflow_phase = "WAITING"


def current_product_snapshot():
    return {
        "assembly_id": assembly_number,
        "screws": list(screws),
        "tightened": tightening_count,
        "total_screws": SCREWS_PER_ASSEMBLY,
        "phase": workflow_phase,
        "idle_duration_s": (round(time.monotonic() - idle_since, 1) if last_semantic_class == "idle" and idle_since is not None else 0),
    }


def broadcast_state(event=None, message=None):
    payload = {
        "assembly_id": assembly_number,
        "screws": list(screws),
        "tightened": tightening_count,
        "total_screws": SCREWS_PER_ASSEMBLY,
        "successful_assemblies": successful_assemblies,
        "bad_assemblies": bad_assemblies,
        "phase": workflow_phase,
        "last_motion": last_semantic_class,
        "idle_duration_s": (round(time.monotonic() - idle_since, 1) if last_semantic_class == "idle" and idle_since is not None else 0),
        "timestamp": utc_timestamp(),
    }

    if event is not None:
        payload["event"] = event

    if message is not None:
        payload["message"] = message

    ui.send_message("assembly_state", payload)


def add_motion_log(raw_label, label, mcu_timestamp_ms):
    entry = {
        "timestamp": utc_timestamp(),
        "raw_label": raw_label,
        "label": label,
        "mcu_timestamp_ms": mcu_timestamp_ms,
    }
    motion_log.appendleft(entry)
    ui.send_message("motion_event", entry)


def record_assembly_result(status, reason):
    global successful_assemblies
    global bad_assemblies

    result = {
        "assembly_id": assembly_number,
        "status": status,
        "reason": reason,
        "tightenings": tightening_count,
        "expected": SCREWS_PER_ASSEMBLY,
        "timestamp": utc_timestamp(),
    }

    assembly_history.appendleft(result)

    if status == "success":
        successful_assemblies += 1
    else:
        bad_assemblies += 1

    log("RESULT", f"assembly {result['assembly_id']} -> {status.upper()} ({reason})")

    if status == "bad":
        request_ai_feedback("defective assembly detected")

    return result


def finish_assembly(status, reason):
    """Close the current assembly: store the result, tell the UI, start the next one."""
    global assembly_number

    result = record_assembly_result(status, reason)
    ui.send_message(
        "assembly_result",
        {
            "assembly_id": result["assembly_id"],
            "status": status,
            "screws": list(screws),
            "reason": result["reason"],
            "timestamp": result["timestamp"],
        },
    )
    assembly_number += 1
    reset_current_product()
    return result


def count_screw():
    """Mark the next screw as tightened and return the phase that follows."""
    global tightening_count

    screws[tightening_count] = True
    tightening_count += 1

    if tightening_count >= SCREWS_PER_ASSEMBLY:
        return "COMPLETE_WAIT_IDLE"
    return "TIGHTENED"


# ---------------------------------------------------------------------------
# Assembly workflow state machine
#
# Valid sequences for a good assembly (the "moving" class is optional):
#   idle, tightening, idle, tightening, idle, tightening, idle, tightening, idle
#   idle, moving, tightening, moving, tightening, ... , tightening, idle
#
# Phases:
#   WAITING             no screw yet
#   MOVING_TO_TIGHTEN   tool is moving towards a screw
#   TIGHTENED           1..N-1 screws done, waiting for the next one
#   COMPLETE_WAIT_IDLE  all screws done, waiting for the tool to go idle
# ---------------------------------------------------------------------------
def handle_motion_transition(raw_label, label, mcu_timestamp_ms):
    global last_semantic_class
    global last_motion_time
    global workflow_phase
    global idle_since

    with state_lock:
        now = time.monotonic()
        last_motion_time = now
        last_semantic_class = label
        idle_since = now if label == "idle" else None

        add_motion_log(raw_label, label, mcu_timestamp_ms)

        event_message = None
        phase = workflow_phase

        if phase in ("WAITING", "MOVING_TO_TIGHTEN", "TIGHTENED"):
            if label == "tightening":
                first_screw = tightening_count == 0
                workflow_phase = count_screw()

                if workflow_phase == "COMPLETE_WAIT_IDLE":
                    event_message = f"Assembly {assembly_number}: all {SCREWS_PER_ASSEMBLY} screws tightened. Return to idle."
                elif first_screw:
                    event_message = f"Assembly {assembly_number} started: screw {tightening_count}/{SCREWS_PER_ASSEMBLY} tightened."
                else:
                    event_message = f"Assembly {assembly_number}: screw {tightening_count}/{SCREWS_PER_ASSEMBLY} tightened."

            elif label == "moving":
                if phase == "WAITING":
                    workflow_phase = "MOVING_TO_TIGHTEN"
                    event_message = f"Assembly {assembly_number} started. Waiting for first screw."
                elif phase == "TIGHTENED":
                    workflow_phase = "MOVING_TO_TIGHTEN"
                    event_message = f"Assembly {assembly_number}: moving to next screw."

            elif label == "idle":
                if phase == "MOVING_TO_TIGHTEN":
                    if tightening_count == 0:
                        workflow_phase = "WAITING"
                        event_message = f"Assembly {assembly_number}: reset before completion."
                    else:
                        workflow_phase = "TIGHTENED"
                        event_message = f"Assembly {assembly_number}: idle, waiting for screw {tightening_count + 1}/{SCREWS_PER_ASSEMBLY}."
                elif phase == "TIGHTENED":
                    event_message = f"Assembly {assembly_number}: idle, waiting for screw {tightening_count + 1}/{SCREWS_PER_ASSEMBLY}."

        elif phase == "COMPLETE_WAIT_IDLE":
            if label == "moving":
                event_message = f"Assembly {assembly_number}: waiting for idle state."
            elif label == "idle":
                result = finish_assembly("success", f"All {SCREWS_PER_ASSEMBLY} screws tightened successfully.")
                event_message = f"Assembly {result['assembly_id']} completed successfully."
            elif label == "tightening":
                result = finish_assembly("bad", "Extra tightening action observed.")
                event_message = f"Assembly {result['assembly_id']} failed due to extra tightening."

        else:
            reset_current_product()
            event_message = f"Assembly {assembly_number}: state reset."

        log("STATE", f"phase {phase} -> {workflow_phase} | {event_message or 'no change'}")
        broadcast_state(event={"type": "workflow", "text": event_message or f"Motion: {label}"})


def check_assembly_timeout():
    """Close an unfinished assembly after five minutes of confirmed idle."""
    if ASSEMBLY_TIMEOUT_S <= 0:
        return

    with state_lock:
        if tightening_count == 0 or workflow_phase == "COMPLETE_WAIT_IDLE":
            return
        if last_semantic_class != "idle" or idle_since is None:
            return

        idle_duration = time.monotonic() - idle_since
        if idle_duration < ASSEMBLY_TIMEOUT_S:
            return

        done = tightening_count
        result = finish_assembly(
            "bad",
            f"Operator remained idle for {int(idle_duration)} seconds after "
            f"{done}/{SCREWS_PER_ASSEMBLY} screws."
        )
        log("STATE", f"idle timeout ({idle_duration:.0f}s) -> assembly {result['assembly_id']} closed as defective")
        broadcast_state(event={
            "type": "workflow",
            "text": f"Assembly {result['assembly_id']} incomplete: {done}/{SCREWS_PER_ASSEMBLY} screws; operator idle for {int(idle_duration)} seconds."
        })


# ---------------------------------------------------------------------------
# STM32 MCU -> Linux Bridge callback
# ---------------------------------------------------------------------------
def on_motion_classification(raw_label, normalized_label, mcu_timestamp_ms):
    try:
        raw_label = str(raw_label)
        normalized_label = str(normalized_label)

        if normalized_label not in {"idle", "moving", "tightening"}:
            log("MOTION", f"ignored unknown class: raw='{raw_label}' normalized='{normalized_label}'")
            return

        if normalized_label == last_semantic_class:
            if LOG_DUPLICATE_MOTION:
                log("MOTION", f"raw='{raw_label}' class={normalized_label} (unchanged, ignored)")
            return

        log("MOTION", f"raw='{raw_label}' class={normalized_label} mcu_ts={mcu_timestamp_ms}ms")
        handle_motion_transition(raw_label, normalized_label, int(mcu_timestamp_ms))
    except Exception as exc:
        log("BRIDGE ERROR", exc)


Bridge.provide("motion_classification", on_motion_classification)


# ---------------------------------------------------------------------------
# Compact prompts (small prompt = fast answer)
# ---------------------------------------------------------------------------

def describe_result(item):
    """Turn one assembly result into a plain-English fact (small models read
    sentences much better than codes like '#2 BAD 2/4')."""
    asm = item["assembly_id"]
    done = item["tightenings"]
    total = item["expected"]

    if item["status"] == "success":
        return f"Assembly {asm} succeeded: all {total} screws were tightened."
    if done < total:
        return f"Assembly {asm} failed: only {done} of {total} screws were tightened."
    return f"Assembly {asm} failed: an extra tightening was done after all {total} screws."


def build_assembly_context():
    current = current_product_snapshot()
    history = list(assembly_history)[:PROMPT_HISTORY_ITEMS]  # newest first

    lines = [f"Each assembly needs {SCREWS_PER_ASSEMBLY} screws."]

    if history:
        for index, item in enumerate(history):
            prefix = "Most recent finished assembly: " if index == 0 else "Earlier: "
            lines.append(prefix + describe_result(item))
    else:
        lines.append("No assembly has finished yet.")

    lines.append(
        f"Assembly {current['assembly_id']} is in progress: "
        f"{current['tightened']} of {current['total_screws']} screws tightened so far."
    )

    if last_semantic_class == "idle" and idle_since is not None:
        idle_seconds = int(time.monotonic() - idle_since)
        lines.append(f"The operator has been idle for {idle_seconds} seconds.")

    return "\n".join(lines)


def create_periodic_feedback_prompt(context):
    return (
        "You are an assistant for a screw assembly line.\n"
        "Facts:\n"
        f"{context}\n"
        "Write 2 short sentences. First: say what happened in the most recent finished assembly. "
        "Second: give one short advice for the operator. "
        "Use only the facts. No labels."
    )


def create_chat_prompt(question):
    context = build_assembly_context()
    return (
        "You are an assistant for a screw assembly line. "
        "Answer using only these facts.\n"
        "Facts:\n"
        f"{context}\n"
        f"Question: {question}\n"
        "Answer in 1 or 2 short sentences. No labels."
    )


_LABEL_PREFIX = re.compile(r"^\s*(feedback|answer|response|assistant)\s*[:\-]\s*", re.IGNORECASE)
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")


def clean_llm_response(text, max_sentences=None):
    if not text:
        return ""
    text = str(text).strip().replace("```", "").replace("**", "").replace("__", "")
    text = " ".join(line.strip() for line in text.splitlines() if line.strip())

    # Remove "Feedback:" style labels, including ones in the middle of the text
    text = re.sub(r"\b(feedback|answer|response)\s*:\s*", "", text, flags=re.IGNORECASE)
    text = _LABEL_PREFIX.sub("", text).strip()

    if max_sentences:
        sentences = _SENTENCE_SPLIT.split(text)
        text = " ".join(sentences[:max_sentences])

    return text


# ---------------------------------------------------------------------------
# LLM queue processing
# ---------------------------------------------------------------------------

def request_ai_feedback(reason):
    global pending_feedback_reason
    pending_feedback_reason = reason


def schedule_periodic_feedback():
    """Queue automatic feedback only after five continuous minutes of idle."""
    if PERIODIC_LLM_FEEDBACK_INTERVAL_S <= 0 or pending_feedback_reason is not None:
        return
    if last_semantic_class != "idle" or idle_since is None:
        return

    idle_duration = time.monotonic() - idle_since
    if idle_duration >= PERIODIC_LLM_FEEDBACK_INTERVAL_S:
        if time.monotonic() - last_feedback_time >= PERIODIC_LLM_FEEDBACK_INTERVAL_S:
            request_ai_feedback("operator idle for five minutes")


def run_periodic_feedback():
    global llm_busy
    global pending_feedback_reason
    global last_feedback_time

    if llm_busy:
        return

    reason = pending_feedback_reason
    if reason is None:
        return

    pending_feedback_reason = None
    last_feedback_time = time.monotonic()  # restart the interval from every feedback
    llm_busy = True

    try:
        context = build_assembly_context()
        prompt = create_periodic_feedback_prompt(context)
        log("LLM PROMPT", f"(feedback, reason='{reason}', {len(prompt)} chars)\n{prompt}")

        started = time.monotonic()
        response_parts = []
        for chunk in llm.chat_stream(prompt):
            response_parts.append(str(chunk))
        raw_response = "".join(response_parts)
        elapsed = time.monotonic() - started

        log("LLM RAW", f"({elapsed:.1f}s)\n{raw_response}")
        response = clean_llm_response(raw_response, MAX_FEEDBACK_SENTENCES)
        log("LLM RESPONSE", f"(feedback, {elapsed:.1f}s) {response}")

        if response:
            ui.send_message(
                "ai_feedback",
                {
                    "text": response,
                    "kind": "warning" if reason == "defective assembly detected" else "info",
                    "reason": reason,
                    "timestamp": utc_timestamp(),
                },
            )
    except Exception as exc:
        log("LLM ERROR", exc)
    finally:
        llm_busy = False


def process_chat_requests():
    global llm_busy

    if llm_busy:
        return

    try:
        question = chat_requests.popleft()
    except IndexError:
        return

    llm_busy = True

    try:
        prompt = create_chat_prompt(question)
        log("LLM PROMPT", f"(chat, {len(prompt)} chars)\n{prompt}")
        ui.send_message("chat_started", {"question": question})

        started = time.monotonic()
        response_parts = []

        for chunk in llm.chat_stream(prompt):
            chunk = str(chunk)
            response_parts.append(chunk)
            ui.send_message("chat_token", {"chunk": chunk})

        raw_response = "".join(response_parts)
        elapsed = time.monotonic() - started

        log("LLM RAW", f"({elapsed:.1f}s)\n{raw_response}")
        response = clean_llm_response(raw_response, MAX_FEEDBACK_SENTENCES)
        log("LLM RESPONSE", f"(chat, {elapsed:.1f}s) {response}")

        ui.send_message(
            "chat_done",
            {
                "question": question,
                "answer": response or "i could not generate an answer.",
                "timestamp": utc_timestamp(),
            },
        )
        log("CHAT", "sent 'chat_done' to the web UI")
    except Exception as exc:
        log("CHAT ERROR", exc)
        ui.send_message(
            "chat_done",
            {
                "question": question,
                "answer": "assistant error.",
                "timestamp": utc_timestamp(),
            },
        )
    finally:
        llm_busy = False


# ---------------------------------------------------------------------------
# Web UI handlers
# ---------------------------------------------------------------------------

def on_chat_message(sid, data):
    if isinstance(data, dict):
        question = str(data.get("question", "")).strip()
    else:
        question = str(data).strip()

    if question:
        log("CHAT", f"question received: {question[:300]}")
        chat_requests.append(question[:300])


def on_client_connect(sid):
    payload = {
        "assembly_id": assembly_number,
        "screws": list(screws),
        "tightened": tightening_count,
        "total_screws": SCREWS_PER_ASSEMBLY,
        "successful_assemblies": successful_assemblies,
        "bad_assemblies": bad_assemblies,
        "phase": workflow_phase,
        "last_motion": last_semantic_class,
        "idle_duration_s": (round(time.monotonic() - idle_since, 1) if last_semantic_class == "idle" and idle_since is not None else 0),
        "timestamp": utc_timestamp(),
    }
    ui.send_message("assembly_state", payload, room=sid)


ui.on_message("chat_query", on_chat_message)
ui.on_connect(on_client_connect)


# ---------------------------------------------------------------------------
# Main App loop
# ---------------------------------------------------------------------------
def broadcast_idle_duration():
    """Refresh the UI idle timer once per second while the classifier says idle."""
    global last_idle_broadcast

    if last_semantic_class != "idle" or idle_since is None:
        return

    now = time.monotonic()
    if now - last_idle_broadcast >= 1.0:
        last_idle_broadcast = now
        idle_seconds = int(now - idle_since)
        broadcast_state(event={
            "type": "idle_timer",
            "text": f"Operator idle for {idle_seconds} seconds."
        })


def user_loop():
    while True:
        process_chat_requests()
        check_assembly_timeout()
        broadcast_idle_duration()
        schedule_periodic_feedback()
        run_periodic_feedback()
        time.sleep(0.1)


log("SYSTEM", "optimized assistant started.")
App.run(user_loop=user_loop)