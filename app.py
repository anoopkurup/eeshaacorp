import builtins
import contextlib
import io
import os
import queue
import threading
import uuid
from pathlib import Path

from flask import Flask, Response, jsonify, render_template, request, stream_with_context

# Pin cwd so relative CAMPAIGNS_DIR = Path("campaigns") resolves correctly
os.chdir(Path(__file__).parent)

from whatsapp_sender import (
    cmd_create,
    cmd_remind1,
    cmd_remind2,
    cmd_remind3,
    cmd_remind_final,
    cmd_send,
    list_campaigns,
    load_tracking,
    request_shutdown,
)

app = Flask(__name__)

# ── Job state ────────────────────────────────────────────────────────────────

_jobs: dict = {}               # job_id -> {state, queue, campaign, cmd}
_active: dict = {}             # campaign -> job_id (one job per campaign at a time)
_lock = threading.Lock()


class _QueueWriter(io.TextIOBase):
    """Redirect print() output into a queue for SSE streaming."""
    def __init__(self, q: queue.Queue):
        self._q = q

    def write(self, s: str) -> int:
        if s:
            self._q.put(s)
        return len(s)

    def flush(self):
        pass


def _run_job(job_id: str, campaign: str, cmd_fn, *args):
    job = _jobs[job_id]
    q = job["queue"]
    original_input = builtins.input

    try:
        builtins.input = lambda prompt="": ""   # skip "Press Enter" gate
        with contextlib.redirect_stdout(_QueueWriter(q)):
            try:
                cmd_fn(*args)
            except ValueError as e:
                if "signal only works in main thread" not in str(e):
                    raise
            except SystemExit as e:
                q.put(f"\n[Exited with code {e.code}]")
                job["state"] = "error"
                return
        job["state"] = "done"
    except Exception as e:
        q.put(f"\n[Unexpected error: {e}]")
        job["state"] = "error"
    finally:
        builtins.input = original_input
        with _lock:
            _active.pop(campaign, None)


# ── Button eligibility helpers ────────────────────────────────────────────────

def _yes(df, col):
    """Boolean mask: column value (case-insensitive) equals 'yes'."""
    return df[col].astype(str).str.lower() == "yes"


def _no(df, col):
    """Boolean mask: column value (case-insensitive) equals 'no'."""
    return df[col].astype(str).str.lower() == "no"


def _button_counts(tracking):
    """Count eligible contacts for each command button.

    Mirrors filter logic in whatsapp_sender.py cmd_* functions.
    """
    df = tracking
    not_submitted = ~_yes(df, "submitted")

    return {
        "send":         int((df["status"] == "pending").sum()),
        "remind1":      int((
            (df["status"] == "sent")
            & not_submitted
            & ~_yes(df, "reminder1_sent")
        ).sum()),
        "remind2":      int((
            _yes(df, "reminder1_sent")
            & not_submitted
            & ~_yes(df, "reminder2_sent")
        ).sum()),
        "remind3":      int((
            _yes(df, "reminder2_sent")
            & not_submitted
            & ~_yes(df, "reminder3_sent")
        ).sum()),
        "remind_final": int((
            _yes(df, "reminder3_sent")
            & not_submitted
            & ~_yes(df, "reminder_final_sent")
        ).sum()),
    }


def _campaign_stats(tracking):
    df = tracking
    sent = int((df["status"] == "sent").sum())
    submitted = int(_yes(df, "submitted").sum())
    return {
        "total":               len(df),
        "sent":                sent,
        "failed":              int((df["status"] == "failed").sum()),
        "pending":             int((df["status"] == "pending").sum()),
        "submitted":           submitted,
        "pending_submission":  max(0, sent - submitted),
        "reminder1_done":      int(_yes(df, "reminder1_sent").sum()),
        "reminder2_done":      int(_yes(df, "reminder2_sent").sum()),
        "reminder3_done":      int(_yes(df, "reminder3_sent").sum()),
        "reminder_final_done": int(_yes(df, "reminder_final_sent").sum()),
    }


_EMPTY_STATS = {
    "total": 0, "sent": 0, "failed": 0, "pending": 0,
    "submitted": 0, "pending_submission": 0,
    "reminder1_done": 0, "reminder2_done": 0,
    "reminder3_done": 0, "reminder_final_done": 0,
}

_EMPTY_COUNTS = {
    "send": 0, "remind1": 0, "remind2": 0, "remind3": 0, "remind_final": 0,
}


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    campaigns = list_campaigns()
    return render_template("index.html", campaigns=campaigns)


