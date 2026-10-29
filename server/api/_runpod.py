"""גישור ל-RunPod (ivrit-ai/runpod-serverless): שליחת אודיו, המתנה לתוצאה, ופענוח הסגמנטים."""
import os

import requests

MODEL = os.environ.get("IVRIT_MODEL", "ivrit-ai/whisper-large-v3-turbo-ct2")


def _base():
    ep = os.environ.get("RUNPOD_ENDPOINT_ID", "").strip()
    key = os.environ.get("RUNPOD_API_KEY", "").strip()
    if not ep or not key:
        raise RuntimeError("RunPod not configured: set RUNPOD_API_KEY and RUNPOD_ENDPOINT_ID in Vercel")
    return f"https://api.runpod.ai/v2/{ep}", {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}


def payload(blob_b64=None, url=None, diarize=False, language="he", num_speakers=None):
    args = {"language": language, "diarize": bool(diarize),
            "output_options": {"word_timestamps": False, "extra_data": False}}
    if diarize and num_speakers and int(num_speakers) > 1:
        args["diarization_args"] = {"num_speakers": int(num_speakers)}
    if url:
        args["url"] = url
    else:
        args["blob"] = blob_b64
    return {"input": {"type": "url" if url else "blob", "model": MODEL,
                      "engine": "stable-whisper" if diarize else "faster-whisper",
                      "streaming": False, "transcribe_args": args}}


def submit(p, sync_wait_s=0):
    """sync_wait_s>0: /runsync (מחכה עד ~90 שניות); אחרת /run. מחזיר את תשובת RunPod."""
    base, h = _base()
    if sync_wait_s:
        r = requests.post(f"{base}/runsync?wait={int(sync_wait_s * 1000)}", json=p, headers=h, timeout=sync_wait_s + 30)
    else:
        r = requests.post(f"{base}/run", json=p, headers=h, timeout=60)
    r.raise_for_status()
    return r.json()


def status(job_id):
    base, h = _base()
    r = requests.get(f"{base}/status/{job_id}", headers=h, timeout=60)
    r.raise_for_status()
    return r.json()


def segments(output):
    """הפלט של ה-worker: [{'result': [[{'type':'segments','data':[seg,...]}, ...], ...]}] — אוספים את כל הסגמנטים."""
    out = []

    def walk(x):
        if isinstance(x, dict):
            if x.get("type") == "segments" and isinstance(x.get("data"), list):
                out.extend(s for s in x["data"] if isinstance(s, dict))
                return
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)
    walk(output)
    clean = []
    for s in out:
        spk = s.get("speakers") or s.get("speaker")
        if isinstance(spk, list):
            spk = spk[0] if spk else None
        clean.append({"start": float(s.get("start") or 0), "end": float(s.get("end") or 0),
                      "text": (s.get("text") or "").strip(), "speaker": spk})
    return clean


def result_from(resp):
    """{status, text?, segments?, error?} מתשובת RunPod (runsync או status)."""
    st = resp.get("status")
    if st == "COMPLETED":
        segs = segments(resp.get("output"))
        return {"status": "done", "text": " ".join(s["text"] for s in segs if s["text"]).strip(), "segments": segs}
    if st in ("FAILED", "CANCELLED", "TIMED_OUT"):
        return {"status": "error", "error": str(resp.get("error") or st)}
    return {"status": "pending", "job_id": resp.get("id"), "runpod_status": st}
