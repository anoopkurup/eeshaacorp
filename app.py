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
    cmd_ask_to_refer,
    cmd_create,
    cmd_followup,
    cmd_followup2,
    cmd_followup3,
    cmd_referral,
    cmd_remind,
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
    not_locked = ~_yes(df, "locked")

    # send: status=pending AND not locked
    send_eligible = ((df["status"] == "pending") & not_locked).sum()

    # followup: responded=yes AND interested!=no AND followup_sent!=yes AND not locked
    fu1_eligible = (
        _yes(df, "responded")
        & ~_no(df, "interested")
        & ~_yes(df, "followup_sent")
        & not_locked
    ).sum()

    # followup2: responded=yes AND interested!=no AND followup_sent=yes AND followup2_sent!=yes AND not locked
    fu2_eligible = (
        _yes(df, "responded")
        & ~_no(df, "interested")
        & _yes(df, "followup_sent")
        & ~_yes(df, "followup2_sent")
        & not_locked
    ).sum()

    # followup3: responded=yes AND interested!=no AND followup2_sent=yes AND followup3_sent!=yes AND not locked
    fu3_eligible = (
        _yes(df, "responded")
        & ~_no(df, "interested")
        & _yes(df, "followup2_sent")
        & ~_yes(df, "followup3_sent")
        & not_locked
    ).sum()

    # remind: status=sent AND responded=no AND reminder_sent!=yes AND not locked
    remind_eligible = (
        (df["status"] == "sent")
        & _no(df, "responded")
        & ~_yes(df, "reminder_sent")
        & not_locked
    ).sum()

    # askrefer: responded=yes AND ask_to_refer_sent!=yes AND not locked
    askrefer_eligible = (
        _yes(df, "responded")
        & ~_yes(df, "ask_to_refer_sent")
        & not_locked
    ).sum()

    # referral: referrer=yes AND referral_sent!=yes AND not locked
    referral_eligible = (
        _yes(df, "referrer")
        & ~_yes(df, "referral_sent")
        & not_locked
    ).sum()

    return {
        "send":      int(send_eligible),
        "followup":  int(fu1_eligible),
        "followup2": int(fu2_eligible),
        "followup3": int(fu3_eligible),
        "remind":    int(remind_eligible),
        "askrefer":  int(askrefer_eligible),
        "referral":  int(referral_eligible),
    }


def _campaign_stats(tracking):
    df = tracking
    sent = int((df["status"] == "sent").sum())
    responded = int(_yes(df, "responded").sum())
    return {
        "total":             len(df),
        "sent":              sent,
        "failed":            int((df["status"] == "failed").sum()),
        "pending":           int((df["status"] == "pending").sum()),
        "responded":         responded,
        "interested":        int(_yes(df, "interested").sum()),
        "not_interested":    int(_no(df, "interested").sum()),
        "paid":              int(_yes(df, "paid").sum()),
        "followup_done":     int(_yes(df, "followup_sent").sum()),
        "followup2_done":    int(_yes(df, "followup2_sent").sum()),
        "followup3_done":    int(_yes(df, "followup3_sent").sum()),
        "reminder_done":     int(_yes(df, "reminder_sent").sum()),
        "referrers":         int(_yes(df, "referrer").sum()),
        "referral_done":     int(_yes(df, "referral_sent").sum()),
        "ask_to_refer_done": int(_yes(df, "ask_to_refer_sent").sum()),
    }


_EMPTY_STATS = {
    "total": 0, "sent": 0, "failed": 0, "pending": 0,
    "responded": 0, "interested": 0, "not_interested": 0, "paid": 0,
    "followup_done": 0, "followup2_done": 0, "followup3_done": 0,
    "reminder_done": 0, "referrers": 0, "referral_done": 0,
    "ask_to_refer_done": 0,
}

_EMPTY_COUNTS = {
    "send": 0, "followup": 0, "followup2": 0, "followup3": 0,
    "remind": 0, "askrefer": 0, "referral": 0,
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
        "send":      cmd_send,
        "followup":  cmd_followup,
        "followup2": cmd_followup2,
        "followup3": cmd_followup3,
        "remind":    cmd_remind,
        "askrefer":  cmd_ask_to_refer,
        "referral":  cmd_referral,
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
