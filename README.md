# ClipForge AI — aguagamer2247

Black-and-purple creator workspace with local video clipping and a server-side Higgsfield Developer API integration.

## Included

- FastAPI backend for local video uploads, transcription and vertical Shorts rendering.
- Chrome extension dashboard with the aguagamer2247 branding.
- Higgsfield API connection form with server-side credential storage encrypted at rest.
- Text-to-video generation through the documented Kling 4.0 text-to-video endpoint.
- Existing-video enhancement through the documented Kling Omni video-edit endpoint.
- Signed source-media upload flow, request history, status polling and cancellation.
- Higgsfield output links are displayed when the provider returns a completed video URL.

## Requirements

- Python 3.10+
- FFmpeg and FFprobe installed and available on PATH
- A Higgsfield Developer API credential with access/balance for the selected models
- Chrome/Chromium for the extension

## Quick start

- Linux / ChromeOS Linux: run `./start_clipforge_linux.sh` from the extracted project folder. On ChromeOS Linux, install FFmpeg first with `sudo apt update && sudo apt install -y ffmpeg`.
- Windows: double-click `start_clipforge_windows.bat`.

The launcher creates a Python virtual environment, installs dependencies, creates `backend/.env`, generates an encryption key if needed, and starts the local backend. Keep the terminal open while using the extension.

## Manual backend setup

```bash
cd backend
python -m venv .venv
# Windows: .venv\\Scripts\\activate
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Generate an encryption key:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Put the generated value in `APP_ENCRYPTION_KEY` in `backend/.env`. Keep this key private and back it up: the website-stored Higgsfield credential cannot be decrypted without it.

Run the backend:

```bash
uvicorn main:app --host 127.0.0.1 --port 8787
```

The API is available at `http://127.0.0.1:8787`; interactive API docs are at `/docs`.

## Load the extension

1. Open `chrome://extensions`.
2. Enable Developer mode.
3. Select **Load unpacked**.
4. Choose the `extension` directory.
5. Keep the local backend running.

Open the extension dashboard and navigate to **Higgsfield AI Studio**.

## Connect Higgsfield

1. Create/manage credentials in the official [Higgsfield Console](https://console.higgsfield.ai).
2. Paste the complete copied credential into the password field in the dashboard.
3. Select **Connect & verify key**.
4. The backend validates the credential and encrypts it in SQLite. The key is not returned to the browser.

For managed hosting, set `HIGGSFIELD_API_KEY` in the server's secret manager instead. An environment-provided key takes precedence over the website-stored key.

## Higgsfield workflows

### Generate a video

- Choose Kling 4.0 — Text to Video.
- Enter a prompt.
- Choose duration (3–30 seconds), resolution (720p/1080p), and aspect ratio (including 9:16).
- Submit. The backend records the request ID and polls the official status endpoint when you open/refresh the request.

### Enhance an existing video

- Upload a source MP4/MOV/WEBM/MKV/M4V.
- Enter the desired edit instructions and choose Standard or Pro mode.
- The backend requests a signed upload URL from Higgsfield, uploads the video, then submits it to Kling Omni Video Edit.
- The backend stores the request ID and polls for the final output.

### Request lifecycle

- Generation POSTs use an `Idempotency-Key`.
- Status is checked through the provider request-status endpoint.
- Cancellation is sent to the provider's cancel endpoint; stopping browser polling alone does not cancel a job.
- Provider failures and errors are shown in the dashboard.
- Never automatically retry a generation after an ambiguous timeout. Check the provider request history first.

## Connect YouTube (Google OAuth)

1. Deploy the backend to Render using the included `render.yaml` Blueprint or Dockerfile. The service includes FFmpeg and a persistent disk mounted at `/var/data`.
2. Copy the public service URL from Render, for example `https://clipforge-ai-api.onrender.com`.
3. In Google Cloud Console > Google Auth Platform > Clients, create or edit a **Web application** OAuth client.
4. Add this exact Authorized redirect URI (replace with your actual Render hostname):
   `https://YOUR-RENDER-SERVICE.onrender.com/api/youtube/callback`
5. In Render > your service > Environment, set:
   - `APP_ENCRYPTION_KEY` = generate with `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`
   - `CLIPFORGE_ACCESS_TOKEN` = generate a long random secret, e.g. `openssl rand -hex 32`
   - `GOOGLE_CLIENT_ID` = OAuth client ID
   - `GOOGLE_CLIENT_SECRET` = OAuth client secret
   - `GOOGLE_REDIRECT_URI` = the exact callback URL above
6. In Google Cloud, enable **YouTube Data API v3**. If the OAuth app is in Testing mode, add the Google account that owns/manages the channel under Test users.
7. Redeploy the Render service after setting variables.
8. Reload the unpacked Chrome extension. In ClipForge Settings, paste the Render service URL and the `CLIPFORGE_ACCESS_TOKEN`, then save both.
9. Click **Connect YouTube channel**, select the correct Google account, review the requested YouTube permissions, and approve.
10. Return to the dashboard. The channel name should show as Connected. Finished clips appear under Ready-to-publish clips. Click **Upload to YouTube**; uploads default to Private.

The callback URL must match character-for-character in both Google Cloud and Render, including `https://`, hostname, path, and no extra trailing slash. Never put the Client Secret in the extension or share it in chat. The OAuth flow requests `youtube.upload` and `youtube.readonly`; tokens are encrypted with `APP_ENCRYPTION_KEY` and stored server-side.

## Render deployment

- The repository root includes `Dockerfile` and `render.yaml`.
- Connect the repository in Render and create a Blueprint from `render.yaml`.
- The Docker image installs FFmpeg and Python requirements. A persistent disk stores uploads, generated clips, SQLite data, and encrypted integration credentials.
- The manually generated `APP_ENCRYPTION_KEY` must remain stable. If you replace it, previously encrypted credentials cannot be decrypted and must be reconnected.
- Render's persistent disk requires a paid service plan. Confirm the plan and storage cost before creating it.

## What still needs your account access

I cannot sign into your Google or Render accounts, accept Google consent, or authorize your channel on your behalf. You must create the OAuth client and enter its values directly into Render's Environment settings. Do not send the Client Secret in chat.

## Important deployment/security notes

This is a private single-owner MVP, not a multi-user service. Remote requests require the `CLIPFORGE_ACCESS_TOKEN`; keep it private and do not share it. CORS remains permissive, so the token is the access control. Before selling or sharing the service, add real user accounts, strict origin controls, per-user data isolation, request quotas, and file/rate limits. The Render Blueprint is intended for a private single-owner test deployment.

The API credential must never be put in Chrome extension files, browser local storage, source control, logs, or screenshots. Rotate it if exposed.

## API documentation used

- [Higgsfield shared documentation index](https://docs.higgsfield.ai/docs/llms.txt)
- [Authentication](https://docs.higgsfield.ai/docs/authentication.md)
- [Kling 4.0 Text-to-Video API reference](https://open.higgsfield.ai/models/kling-video/v4.0/text-to-video/api-reference)
- [Kling Omni Video Edit API reference](https://open.higgsfield.ai/models/kling-video/omni/video-edit/api-reference)
- [Higgsfield API quickstart and signed upload flow](https://open.higgsfield.ai/quick-start)

## Not included yet

- Automated scheduled publishing is not implemented yet; YouTube OAuth and manual clip upload are implemented.
- This integration implements two documented model workflows, not every model in the Higgsfield catalog.
- Live generation has not been tested because no user's Higgsfield API credentials were supplied.