@app.route("/api/campaigns")
def api_campaigns():
    return jsonify(list_campaigns())


@app.route("/api/status/<campaign>")
def api_status(campaign):
    campaign_dir = Path("campaigns") / campaign
    if not campaign_dir.exists():
        return jsonify({"error": "Campaign not found"}), 404

    tracking = load_tracking(campaign_dir)
    with _lock:
        running_job_id = _active.get(campaign)
    is_running = running_job_id is not None
    running_cmd = _jobs[running_job_id]["cmd"] if is_running else None

    if tracking.empty:
        stats = dict(_EMPTY_STATS)
        counts = dict(_EMPTY_COUNTS)
        # Send button is always eligible if campaign exists but no tracking yet
        contacts_path = campaign_dir / "contacts.csv"
        if contacts_path.exists():
            counts["send"] = 1  # show as enabled so user can kick off first send
    else:
        stats = _campaign_stats(tracking)
        counts = _button_counts(tracking)

    return jsonify({
        "stats": stats,
        "button_counts": counts,
        "is_running": is_running,
        "running_cmd": running_cmd,
    })


@app.route("/api/run/<campaign>/<cmd>", methods=["POST"])
def api_run(campaign, cmd):
    cmd_map = {
        "send":         cmd_send,
        "remind1":      cmd_remind1,
        "remind2":      cmd_remind2,
        "remind3":      cmd_remind3,
        "remind_final": cmd_remind_final,
    }
    if cmd not in cmd_map:
        return jsonify({"error": f"Unknown command: {cmd}"}), 400

    with _lock:
        if campaign in _active:
            return jsonify({"error": "A command is already running for this campaign"}), 409
        job_id = str(uuid.uuid4())
        _jobs[job_id] = {"state": "running", "queue": queue.Queue(), "campaign": campaign, "cmd": cmd}
        _active[campaign] = job_id

    t = threading.Thread(target=_run_job, args=(job_id, campaign, cmd_map[cmd], campaign), daemon=True)
    t.start()
    return jsonify({"job_id": job_id})


@app.route("/api/stream/<job_id>")
def api_stream(job_id):
    def generate():
        job = _jobs.get(job_id)
        if not job:
            yield "data: [Job not found]\n\ndata: __DONE__\n\n"
            return
        q = job["queue"]
        while True:
            try:
                chunk = q.get(timeout=1.0)
                # Escape each line separately so SSE framing is preserved
                for line in chunk.splitlines(keepends=False):
                    yield f"data: {line}\n\n"
            except queue.Empty:
                if job["state"] in ("done", "error"):
                    while not q.empty():
                        chunk = q.get_nowait()
                        for line in chunk.splitlines(keepends=False):
                            yield f"data: {line}\n\n"
                    yield "data: __DONE__\n\n"
                    return
                yield ": heartbeat\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/api/job/<job_id>")
def api_job(job_id):
    job = _jobs.get(job_id)
    if not job:
        return jsonify({"error": "Not found"}), 404
    return jsonify({"state": job["state"], "cmd": job["cmd"], "campaign": job["campaign"]})


@app.route("/api/cancel/<job_id>", methods=["POST"])
def api_cancel(job_id):
    """Cooperative cancel: set the shutdown flag that the command loop polls.

    The running command will finish its current message, close the browser,
    and save tracking.csv before exiting — same path as Ctrl+C on the CLI.
    """
    job = _jobs.get(job_id)
    if not job:
        return jsonify({"error": "Not found"}), 404
    if job["state"] != "running":
        return jsonify({"error": f"Job is {job['state']}, not running"}), 400
    request_shutdown(reason=f"cancel from UI (job {job_id[:8]})")
    return jsonify({"ok": True, "message": "Cancel requested; will stop after current message."})


@app.route("/api/create", methods=["POST"])
def api_create():
    data = request.get_json(force=True)
    name = (data.get("campaign_name") or "").strip()
    if not name:
        return jsonify({"error": "campaign_name is required"}), 400
    contacts_path = data.get("contacts_path") or None
    message_path = data.get("message_path") or None

    capture = io.StringIO()
    try:
        with contextlib.redirect_stdout(capture):
            cmd_create(name, contacts_path, message_path)
    except SystemExit:
        return jsonify({"error": capture.getvalue()}), 400

    return jsonify({"campaign": name, "output": capture.getvalue()})


if __name__ == "__main__":
    app.run(debug=False, threaded=True, port=5000)
