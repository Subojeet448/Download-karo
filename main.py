#!/usr/bin/env python3
"""
Universal Media Downloader — FastAPI Edition
Developer: MANDAL !!
Version: 1.0 — Render Ready
"""

import os
import re
import shutil
import logging
import asyncio
import tempfile
from pathlib import Path
from typing import Optional

import yt_dlp
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import StreamingResponse, HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s — %(levelname)s — %(message)s")
logger = logging.getLogger(__name__)

# ── App ───────────────────────────────────────────────────────────────────────
app = FastAPI(title="Universal Media Downloader", version="1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Helpers ───────────────────────────────────────────────────────────────────
def clean_filename(name: str) -> str:
    name = re.sub(r"@\w+", "", name)
    name = re.sub(r"https?://\S+", "", name)
    name = re.sub(r"[^\w\s\-]", " ", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name[:80] if name else "video"


COMMON_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}


# ── /info — fetch title, thumbnail, formats ───────────────────────────────────
@app.get("/info")
async def get_info(url: str = Query(..., description="Video URL")):
    """Return video metadata + available quality options."""
    loop = asyncio.get_event_loop()
    try:
        result = await loop.run_in_executor(None, _fetch_formats, url)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error", "Could not fetch video info"))

    return JSONResponse(result)


def _fetch_formats(url: str) -> dict:
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": 30,
        "http_headers": COMMON_HEADERS,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)

        title     = info.get("title", "video")
        duration  = info.get("duration", 0)
        thumbnail = info.get("thumbnail")
        uploader  = info.get("uploader", "")
        raw_fmts  = info.get("formats", [])

        seen_heights = set()
        quality_list = []

        for f in reversed(raw_fmts):
            h = f.get("height")
            if not h:
                continue
            vcodec = f.get("vcodec", "none")
            if vcodec == "none":
                continue
            has_audio = f.get("acodec", "none") != "none"
            ext  = f.get("ext", "mp4")
            fs   = f.get("filesize") or f.get("filesize_approx") or 0

            if h not in seen_heights:
                seen_heights.add(h)
                quality_list.append({
                    "height":    h,
                    "ext":       ext,
                    "filesize":  fs,
                    "has_audio": has_audio,
                    "label":     f"{h}p",
                    "quality":   str(h),
                })

        quality_list.sort(key=lambda x: x["height"], reverse=True)

        if not quality_list:
            for h in [1080, 720, 480, 360, 240]:
                quality_list.append({
                    "height": h, "ext": "mp4", "filesize": 0,
                    "has_audio": True, "label": f"{h}p", "quality": str(h),
                })

        # Always add MP3 audio option
        quality_list.append({
            "height": 0, "ext": "mp3", "filesize": 0,
            "has_audio": True, "label": "MP3 🎵", "quality": "audio",
        })

        return {
            "ok": True,
            "title": title,
            "uploader": uploader,
            "duration": duration,
            "thumbnail": thumbnail,
            "formats": quality_list,
        }

    except Exception as e:
        logger.error(f"_fetch_formats error: {e}")
        return {"ok": False, "error": str(e)}


# ── /download — stream file to browser ───────────────────────────────────────
@app.get("/download")
async def download_video(
    url: str     = Query(..., description="Video URL"),
    quality: str = Query("720", description="Quality: 144/240/360/480/720/1080 or 'audio'"),
):
    """Download video/audio and stream it directly to the browser."""
    loop = asyncio.get_event_loop()
    tmp_dir = tempfile.mkdtemp(prefix="dlr_")
    try:
        filepath, err, title, _ = await loop.run_in_executor(
            None, _blocking_download, url, tmp_dir, quality
        )
    except Exception as e:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise HTTPException(status_code=500, detail=str(e))

    if err or not filepath:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise HTTPException(status_code=400, detail=f"Download failed: {err}")

    path_obj  = Path(filepath)
    file_size = path_obj.stat().st_size
    ext       = path_obj.suffix.lstrip(".")
    safe_name = clean_filename(title or "download")
    dl_name   = f"{safe_name}.{ext}"

    media_type = "audio/mpeg" if ext == "mp3" else "video/mp4"

    def iter_file():
        try:
            with open(filepath, "rb") as f:
                while chunk := f.read(1024 * 1024):   # 1 MB chunks
                    yield chunk
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    headers = {
        "Content-Disposition": f'attachment; filename="{dl_name}"',
        "Content-Length":      str(file_size),
        "Accept-Ranges":       "bytes",
    }
    return StreamingResponse(iter_file(), media_type=media_type, headers=headers)


