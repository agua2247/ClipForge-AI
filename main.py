import os, uuid, sqlite3, subprocess, shutil, json, re, asyncio, time, random, mimetypes, secrets, importlib.util
import logging
from googleapiclient.errors import HttpError
import httpx
from cryptography.fernet import Fernet, InvalidToken
from dotenv import load_dotenv
from pathlib import Path
from datetime import datetime, timezone, timedelta
from urllib.parse import urlencode
from fastapi import FastAPI, UploadFile, File, Form, HTTPException, BackgroundTasks, Request
from fastapi.responses import HTMLResponse
from starlette.concurrency import run_in_threadpool
import hmac
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")
DATA = Path(os.getenv("CLIPFORGE_DATA_DIR", str(ROOT / "data"))).expanduser()
UPLOADS, OUTPUTS, TRANSCRIPTS = DATA/"uploads", DATA/"clips", DATA/"transcripts"
for p in (UPLOADS, OUTPUTS, TRANSCRIPTS): p.mkdir(parents=True, exist_ok=True)
DB = DATA/"clipforge.db"
app = FastAPI(title="ClipForge AI Backend", version="0.3.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
app.mount("/clips", StaticFiles(directory=OUTPUTS), name="clips")

@app.middleware("http")
async def require_private_instance_token(request: Request, call_next):
    # Local development may run without a token. Any non-local deployment must
    # configure CLIPFORGE_ACCESS_TOKEN; the Google OAuth callback is state-protected.
    if request.method == "OPTIONS" or request.url.path in ("/api/health", "/api/youtube/callback"):
        return await call_next(request)
    expected = os.getenv("CLIPFORGE_ACCESS_TOKEN", "").strip()
    client_host = request.client.host if request.client else ""
    if not expected and client_host not in ("127.0.0.1", "::1", "localhost", "testclient"):
        from fastapi.responses import JSONResponse
        return JSONResponse({"detail": "Private instance is not configured. Set CLIPFORGE_ACCESS_TOKEN."}, status_code=503)
    if expected:
        supplied = request.headers.get("authorization", "")
        supplied = supplied[7:].strip() if supplied.lower().startswith("bearer ") else ""
        if not hmac.compare_digest(supplied, expected):
            from fastapi.responses import JSONResponse
            return JSONResponse({"detail": "Unauthorized. Enter the ClipForge access token in Settings."}, status_code=401)
    return await call_next(request)

def db():
    c=sqlite3.connect(DB); c.row_factory=sqlite3.Row; return c

def init():
    with db() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS jobs(
          id TEXT PRIMARY KEY, source_name TEXT, source_path TEXT, clip_count INTEGER,
          clip_length INTEGER, status TEXT, message TEXT, created_at TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS clips(
          id TEXT PRIMARY KEY, job_id TEXT, filename TEXT, start REAL, duration INTEGER,
          title TEXT, transcript TEXT, created_at TEXT, published INTEGER DEFAULT 0)""")
        c.execute("""CREATE TABLE IF NOT EXISTS schedules(
          id TEXT PRIMARY KEY, clip_id TEXT, publish_at TEXT, status TEXT, message TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS integrations(
          name TEXT PRIMARY KEY, secret BLOB NOT NULL, updated_at TEXT NOT NULL)""")
        c.execute("""CREATE TABLE IF NOT EXISTS oauth_states(
          state TEXT PRIMARY KEY, created_at TEXT NOT NULL)""")
        c.execute("""CREATE TABLE IF NOT EXISTS higgsfield_requests(
          id TEXT PRIMARY KEY, request_id TEXT UNIQUE, operation TEXT, model TEXT,
          status TEXT, prompt TEXT, result_url TEXT, error TEXT, created_at TEXT,
          updated_at TEXT)""")
init()

def run(cmd, timeout=1800):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=True)

def get_duration(path):
    p=run(["ffprobe","-v","error","-show_entries","format=duration","-of","default=noprint_wrappers=1:nokey=1",str(path)])
    return float(p.stdout.strip())

def transcribe(path):
    """Optional local transcription. Install faster-whisper to enable."""
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        return [], "faster-whisper not installed; transcript unavailable"
    model_name=os.getenv("WHISPER_MODEL","small")
    model=WhisperModel(model_name, device=os.getenv("WHISPER_DEVICE","cpu"),
                       compute_type=os.getenv("WHISPER_COMPUTE_TYPE","int8"))
    segments, info=model.transcribe(str(path), word_timestamps=True, vad_filter=True)
    out=[]
    for seg in segments:
        words=getattr(seg,"words",None) or []
        if words:
            for w in words:
                out.append({"start":float(w.start),"end":float(w.end),"text":w.word.strip()})
        else:
            out.append({"start":float(seg.start),"end":float(seg.end),"text":seg.text.strip()})
    return out, None

def score_windows(words, duration, clip_len):
    """Heuristic highlight ranking from transcript cues, speech density and exclamations."""
    cues={"wow":2,"what":1,"no way":3,"lets go":3,"let's go":3,"insane":3,"crazy":2,
          "oh my":2,"bro":0.5,"gg":1,"clutch":3,"win":1.5,"won":1.5,"easy":1,
          "holy":2,"wait":1,"actually":0.5,"help":0.5,"damn":1.5}
    if not words: return []
    step=max(5, clip_len//3)
    candidates=[]
    for start in range(0,max(1,int(duration-clip_len)+1),step):
        end=min(duration,start+clip_len)
        chunk=[w for w in words if start <= w["start"] < end]
        if not chunk: continue
        text=" ".join(w["text"] for w in chunk).lower()
        density=len(chunk)/max(clip_len,1)
        cue=sum(weight for phrase,weight in cues.items() if phrase in text)
        exclamations=text.count("!")*1.5
        score=density + cue + exclamations
        # prefer clips with a spoken opening and ending, not empty tails
        if chunk[0]["start"]-start > 5: score-=1
        if end-chunk[-1]["end"] > 5: score-=1
        candidates.append({"start":float(start),"score":score,"transcript":text})
    candidates.sort(key=lambda x:x["score"],reverse=True)
    selected=[]
    for cand in candidates:
        if all(abs(cand["start"]-s["start"]) >= clip_len*0.75 for s in selected):
            selected.append(cand)
        if len(selected)>=12: break
    return selected

def ass_time(sec):
    cs=int(round(sec*100)); h=cs//360000; m=(cs%360000)//6000; s=(cs%6000)//100
    return f"{h}:{m:02d}:{s:02d}.{cs%100:02d}"

def write_ass(words, clip_start, clip_duration, target):
    header="""[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
ScaledBorderAndShadow: yes
[V4+ Styles]
Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding
Style: Default,Arial,72,&H00FFFFFF,&H0000FFFF,&H00100A18,&H80000000,1,0,0,0,100,100,0,0,1,5,2,2,70,70,250,1
[Events]
Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text
"""
    events=[]
    # Display short word groups with karaoke-style emphasis. Group into 3-5 word bursts.
    local=[w for w in words if clip_start <= w["start"] < clip_start+clip_duration]
    for i in range(0,len(local),4):
        group=local[i:i+4]
        if not group: continue
        st=max(0,group[0]["start"]-clip_start)
        en=min(clip_duration,group[-1]["end"]-clip_start+0.15)
        parts=[]
        for w in group:
            dur=max(1,int((w["end"]-w["start"])*100))
            clean=re.sub(r"([{}])",r"\\\1",w["text"].upper())
            parts.append(f"{{\\k{dur}}}{clean}")
        events.append(f"Dialogue: 0,{ass_time(st)},{ass_time(max(st+0.2,en))},Default,,0,0,0,,{{\\an2}}"+r"\N".join([" ".join(parts)]))
    Path(target).write_text(header+"\n".join(events)+"\n",encoding="utf-8")

def render_clip(source, job_id, index, start, length, words):
    out=OUTPUTS/f"{job_id}_{index:02d}.mp4"
    ass=TRANSCRIPTS/f"{job_id}_{index:02d}.ass"
    write_ass(words,start,length,ass)
    # ASS subtitle path escaped for ffmpeg filter parser.
    ass_path=str(ass).replace("\\","/").replace(":","\\:")
    vf=f"scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,subtitles='{ass_path}'"
    run(["ffmpeg","-y","-ss",str(start),"-i",str(source),"-t",str(length),"-vf",vf,
         "-c:v","libx264","-preset","veryfast","-crf","22","-c:a","aac","-movflags","+faststart",str(out)])
    return out

def process_job(job_id, source, requested_count, clip_length):
    with db() as c: c.execute("UPDATE jobs SET status='transcribing',message='Transcribing video…' WHERE id=?",(job_id,))
    try:
        duration=get_duration(source)
        words, warning=transcribe(source)
        candidates=score_windows(words,duration,clip_length) if words else []
        if candidates:
            chosen=candidates[:requested_count]
        else:
            count=min(requested_count,max(1,int(duration//clip_length)))
            starts=[max(0,(duration-clip_length)*i/max(count-1,1)) for i in range(count)]
            chosen=[{"start":s,"score":0,"transcript":""} for s in starts]
        with db() as c: c.execute("UPDATE jobs SET status='rendering',message='Rendering vertical clips and captions…' WHERE id=?",(job_id,))
        made=[]
        for i,item in enumerate(chosen,1):
            out=render_clip(source,job_id,i,item["start"],clip_length,words)
            clip_id=str(uuid.uuid4())
            title=f"aguagamer2247 highlight {i}"
            with db() as c:
                c.execute("INSERT INTO clips(id,job_id,filename,start,duration,title,transcript,created_at) VALUES(?,?,?,?,?,?,?,?)",
                  (clip_id,job_id,out.name,item["start"],clip_length,title,item.get("transcript",""),datetime.now(timezone.utc).isoformat()))
            made.append(out.name)
        msg=f"Created {len(made)} clip(s)."
        if warning: msg += " " + warning
        with db() as c: c.execute("UPDATE jobs SET status='completed',message=?,clip_count=? WHERE id=?",(msg,len(made),job_id))
    except Exception as e:
        with db() as c: c.execute("UPDATE jobs SET status='failed',message=? WHERE id=?",(str(e),job_id))


# Higgsfield Developer API integration.
# Credentials are encrypted at rest and are never returned to the browser.
HIGGSFIELD_API = "https://api.higgsfield.ai"
HIGGSFIELD_MODELS = {
    "kling-video/v4.0/text-to-video": {
        "label": "Kling 4.0 — Text to Video",
        "operation": "generate",
        "description": "Generate a new video from a text prompt.",
    },
    "kling-video/omni/video-edit": {
        "label": "Kling Omni — Video Edit / Enhance",
        "operation": "enhance",
        "description": "Edit an existing video using a prompt and optional pro mode.",
    },
}

def _fernet():
    key = os.getenv("APP_ENCRYPTION_KEY", "").strip()
    if not key:
        raise HTTPException(503, "APP_ENCRYPTION_KEY is missing. Set it in Render > clipforge-ai-api > Environment, then redeploy.")
    try:
        return Fernet(key.encode())
    except Exception:
        raise HTTPException(503, "APP_ENCRYPTION_KEY must be a valid Fernet key. Correct it in Render Environment.")

def _higgsfield_credential():
    # Environment credentials take precedence for managed deployments.
    env_key = os.getenv("HIGGSFIELD_API_KEY", "").strip()
    if env_key:
        return env_key
    with db() as c:
        row = c.execute("SELECT secret FROM integrations WHERE name='higgsfield'").fetchone()
    if not row:
        return None
    try:
        return _fernet().decrypt(row["secret"]).decode()
    except (InvalidToken, ValueError):
        raise HTTPException(500, "Stored Higgsfield credential cannot be decrypted. Reconnect the API key.")

def _hf_headers(credential):
    return {"Authorization": f"Key {credential}", "Content-Type": "application/json"}

async def _hf_error(response):
    try:
        payload = response.json()
        detail = payload.get("detail", payload.get("error", payload))
    except Exception:
        detail = response.text[:500]
    # Never include authorization data in surfaced errors.
    return HTTPException(response.status_code if response.status_code < 500 else 502,
                         f"Higgsfield API error ({response.status_code}): {detail}")

@app.get("/api/higgsfield/connection")
def higgsfield_connection():
    configured = bool(_higgsfield_credential())
    return {"configured": configured, "provider": "Higgsfield Developer API",
            "credential_source": "environment" if os.getenv("HIGGSFIELD_API_KEY") else ("encrypted_app_storage" if configured else None)}

@app.post("/api/higgsfield/connect")
async def higgsfield_connect(payload: dict):
    credential = str(payload.get("api_key", "")).strip()
    if not credential or len(credential) > 512 or "\n" in credential:
        raise HTTPException(400, "Paste the complete Higgsfield API credential.")
    cipher = _fernet()
    # Validate credentials with a harmless status request before storing them.
    # The API's status endpoint requires a request ID, so validate by requesting
    # an intentionally nonexistent ID; auth failures are distinguishable from 404.
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.get(f"{HIGGSFIELD_API}/requests/00000000-0000-0000-0000-000000000000/status",
                                        headers=_hf_headers(credential))
    except httpx.TimeoutException:
        raise HTTPException(504, "Higgsfield did not respond within 20 seconds. Try again later.")
    except httpx.RequestError:
        raise HTTPException(502, "Could not reach the Higgsfield API. Check your Render outbound connection.")
    if response.status_code in (401, 403):
        raise HTTPException(401, "Higgsfield rejected this credential. Check the API key and secret copied from the console.")
    if response.status_code not in (200, 404):
        raise HTTPException(502, f"Higgsfield credential verification returned HTTP {response.status_code}. Check the API account and developer access.")
    encrypted = cipher.encrypt(credential.encode())
    with db() as c:
        c.execute("INSERT INTO integrations(name,secret,updated_at) VALUES('higgsfield',?,?) "
                  "ON CONFLICT(name) DO UPDATE SET secret=excluded.secret,updated_at=excluded.updated_at",
                  (encrypted, datetime.now(timezone.utc).isoformat()))
    return {"configured": True, "message": "Higgsfield API credential saved securely."}

@app.delete("/api/higgsfield/connect")
def higgsfield_disconnect():
    with db() as c:
        c.execute("DELETE FROM integrations WHERE name='higgsfield'")
    return {"configured": False, "message": "Stored Higgsfield credential removed."}

@app.get("/api/higgsfield/models")
def higgsfield_models():
    return {"models": [{"id": k, **v} for k, v in HIGGSFIELD_MODELS.items()]}

@app.post("/api/higgsfield/generate")
async def higgsfield_generate(payload: dict):
    credential = _higgsfield_credential()
    if not credential:
        raise HTTPException(400, "Connect your Higgsfield API key first.")
    model = str(payload.get("model", "kling-video/v4.0/text-to-video"))
    if model not in HIGGSFIELD_MODELS or HIGGSFIELD_MODELS[model]["operation"] != "generate":
        raise HTTPException(400, "Choose a supported text-to-video model.")
    prompt = str(payload.get("prompt", "")).strip()
    if not prompt or len(prompt) > 5000:
        raise HTTPException(400, "Enter a prompt between 1 and 5000 characters.")
    duration = int(payload.get("duration", 10))
    resolution = str(payload.get("resolution", "1080p"))
    aspect_ratio = str(payload.get("aspect_ratio", "9:16"))
    if duration < 3 or duration > 30: raise HTTPException(400, "Duration must be between 3 and 30 seconds.")
    if resolution not in ("720p", "1080p"): raise HTTPException(400, "Resolution must be 720p or 1080p.")
    if aspect_ratio not in ("21:9", "16:9", "9:16", "1:1"): raise HTTPException(400, "Unsupported aspect ratio.")
    body = {"prompt": prompt, "duration": duration, "resolution": resolution, "aspect_ratio": aspect_ratio}
    local_id = str(uuid.uuid4())
    idem = str(uuid.uuid4())
    async with httpx.AsyncClient(timeout=45) as client:
        try:
            response = await client.post(f"{HIGGSFIELD_API}/{model}", headers={
                **_hf_headers(credential), "Idempotency-Key": idem}, json=body)
        except httpx.TimeoutException:
            raise HTTPException(504, "Higgsfield submission timed out. Check your request history before submitting again.")
    if response.status_code >= 400: raise await _hf_error(response)
    data = response.json()
    request_id = data.get("request_id")
    if not request_id: raise HTTPException(502, "Higgsfield did not return a request_id.")
    now = datetime.now(timezone.utc).isoformat()
    with db() as c:
        c.execute("INSERT INTO higgsfield_requests VALUES(?,?,?,?,?,?,?,?,?,?)",
                  (local_id, request_id, "generate", model, data.get("status","queued"), prompt,
                   None, None, now, now))
    return {"id": local_id, "request_id": request_id, "status": data.get("status","queued"),
            "message": "Generation submitted to Higgsfield."}

@app.post("/api/higgsfield/enhance")
async def higgsfield_enhance(file: UploadFile = File(...), prompt: str = Form(...),
                             mode: str = Form("standard")):
    credential = _higgsfield_credential()
    if not credential: raise HTTPException(400, "Connect your Higgsfield API key first.")
    if mode not in ("standard", "pro"): raise HTTPException(400, "Mode must be standard or pro.")
    prompt = prompt.strip()
    if not prompt or len(prompt) > 5000: raise HTTPException(400, "Enter an enhancement prompt.")
    ext = Path(file.filename or "source.mp4").suffix.lower()
    if ext not in (".mp4", ".mov", ".webm", ".mkv", ".m4v"):
        raise HTTPException(400, "Upload an MP4, MOV, WEBM, MKV or M4V video.")
    file.file.seek(0, 2)
    size = file.file.tell()
    file.file.seek(0)
    if not size or size > 2 * 1024**3:
        raise HTTPException(413, "Video must be non-empty and no larger than 2 GB.")
    content_type = file.content_type or mimetypes.guess_type(file.filename or "")[0] or "video/mp4"
    async def upload_stream():
        while chunk := await file.read(1024 * 1024):
            yield chunk
    async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
        # Obtain a signed storage URL from Higgsfield.
        signed = await client.post(f"{HIGGSFIELD_API}/files/generate-upload-url",
                                   headers=_hf_headers(credential), json={"content_type": content_type})
        if signed.status_code >= 400: raise await _hf_error(signed)
        up = signed.json()
        upload_url, public_url = up.get("upload_url"), up.get("public_url")
        upload_headers = up.get("upload_headers") or {}
        if not upload_url or not public_url: raise HTTPException(502, "Higgsfield did not return upload_url/public_url.")
        try:
            put_headers = dict(upload_headers)
            put_headers.setdefault("Content-Length", str(size))
            put = await client.put(upload_url, headers=put_headers, content=upload_stream())
        except httpx.HTTPError:
            raise HTTPException(502, "Could not upload the source video to Higgsfield.")
        if put.status_code >= 400: raise HTTPException(502, f"Higgsfield media upload failed (HTTP {put.status_code}).")
        body = {"mode": mode, "prompt": prompt, "video_urls": [public_url]}
        idem = str(uuid.uuid4())
        try:
            response = await client.post(f"{HIGGSFIELD_API}/kling-video/omni/video-edit",
                                         headers={**_hf_headers(credential), "Idempotency-Key": idem}, json=body)
        except httpx.TimeoutException:
            raise HTTPException(504, "Enhancement submission timed out. Check Higgsfield request history before retrying.")
    if response.status_code >= 400: raise await _hf_error(response)
    data = response.json()
    request_id = data.get("request_id")
    if not request_id: raise HTTPException(502, "Higgsfield did not return a request_id.")
    local_id = str(uuid.uuid4()); now = datetime.now(timezone.utc).isoformat()
    with db() as c:
        c.execute("INSERT INTO higgsfield_requests VALUES(?,?,?,?,?,?,?,?,?,?)",
                  (local_id, request_id, "enhance", "kling-video/omni/video-edit",
                   data.get("status","queued"), prompt, None, None, now, now))
    return {"id": local_id, "request_id": request_id, "status": data.get("status","queued"),
            "message": "Video uploaded and enhancement submitted to Higgsfield."}

@app.get("/api/higgsfield/requests")
def higgsfield_request_history():
    with db() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT id,request_id,operation,model,status,prompt,result_url,error,created_at,updated_at "
            "FROM higgsfield_requests ORDER BY created_at DESC LIMIT 50")]
    return {"requests": rows}

@app.get("/api/higgsfield/requests/{local_id}")
async def higgsfield_request_status(local_id: str):
    credential = _higgsfield_credential()
    if not credential: raise HTTPException(400, "Connect your Higgsfield API key first.")
    with db() as c:
        row = c.execute("SELECT * FROM higgsfield_requests WHERE id=?", (local_id,)).fetchone()
    if not row: raise HTTPException(404, "Higgsfield request not found.")
    async with httpx.AsyncClient(timeout=25) as client:
        response = await client.get(f"{HIGGSFIELD_API}/requests/{row['request_id']}/status",
                                    headers=_hf_headers(credential))
    if response.status_code >= 400: raise await _hf_error(response)
    data = response.json()
    status = data.get("status", row["status"])
    video = data.get("video") or {}
    result_url = video.get("url") if isinstance(video, dict) else None
    if not result_url and isinstance(data.get("videos"), list) and data["videos"]:
        result_url = data["videos"][0].get("url")
    error = data.get("error")
    with db() as c:
        c.execute("UPDATE higgsfield_requests SET status=?,result_url=COALESCE(?,result_url),error=?,updated_at=? WHERE id=?",
                  (status, result_url, error, datetime.now(timezone.utc).isoformat(), local_id))
    return {"id": local_id, "request_id": row["request_id"], "status": status,
            "result_url": result_url, "error": error, "raw": data}

@app.post("/api/higgsfield/requests/{local_id}/cancel")
async def higgsfield_cancel(local_id: str):
    credential = _higgsfield_credential()
    if not credential: raise HTTPException(400, "Connect your Higgsfield API key first.")
    with db() as c:
        row = c.execute("SELECT * FROM higgsfield_requests WHERE id=?", (local_id,)).fetchone()
    if not row: raise HTTPException(404, "Higgsfield request not found.")
    async with httpx.AsyncClient(timeout=25) as client:
        response = await client.post(f"{HIGGSFIELD_API}/requests/{row['request_id']}/cancel",
                                     headers=_hf_headers(credential))
    if response.status_code >= 400: raise await _hf_error(response)
    data = response.json() if response.content else {}
    status = data.get("status", "canceled")
    with db() as c:
        c.execute("UPDATE higgsfield_requests SET status=?,updated_at=? WHERE id=?",
                  (status, datetime.now(timezone.utc).isoformat(), local_id))
    return {"id": local_id, "request_id": row["request_id"], "status": status}


# YouTube OAuth: tokens and temporary OAuth state are kept server-side.
YOUTUBE_SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
]
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"

def _youtube_configured():
    return bool(os.getenv("GOOGLE_CLIENT_ID", "").strip() and
                os.getenv("GOOGLE_CLIENT_SECRET", "").strip() and
                os.getenv("GOOGLE_REDIRECT_URI", "").strip())

def _youtube_credentials():
    if not _youtube_configured():
        raise HTTPException(503, "Set GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET and GOOGLE_REDIRECT_URI in the backend environment.")
    with db() as c:
        row = c.execute("SELECT secret FROM integrations WHERE name='youtube_oauth'").fetchone()
    if not row:
        raise HTTPException(400, "YouTube is not connected. Start Google authorization first.")
    try:
        from google.oauth2.credentials import Credentials
        payload = json.loads(_fernet().decrypt(row["secret"]).decode())
        creds = Credentials.from_authorized_user_info(payload, YOUTUBE_SCOPES)
        if creds.expired and creds.refresh_token:
            from google.auth.transport.requests import Request as GoogleRequest
            creds.refresh(GoogleRequest())
            encrypted = _fernet().encrypt(creds.to_json().encode())
            with db() as c:
                c.execute("UPDATE integrations SET secret=?,updated_at=? WHERE name='youtube_oauth'",
                          (encrypted, datetime.now(timezone.utc).isoformat()))
        if not creds.valid:
            raise HTTPException(401, "YouTube authorization expired. Reconnect your channel.")
        return creds
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(500, "Could not read saved YouTube authorization. Reconnect your channel.")

def _youtube_channel(creds):
    from googleapiclient.discovery import build
    service = build("youtube", "v3", credentials=creds, cache_discovery=False)
    result = service.channels().list(part="snippet", mine=True, maxResults=1).execute()
    items = result.get("items", [])
    if not items:
        raise HTTPException(403, "This Google account has no accessible YouTube channel.")
    item = items[0]
    return {"id": item.get("id"), "title": item.get("snippet", {}).get("title", "YouTube channel"),
            "custom_url": item.get("snippet", {}).get("customUrl")}

@app.get("/api/youtube/connection")
def youtube_connection():
    with db() as c:
        row = c.execute("SELECT secret FROM integrations WHERE name='youtube_oauth'").fetchone()
    if not row:
        return {"configured": _youtube_configured(), "connected": False, "channel": None}
    try:
        creds = _youtube_credentials()
        channel = _youtube_channel(creds)
        return {"configured": True, "connected": True, "channel": channel}
    except HTTPException as exc:
        return {"configured": _youtube_configured(), "connected": False, "channel": None, "message": exc.detail}

@app.post("/api/youtube/connect")
def youtube_connect():
    if not _youtube_configured():
        raise HTTPException(503, "Google OAuth is not configured. Add GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET and GOOGLE_REDIRECT_URI to Render.")
    state = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    with db() as c:
        c.execute("DELETE FROM oauth_states WHERE created_at < ?", ((now-timedelta(minutes=15)).isoformat(),))
        c.execute("INSERT INTO oauth_states(state,created_at) VALUES(?,?)", (state, now.isoformat()))
    params = {
        "client_id": os.environ["GOOGLE_CLIENT_ID"].strip(),
        "redirect_uri": os.environ["GOOGLE_REDIRECT_URI"].strip(),
        "response_type": "code", "scope": " ".join(YOUTUBE_SCOPES), "access_type": "offline",
        "include_granted_scopes": "true", "prompt": "consent", "state": state,
    }
    return {"authorization_url": GOOGLE_AUTH_URL + "?" + urlencode(params)}

@app.get("/api/youtube/callback", response_class=HTMLResponse)
async def youtube_callback(request: Request):
    state = request.query_params.get("state", "")
    code = request.query_params.get("code", "")
    error = request.query_params.get("error", "")
    with db() as c:
        row = c.execute("SELECT created_at FROM oauth_states WHERE state=?", (state,)).fetchone()
        c.execute("DELETE FROM oauth_states WHERE state=?", (state,))
    if not row:
        return HTMLResponse("<h2>ClipForge AI: authorization expired</h2><p>Return to the dashboard and try connecting again.</p>", status_code=400)
    created = datetime.fromisoformat(row["created_at"])
    if datetime.now(timezone.utc)-created > timedelta(minutes=15):
        return HTMLResponse("<h2>ClipForge AI: authorization expired</h2><p>Return to the dashboard and try connecting again.</p>", status_code=400)
    if error:
        return HTMLResponse(f"<h2>YouTube connection was not completed</h2><p>{error}</p><p>Return to ClipForge AI and try again.</p>", status_code=400)
    if not code:
        return HTMLResponse("<h2>Missing Google authorization code</h2><p>Return to ClipForge AI and try again.</p>", status_code=400)
    if not _youtube_configured():
        return HTMLResponse("<h2>ClipForge AI is not configured</h2><p>Google OAuth environment variables are missing on the backend.</p>", status_code=503)
    stage = "token_exchange"
    try:
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.post(GOOGLE_TOKEN_URL, data={
                    "code": code, "client_id": os.environ["GOOGLE_CLIENT_ID"].strip(),
                    "client_secret": os.environ["GOOGLE_CLIENT_SECRET"].strip(),
                    "redirect_uri": os.environ["GOOGLE_REDIRECT_URI"].strip(),
                    "grant_type": "authorization_code",
                })
        except httpx.RequestError:
            return HTMLResponse("<h2>Google is temporarily unreachable</h2><p>Check backend connectivity and start a new authorization attempt.</p>", status_code=502)
        if response.status_code >= 400:
            # Display only Google's standardized error code, never its raw response.
            # Authorization codes, refresh tokens and provider descriptions remain private.
            try:
                provider_error = response.json().get("error", "")
            except (ValueError, AttributeError):
                provider_error = ""
            guidance = {
                "invalid_client": "Check that GOOGLE_CLIENT_SECRET matches the Web application OAuth Client ID in Google Cloud.",
                "invalid_grant": "The code expired, was already used, or the redirect URI differs. Start a NEW connection and verify the exact redirect URI.",
                "redirect_uri_mismatch": "Check the authorized redirect URI in Google Cloud and GOOGLE_REDIRECT_URI in Render.",
                "unauthorized_client": "Check that Google OAuth is configured as a Web application and that the project permits this access.",
            }
            hint = guidance.get(provider_error, "Check the client credentials and redirect URI in Render and Google Cloud.")
            label = provider_error if provider_error in guidance else "token_exchange_error"
            return HTMLResponse(f"<h2>Google token exchange failed: {label}</h2><p>{hint}</p>", status_code=400)
        token_data = response.json()
        if not token_data.get("access_token"):
            return HTMLResponse("<h2>Google did not return an access token</h2><p>Try connecting again.</p>", status_code=400)
        from google.oauth2.credentials import Credentials
        # google-auth compares expiry against naive UTC; aware datetimes raise TypeError.
        creds = Credentials(token=token_data.get("access_token"), refresh_token=token_data.get("refresh_token"),
                            token_uri=GOOGLE_TOKEN_URL, client_id=os.environ["GOOGLE_CLIENT_ID"].strip(),
                            client_secret=os.environ["GOOGLE_CLIENT_SECRET"].strip(), scopes=YOUTUBE_SCOPES,
                            expiry=datetime.now(timezone.utc).replace(tzinfo=None)+timedelta(seconds=int(token_data.get("expires_in",3600))))
        stage = "channel_lookup"
        channel = await run_in_threadpool(_youtube_channel, creds)
        stage = "token_encryption"
        encrypted = _fernet().encrypt(creds.to_json().encode())
        stage = "credential_storage"
        with db() as c:
            c.execute("INSERT INTO integrations(name,secret,updated_at) VALUES('youtube_oauth',?,?) "
                      "ON CONFLICT(name) DO UPDATE SET secret=excluded.secret,updated_at=excluded.updated_at",
                      (encrypted, datetime.now(timezone.utc).isoformat()))
        title = str(channel.get("title", "YouTube channel")).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        return HTMLResponse(f"<html><body style='background:#100b19;color:#f5edff;font:16px Arial;padding:40px'><h1 style='color:#bd7aff'>ClipForge AI connected!</h1><p>You connected the YouTube channel <b>{title}</b>.</p><p>You can close this tab and return to your ClipForge dashboard.</p><script>setTimeout(()=>{{try{{window.close()}}catch(e){{}}}},1800)</script></body></html>")
    except HTTPException as exc:
        # HTTPException messages in this flow are authored by the backend, not Google.
        return HTMLResponse(
            "<h2>YouTube connection not completed</h2><p>" + str(exc.detail) + "</p>",
            status_code=exc.status_code,
        )
    except HttpError as exc:
        status = int(exc.resp.status)
        if status == 403:
            reason = "Google denied the YouTube channel lookup. Enable YouTube Data API v3 in your Google Cloud project and check API access and quota."
        elif status == 401:
            reason = "Google rejected the YouTube API authorization. Reconnect and approve the requested YouTube permissions."
        else:
            reason = f"YouTube Data API returned HTTP {status}. Check the Google Cloud API settings and try again."
        logging.getLogger("clipforge.oauth").warning("YouTube API failure: HTTP %s at %s", status, stage)
        return HTMLResponse("<h2>YouTube API connection failed</h2><p>" + reason + "</p>", status_code=502)
    except Exception as exc:
        # Log only the exception type and stage. Never log codes, tokens, URLs, or provider response bodies.
        logging.getLogger("clipforge.oauth").warning("YouTube OAuth failure: %s at %s", type(exc).__name__, stage)
        return HTMLResponse(
            "<h2>Could not finish YouTube connection</h2><p>Backend failure during " + stage.replace("_", " ") +
            ". Review Render application logs for the sanitized error category. Then start a NEW Google authorization.</p>",
            status_code=500,
        )

@app.delete("/api/youtube/connect")
def youtube_disconnect():
    with db() as c:
        c.execute("DELETE FROM integrations WHERE name='youtube_oauth'")
    return {"connected": False}

@app.post("/api/youtube/upload/{clip_id}")
async def youtube_upload_clip(clip_id: str, payload: dict):
    with db() as c:
        clip = c.execute("SELECT * FROM clips WHERE id=?", (clip_id,)).fetchone()
    if not clip:
        raise HTTPException(404, "Clip not found.")
    source = OUTPUTS / clip["filename"]
    if not source.is_file():
        raise HTTPException(404, "Rendered clip file is missing from backend storage.")
    title = str(payload.get("title") or clip["title"] or "ClipForge Short").strip()[:100]
    description = str(payload.get("description") or "Created with ClipForge AI").strip()[:5000]
    privacy = str(payload.get("privacy_status", "private"))
    if privacy not in ("private", "unlisted", "public"):
        raise HTTPException(400, "privacy_status must be private, unlisted or public.")
    creds = _youtube_credentials()
    def upload():
        from googleapiclient.discovery import build
        from googleapiclient.http import MediaFileUpload
        service = build("youtube", "v3", credentials=creds, cache_discovery=False)
        request_obj = service.videos().insert(part="snippet,status", body={
            "snippet": {"title": title, "description": description, "categoryId": "20", "tags": ["Shorts", "gaming", "aguagamer2247"]},
            "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False},
        }, media_body=MediaFileUpload(str(source), mimetype="video/mp4", chunksize=8*1024*1024, resumable=True))
        response = None
        while response is None:
            _, response = request_obj.next_chunk()
        return response
    try:
        result = await run_in_threadpool(upload)
    except Exception as exc:
        raise HTTPException(502, "YouTube upload failed. Check channel permissions, API quota and video policy settings.") from exc
    with db() as c:
        c.execute("UPDATE clips SET published=1 WHERE id=?", (clip_id,))
    return {"uploaded": True, "video_id": result.get("id"),
            "url": f"https://www.youtube.com/watch?v={result.get('id')}", "privacy_status": privacy}


@app.get("/api/config-status")
def config_status():
    """Safe troubleshooting signals; protected by the private instance token middleware."""
    fernet_valid = False
    try:
        fernet_valid = bool(_fernet())
    except HTTPException:
        pass
    client_id = os.getenv("GOOGLE_CLIENT_ID", "").strip()
    redirect = os.getenv("GOOGLE_REDIRECT_URI", "").strip()
    return {
        "encryption_ready": fernet_valid,
        "google_oauth_ready": _youtube_configured(),
        "google_client_id_format_ok": client_id.endswith(".apps.googleusercontent.com"),
        "redirect_uri_matches_render": redirect == "https://clipforge-ai-api.onrender.com/api/youtube/callback",
        "higgsfield_environment_key_present": bool(os.getenv("HIGGSFIELD_API_KEY", "").strip()),
    }

@app.get("/api/health")
def health():
    return {"ok":True,"app":"ClipForge AI","channel":"aguagamer2247","ffmpeg":bool(shutil.which("ffmpeg")),"ffprobe":bool(shutil.which("ffprobe")),"youtube_oauth_configured":_youtube_configured()}

@app.get("/api/dashboard")
def dashboard():
    with db() as c:
        jobs=[dict(r) for r in c.execute("SELECT * FROM jobs ORDER BY created_at DESC LIMIT 30")]
        clips=[dict(r) for r in c.execute("SELECT * FROM clips ORDER BY created_at DESC LIMIT 50")]
        schedules=[dict(r) for r in c.execute("SELECT * FROM schedules ORDER BY publish_at")]
    return {"clips_created":len(clips),"jobs":jobs,"clips":clips,"schedules":schedules}

@app.post("/api/jobs")
async def create_job(file:UploadFile=File(...),clip_count:int=Form(5),clip_length:int=Form(30),background_tasks:BackgroundTasks=None):
    if clip_count<1 or clip_count>12: raise HTTPException(400,"Choose between 1 and 12 clips.")
    if clip_length not in (20,30,45,60): raise HTTPException(400,"Unsupported clip length.")
    ext=Path(file.filename or "video.mp4").suffix.lower()
    if ext not in (".mp4",".mov",".mkv",".webm",".m4v"): raise HTTPException(400,"Unsupported video format.")
    jid=str(uuid.uuid4()); source=UPLOADS/f"{jid}{ext}"; size=0
    with source.open("wb") as f:
        while chunk:=await file.read(1024*1024):
            size+=len(chunk)
            if size>2*1024**3:
                source.unlink(missing_ok=True); raise HTTPException(413,"Maximum upload size is 2 GB.")
            f.write(chunk)
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        source.unlink(missing_ok=True); raise HTTPException(503,"Install FFmpeg and FFprobe and add them to PATH.")
    created=datetime.now(timezone.utc).isoformat(timespec="seconds")
    with db() as c:
        c.execute("INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?)",(jid,file.filename,str(source),clip_count,clip_length,"queued","Queued",created))
    background_tasks.add_task(process_job,jid,source,clip_count,clip_length)
    return {"job_id":jid,"status":"queued","message":"Upload received. Transcription and clip rendering are running in the background."}

@app.get("/api/clips")
def clips():
    with db() as c: rows=[dict(r) for r in c.execute("SELECT * FROM clips ORDER BY created_at DESC")]
    return {"clips":[{**r,"url":f"/clips/{r['filename']}"} for r in rows]}

@app.post("/api/schedules")
def schedule_clip(clip_id:str,publish_at:str):
    try: dt=datetime.fromisoformat(publish_at.replace("Z","+00:00"))
    except ValueError: raise HTTPException(400,"publish_at must be ISO-8601.")
    if dt.tzinfo is None: raise HTTPException(400,"Include a timezone offset.")
    with db() as c:
        if not c.execute("SELECT id FROM clips WHERE id=?",(clip_id,)).fetchone(): raise HTTPException(404,"Clip not found.")
        sid=str(uuid.uuid4())
        c.execute("INSERT INTO schedules VALUES(?,?,?,?,?)",(sid,clip_id,dt.isoformat(),"pending","Awaiting YouTube connection"))
    return {"schedule_id":sid,"status":"pending","message":"Schedule saved. It will not publish until YouTube OAuth is configured."}

@app.get("/api/integrations")
def integrations():
    return {
      "higgsfield":{"configured":bool(_higgsfield_credential()),"note":"Official Higgsfield Developer API; server-side credential storage."},
      "youtube":{"configured":_youtube_configured(),"note":"Google OAuth authorization and YouTube video upload are implemented. Add the OAuth environment variables and connect your channel."},
      "transcription":{"configured":importlib.util.find_spec("faster_whisper") is not None}
    }