# ── /stream — serve file inline for browser playback ─────────────────────────
@app.get("/stream")
async def stream_video(
    url: str     = Query(..., description="Video URL"),
    quality: str = Query("720", description="Quality"),
):
    """Stream video/audio inline for in-browser playback (no forced download)."""
    loop = asyncio.get_event_loop()
    tmp_dir = tempfile.mkdtemp(prefix="strm_")
    try:
        filepath, err, title, _ = await loop.run_in_executor(
            None, _blocking_download, url, tmp_dir, quality
        )
    except Exception as e:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise HTTPException(status_code=500, detail=str(e))

    if err or not filepath:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise HTTPException(status_code=400, detail=f"Stream failed: {err}")

    path_obj  = Path(filepath)
    file_size = path_obj.stat().st_size
    ext       = path_obj.suffix.lstrip(".")

    media_type = "audio/mpeg" if ext == "mp3" else "video/mp4"

    def iter_file():
        try:
            with open(filepath, "rb") as f:
                while chunk := f.read(1024 * 1024):
                    yield chunk
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    headers = {
        "Content-Length": str(file_size),
        "Accept-Ranges":  "bytes",
        "Cache-Control":  "no-store",
    }
    return StreamingResponse(iter_file(), media_type=media_type, headers=headers)


def _blocking_download(url: str, download_dir: str, quality: str):
    """Blocking yt-dlp download — runs in thread pool."""
    extract_audio = (quality == "audio")

    ydl_opts = {
        "quiet":               True,
        "no_warnings":         True,
        "noplaylist":          True,
        "outtmpl":             f"{download_dir}/%(title)s.%(ext)s",
        "socket_timeout":      60,
        "retries":             5,
        "fragment_retries":    5,
        "http_headers":        COMMON_HEADERS,
        "concurrent_fragment_downloads": 4,
    }

    if extract_audio:
        ydl_opts.update({
            "format": "bestaudio/best",
            "postprocessors": [{
                "key":              "FFmpegExtractAudio",
                "preferredcodec":   "mp3",
                "preferredquality": "192",
            }],
        })
    else:
        h = int(quality)
        ydl_opts["format"] = (
            f"bestvideo[height<={h}][ext=mp4]+bestaudio[ext=m4a]"
            f"/bestvideo[height<={h}]+bestaudio"
            f"/best[height<={h}][ext=mp4]"
            f"/best[height<={h}]"
            f"/best"
        )
        ydl_opts["merge_output_format"] = "mp4"
        ydl_opts["postprocessors"] = [{
            "key":           "FFmpegVideoConvertor",
            "preferedformat": "mp4",
        }]

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)

        title         = info.get("title", "video")
        thumbnail_url = info.get("thumbnail")

        all_files   = list(Path(download_dir).glob("*"))
        media_files = [
            f for f in all_files
            if f.suffix.lower() not in (".jpg", ".jpeg", ".png", ".webp", ".part")
        ]
        chosen = media_files[0] if media_files else (all_files[0] if all_files else None)

        if chosen:
            real_size = os.path.getsize(str(chosen))
            if real_size > 4 * 1024 * 1024 * 1024:   # 4 GB hard cap
                return None, "file_too_large", title, None
            return str(chosen), None, title, thumbnail_url

        return None, "no_file_found", title, None

    except Exception as e:
        logger.error(f"_blocking_download error: {e}")
        return None, str(e), None, None


# ── / — beautiful single-page UI ──────────────────────────────────────────────
HTML_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>MANDAL Downloader</title>
<link rel="preconnect" href="https://fonts.googleapis.com"/>
<link href="https://fonts.googleapis.com/css2?family=Bebas+Neue&family=DM+Mono:wght@400;500&family=DM+Sans:wght@300;400;600&display=swap" rel="stylesheet"/>
<style>
  :root{
    --bg:#0a0a0f;
    --surface:#13131a;
    --card:#1c1c27;
    --border:#2a2a3d;
    --accent:#ff3c78;
    --accent2:#00e5ff;
    --gold:#ffd166;
    --text:#e8e8f0;
    --muted:#6b6b8a;
    --radius:14px;
  }
  *{box-sizing:border-box;margin:0;padding:0}
  body{
    background:var(--bg);
    color:var(--text);
    font-family:'DM Sans',sans-serif;
    min-height:100vh;
    display:flex;
    flex-direction:column;
    align-items:center;
    overflow-x:hidden;
  }

  /* ── background decoration ── */
  body::before{
    content:'';
    position:fixed;inset:0;
    background:
      radial-gradient(ellipse 80% 50% at 20% -10%, rgba(255,60,120,.15) 0%, transparent 60%),
      radial-gradient(ellipse 60% 40% at 80% 110%, rgba(0,229,255,.12) 0%, transparent 60%);
    pointer-events:none;z-index:0;
  }

  .container{
    position:relative;z-index:1;
    width:100%;max-width:720px;
    padding:40px 20px 80px;
  }

  /* ── header ── */
  header{text-align:center;margin-bottom:48px}
  .logo{
    font-family:'Bebas Neue',sans-serif;
    font-size:clamp(52px,10vw,88px);
    letter-spacing:4px;
    line-height:1;
    background:linear-gradient(135deg,var(--accent) 0%,var(--accent2) 100%);
    -webkit-background-clip:text;-webkit-text-fill-color:transparent;
    background-clip:text;
  }
  .tagline{
    font-family:'DM Mono',monospace;
    font-size:12px;
    color:var(--muted);
    letter-spacing:3px;
    text-transform:uppercase;
    margin-top:6px;
  }

  /* ── input section ── */
  .input-wrap{
    display:flex;gap:10px;
    background:var(--card);
    border:1px solid var(--border);
    border-radius:var(--radius);
    padding:10px;
    transition:border-color .25s;
  }
  .input-wrap:focus-within{border-color:var(--accent)}
  #urlInput{
    flex:1;
    background:none;border:none;outline:none;
    font-family:'DM Mono',monospace;
    font-size:14px;color:var(--text);
    padding:8px 12px;
  }
  #urlInput::placeholder{color:var(--muted)}
  #fetchBtn{
    background:linear-gradient(135deg,var(--accent),#c0254d);
    border:none;border-radius:9px;
    color:#fff;font-family:'DM Sans',sans-serif;
    font-weight:600;font-size:14px;
    padding:10px 22px;cursor:pointer;
    transition:opacity .2s,transform .15s;
    white-space:nowrap;
  }
  #fetchBtn:hover{opacity:.9;transform:translateY(-1px)}
  #fetchBtn:active{transform:translateY(0)}
  #fetchBtn:disabled{opacity:.45;cursor:not-allowed;transform:none}

  /* ── status ── */
  #status{
    font-family:'DM Mono',monospace;
    font-size:13px;color:var(--muted);
    text-align:center;margin-top:16px;
    min-height:20px;
  }
  #status.error{color:#ff6b6b}

  /* ── video info card ── */
  #infoCard{
    display:none;
    background:var(--card);
    border:1px solid var(--border);
    border-radius:var(--radius);
    overflow:hidden;
    margin-top:28px;
    animation:slideUp .35s ease;
  }
  @keyframes slideUp{from{opacity:0;transform:translateY(20px)}to{opacity:1;transform:translateY(0)}}

  .thumb-wrap{position:relative;width:100%}
  .thumb-wrap img{
    width:100%;height:220px;object-fit:cover;display:block;
  }
  .thumb-overlay{
    position:absolute;inset:0;
    background:linear-gradient(to top, rgba(10,10,15,.95) 0%, transparent 60%);
  }
  .video-meta{padding:20px 24px 0}
  .video-title{
    font-family:'DM Sans',sans-serif;
    font-size:17px;font-weight:600;
    line-height:1.35;
    display:-webkit-box;-webkit-line-clamp:2;
    -webkit-box-orient:vertical;overflow:hidden;
  }
  .video-sub{
    font-family:'DM Mono',monospace;
    font-size:12px;color:var(--muted);
    margin-top:6px;
  }

  /* ── quality grid ── */
  .quality-label{
    font-family:'DM Mono',monospace;
    font-size:11px;letter-spacing:2px;
    text-transform:uppercase;color:var(--muted);
    padding:20px 24px 10px;
  }
  .quality-grid{
    display:grid;
    grid-template-columns:repeat(auto-fill,minmax(120px,1fr));
    gap:10px;
    padding:0 24px 24px;
  }
  .qbtn{
    background:var(--surface);
    border:1px solid var(--border);
    border-radius:10px;
    color:var(--text);
    font-family:'DM Mono',monospace;
    font-size:13px;font-weight:500;
    padding:12px 8px;
    cursor:pointer;
    text-align:center;
    transition:all .2s;
    position:relative;overflow:hidden;
  }
  /* PINK hover for video quality buttons */
  .qbtn:hover{
    border-color:var(--accent);
    color:var(--accent);
    background:rgba(255,60,120,.08);
    transform:translateY(-2px);
  }
  .qbtn.audio{border-color:var(--gold);color:var(--gold)}
  .qbtn.audio:hover{
    background:rgba(255,209,102,.08);
    border-color:var(--gold);
    color:var(--gold);
  }
  .qbtn .size{
    display:block;font-size:10px;
    color:var(--muted);margin-top:3px;
  }
  .qbtn.loading{
    opacity:.6;cursor:not-allowed;transform:none !important;
    animation:pulse 1s infinite;
  }
  @keyframes pulse{0%,100%{opacity:.6}50%{opacity:.3}}

  /* ── download progress bar ── */
  .dl-progress{
    height:3px;
    background:linear-gradient(90deg,var(--accent),var(--accent2));
    width:0%;transition:width .3s;
    border-radius:0 0 0 0;
    display:none;
  }

  /* ── footer ── */
  footer{
    position:relative;z-index:1;
    text-align:center;
    font-family:'DM Mono',monospace;
    font-size:11px;color:var(--muted);
    margin-top:auto;padding:20px;
    letter-spacing:1px;
  }
  footer span{color:var(--accent)}

  /* ── spinner ── */
  .spin{
    display:inline-block;width:14px;height:14px;
    border:2px solid rgba(255,255,255,.2);
    border-top-color:#fff;border-radius:50%;
    animation:spin .7s linear infinite;
    vertical-align:middle;margin-right:6px;
  }
  @keyframes spin{to{transform:rotate(360deg)}}

  /* ── VIDEO PLAYER MODAL ── */
  #playerModal{
    display:none;
    position:fixed;inset:0;z-index:1000;
    background:rgba(0,0,0,.88);
    backdrop-filter:blur(8px);
    align-items:center;justify-content:center;
    animation:fadeIn .2s ease;
  }
  #playerModal.open{display:flex}
  @keyframes fadeIn{from{opacity:0}to{opacity:1}}

  .modal-inner{
    position:relative;
    width:min(92vw,860px);
    background:var(--card);
    border:1px solid var(--border);
    border-radius:18px;
    overflow:hidden;
    box-shadow:0 32px 80px rgba(0,0,0,.7);
    animation:scaleUp .25s ease;
  }
  @keyframes scaleUp{from{transform:scale(.94);opacity:0}to{transform:scale(1);opacity:1}}

  .modal-header{
    display:flex;align-items:center;justify-content:space-between;
    padding:14px 18px;
    border-bottom:1px solid var(--border);
  }
  .modal-title{
    font-family:'DM Sans',sans-serif;
    font-size:14px;font-weight:600;
    color:var(--text);
    white-space:nowrap;overflow:hidden;text-overflow:ellipsis;
    max-width:calc(100% - 120px);
  }
  .modal-actions{display:flex;gap:8px;align-items:center;flex-shrink:0}

  /* PINK download button inside modal */
  .modal-dl-btn{
    background:linear-gradient(135deg,var(--accent),#c0254d);
    border:none;border-radius:8px;
    color:#fff;font-family:'DM Sans',sans-serif;
    font-weight:600;font-size:12px;
    padding:7px 14px;cursor:pointer;
    transition:opacity .2s,transform .15s;
    white-space:nowrap;
  }
  .modal-dl-btn:hover{opacity:.9;transform:translateY(-1px)}

  .modal-close{
    background:var(--surface);
    border:1px solid var(--border);
    border-radius:8px;
    color:var(--muted);
    font-size:18px;line-height:1;
    width:32px;height:32px;
    cursor:pointer;
    display:flex;align-items:center;justify-content:center;
    transition:color .2s,border-color .2s;
    flex-shrink:0;
  }
  .modal-close:hover{color:var(--text);border-color:var(--accent)}

  .modal-video-wrap{
    background:#000;
    display:flex;align-items:center;justify-content:center;
  }
  #modalVideo{
    width:100%;
    max-height:70vh;
    display:block;
    outline:none;
  }
  #modalAudio{
    width:100%;
    padding:20px;
    display:block;
  }

  .modal-loading{
    display:flex;flex-direction:column;
    align-items:center;justify-content:center;
    padding:60px 20px;gap:14px;
    color:var(--muted);
    font-family:'DM Mono',monospace;font-size:13px;
  }
  .modal-loading .big-spin{
    width:36px;height:36px;
    border:3px solid rgba(255,60,120,.2);
    border-top-color:var(--accent);
    border-radius:50%;
    animation:spin .8s linear infinite;
  }

  @media(max-width:480px){
    .quality-grid{grid-template-columns:repeat(3,1fr)}
    .thumb-wrap img{height:160px}
    #modalVideo{max-height:55vw}
  }
</style>
</head>
<body>
<div class="container">

  <header>
    <div class="logo">MANDAL<br/>DL</div>
    <p class="tagline">Universal Media Downloader &mdash; v1.0</p>
  </header>

  <div class="input-wrap">
    <input
      id="urlInput"
      type="url"
      placeholder="Paste YouTube, Instagram, TikTok, Twitter URL..."
      autocomplete="off" spellcheck="false"
    />
    <button id="fetchBtn" onclick="fetchInfo()">Fetch &#x2197;</button>
  </div>

  <div id="status"></div>

  <div id="infoCard">
    <div class="thumb-wrap">
      <img id="thumbnail" src="" alt="thumbnail"/>
      <div class="thumb-overlay"></div>
    </div>
    <div class="dl-progress" id="dlBar"></div>
    <div class="video-meta">
      <div class="video-title" id="videoTitle"></div>
      <div class="video-sub" id="videoSub"></div>
    </div>
    <div class="quality-label">Select Quality to Play / Download</div>
    <div class="quality-grid" id="qualityGrid"></div>
  </div>

</div>

<!-- ── VIDEO PLAYER MODAL ── -->
<div id="playerModal">
  <div class="modal-inner">
    <div class="modal-header">
      <div class="modal-title" id="modalTitle">Loading...</div>
      <div class="modal-actions">
        <button class="modal-dl-btn" id="modalDlBtn" onclick="triggerDownload()">&#x2B07; Download</button>
        <button class="modal-close" onclick="closePlayer()">&#x2715;</button>
      </div>
    </div>
    <div class="modal-video-wrap" id="modalBody">
      <div class="modal-loading" id="modalLoading">
        <div class="big-spin"></div>
        <span>Processing... thoda wait karo</span>
      </div>
      <video id="modalVideo" controls playsinline style="display:none"></video>
      <audio id="modalAudio" controls style="display:none"></audio>
    </div>
  </div>
</div>

<footer>Built by <span>@MANDAL4482</span> &mdash; yt-dlp + ffmpeg + FastAPI</footer>

<script>
  const $ = id => document.getElementById(id);

  function setStatus(msg, isError=false){
    const el = $('status');
    el.innerHTML = msg;
    el.className = isError ? 'error' : '';
  }

  function fmtSize(bytes){
    if(!bytes) return '';
    if(bytes > 1073741824) return (bytes/1073741824).toFixed(1)+' GB';
    if(bytes > 1048576)    return (bytes/1048576).toFixed(1)+' MB';
    return (bytes/1024).toFixed(0)+' KB';
  }

  function fmtDuration(s){
    if(!s) return '';
    const m = Math.floor(s/60), sec = s%60;
    return m+'m '+String(sec).padStart(2,'0')+'s';
  }

  let currentUrl    = '';
  let activeQuality = '';
  let activeLabel   = '';

  async function fetchInfo(){
    const url = $('urlInput').value.trim();
    if(!url){ setStatus('&#x26A0;&#xFE0F; URL daalo pehle!', true); return; }
    currentUrl = url;

    const btn = $('fetchBtn');
    btn.disabled = true;
    btn.innerHTML = '<span class="spin"></span>Fetching...';
    setStatus('<span class="spin"></span> Video info la raha hoon...');
    $('infoCard').style.display = 'none';

    try{
      const res  = await fetch('/info?url='+encodeURIComponent(url));
      const data = await res.json();

      if(!res.ok || !data.ok){
        setStatus('&#x274C; '+(data.detail || data.error || 'URL se info nahi mili'), true);
        return;
      }

      const thumb = $('thumbnail');
      if(data.thumbnail){ thumb.src = data.thumbnail; thumb.style.display='block'; }
      else { thumb.style.display='none'; }

      $('videoTitle').textContent = data.title || 'Untitled';
      $('videoSub').textContent   = [
        data.uploader,
        fmtDuration(data.duration)
      ].filter(Boolean).join(' · ');

      // Build quality buttons — click opens player
      const grid = $('qualityGrid');
      grid.innerHTML = '';
      data.formats.forEach(f => {
        const btn = document.createElement('button');
        btn.className = 'qbtn' + (f.quality==='audio' ? ' audio' : '');
        btn.dataset.quality = f.quality;
        btn.dataset.label   = f.label;
        const sz = fmtSize(f.filesize);
        btn.innerHTML = '&#x25B6; ' + f.label + (sz ? '<span class="size">'+sz+'</span>' : '');
        btn.onclick = () => openPlayer(f.quality, f.label);
        grid.appendChild(btn);
      });

      $('infoCard').style.display = 'block';
      setStatus('&#x2705; '+data.formats.length+' quality options mili! Play karo ya download karo.');

    }catch(e){
      setStatus('&#x274C; Network error: '+e.message, true);
    }finally{
      btn.disabled = false;
      btn.innerHTML = 'Fetch &#x2197;';
    }
  }

  // ── Player modal ──────────────────────────────────────────────────────────
  function openPlayer(quality, label){
    activeQuality = quality;
    activeLabel   = label;

    $('modalTitle').textContent = $('videoTitle').textContent;
    $('modalLoading').style.display = 'flex';
    $('modalLoading').innerHTML = '<div class="big-spin"></div><span>Processing... thoda wait karo</span>';
    $('modalVideo').style.display = 'none';
    $('modalAudio').style.display = 'none';
    $('modalVideo').src = '';
    $('modalAudio').src = '';

    $('playerModal').classList.add('open');
    document.body.style.overflow = 'hidden';

    const streamUrl = '/stream?url='+encodeURIComponent(currentUrl)+'&quality='+encodeURIComponent(quality);
    const isAudio   = quality === 'audio';

    if(isAudio){
      const audio = $('modalAudio');
      audio.src = streamUrl;
      audio.oncanplay = () => {
        $('modalLoading').style.display = 'none';
        audio.style.display = 'block';
        audio.play().catch(()=>{});
      };
      audio.onerror = () => {
        $('modalLoading').innerHTML = '<span style="color:#ff6b6b">&#x274C; Stream load nahi hua. Download try karo.</span>';
      };
    } else {
      const video = $('modalVideo');
      video.src = streamUrl;
      video.oncanplay = () => {
        $('modalLoading').style.display = 'none';
        video.style.display = 'block';
        video.play().catch(()=>{});
      };
      video.onerror = () => {
        $('modalLoading').innerHTML = '<span style="color:#ff6b6b">&#x274C; Stream load nahi hua. Download try karo.</span>';
      };
    }
  }

  function closePlayer(){
    $('playerModal').classList.remove('open');
    document.body.style.overflow = '';
    const v = $('modalVideo');
    const a = $('modalAudio');
    v.pause(); v.src = '';
    a.pause(); a.src = '';
  }

  function triggerDownload(){
    const dlUrl = '/download?url='+encodeURIComponent(currentUrl)+'&quality='+encodeURIComponent(activeQuality);
    const a = document.createElement('a');
    a.href = dlUrl;
    a.download = '';
    document.body.appendChild(a);
    a.click();
    a.remove();
    setStatus('&#x2B07;&#xFE0F; Download shuru ho gayi background mein...');
  }

  // Close modal on backdrop click
  $('playerModal').addEventListener('click', e => {
    if(e.target === $('playerModal')) closePlayer();
  });

  // ESC to close
  document.addEventListener('keydown', e => {
    if(e.key === 'Escape') closePlayer();
  });

  // Enter key on URL input
  $('urlInput').addEventListener('keydown', e => {
    if(e.key === 'Enter') fetchInfo();
  });
</script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
async def root():
    return HTMLResponse(content=HTML_PAGE)
