#!/usr/bin/env python3
"""
Universal Media Downloader — FastAPI Edition
Developer: MANDAL !!
Version: 2.0 — Pro UI
"""

import os, re, shutil, logging, asyncio, tempfile
from pathlib import Path

import yt_dlp
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import StreamingResponse, HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware

logging.basicConfig(level=logging.INFO, format="%(asctime)s — %(levelname)s — %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(title="MANDAL Downloader", version="2.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

COMMON_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}

def clean_filename(name: str) -> str:
    name = re.sub(r"@\w+", "", name)
    name = re.sub(r"https?://\S+", "", name)
    name = re.sub(r"[^\w\s\-]", " ", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name[:80] if name else "video"


@app.get("/info")
async def get_info(url: str = Query(...)):
    loop = asyncio.get_event_loop()
    try:
        result = await loop.run_in_executor(None, _fetch_formats, url)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error", "Could not fetch info"))
    return JSONResponse(result)


def _fetch_formats(url: str) -> dict:
    opts = {"quiet": True, "no_warnings": True, "noplaylist": True,
            "socket_timeout": 30, "http_headers": COMMON_HEADERS}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)

        title     = info.get("title", "video")
        duration  = info.get("duration", 0)
        thumbnail = info.get("thumbnail")
        uploader  = info.get("uploader", "")
        raw_fmts  = info.get("formats", [])

        seen, quality_list = set(), []
        for f in reversed(raw_fmts):
            h = f.get("height")
            if not h or f.get("vcodec", "none") == "none":
                continue
            fs = f.get("filesize") or f.get("filesize_approx") or 0
            if h not in seen:
                seen.add(h)
                quality_list.append({"height": h, "ext": f.get("ext","mp4"),
                    "filesize": fs, "has_audio": f.get("acodec","none") != "none",
                    "label": f"{h}p", "quality": str(h)})

        quality_list.sort(key=lambda x: x["height"], reverse=True)
        if not quality_list:
            for h in [1080,720,480,360,240]:
                quality_list.append({"height":h,"ext":"mp4","filesize":0,
                    "has_audio":True,"label":f"{h}p","quality":str(h)})

        quality_list.append({"height":0,"ext":"mp3","filesize":0,
            "has_audio":True,"label":"MP3 🎵","quality":"audio"})

        return {"ok":True,"title":title,"uploader":uploader,
                "duration":duration,"thumbnail":thumbnail,"formats":quality_list}
    except Exception as e:
        logger.error(f"_fetch_formats error: {e}")
        return {"ok":False,"error":str(e)}


@app.get("/download")
async def download_video(
    url: str     = Query(...),
    quality: str = Query("720"),
):
    loop = asyncio.get_event_loop()
    tmp_dir = tempfile.mkdtemp(prefix="dlr_")
    try:
        filepath, err, title, _ = await loop.run_in_executor(
            None, _blocking_download, url, tmp_dir, quality)
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
                while chunk := f.read(1024 * 1024):
                    yield chunk
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    headers = {
        "Content-Disposition": f'attachment; filename="{dl_name}"',
        "Content-Length": str(file_size),
        "Accept-Ranges": "bytes",
    }
    return StreamingResponse(iter_file(), media_type=media_type, headers=headers)


def _blocking_download(url, download_dir, quality):
    extract_audio = (quality == "audio")
    ydl_opts = {
        "quiet": True, "no_warnings": True, "noplaylist": True,
        "outtmpl": f"{download_dir}/%(title)s.%(ext)s",
        "socket_timeout": 60, "retries": 5, "fragment_retries": 5,
        "http_headers": COMMON_HEADERS,
        "concurrent_fragment_downloads": 4,
    }
    if extract_audio:
        ydl_opts.update({"format": "bestaudio/best",
            "postprocessors": [{"key":"FFmpegExtractAudio",
                "preferredcodec":"mp3","preferredquality":"192"}]})
    else:
        h = int(quality)
        ydl_opts["format"] = (
            f"bestvideo[height<={h}][ext=mp4]+bestaudio[ext=m4a]"
            f"/bestvideo[height<={h}]+bestaudio"
            f"/best[height<={h}][ext=mp4]/best[height<={h}]/best")
        ydl_opts["merge_output_format"] = "mp4"
        ydl_opts["postprocessors"] = [{"key":"FFmpegVideoConvertor","preferedformat":"mp4"}]

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
        title = info.get("title","video")
        thumbnail_url = info.get("thumbnail")
        all_files = list(Path(download_dir).glob("*"))
        media_files = [f for f in all_files if f.suffix.lower() not in
                       (".jpg",".jpeg",".png",".webp",".part")]
        chosen = media_files[0] if media_files else (all_files[0] if all_files else None)
        if chosen:
            real_size = os.path.getsize(str(chosen))
            if real_size > 4 * 1024**3:
                return None, "file_too_large", title, None
            return str(chosen), None, title, thumbnail_url
        return None, "no_file_found", title, None
    except Exception as e:
        logger.error(f"_blocking_download error: {e}")
        return None, str(e), None, None


# ─────────────────────────────────────────────────────────────────────────────
#  FULL HTML UI
# ─────────────────────────────────────────────────────────────────────────────
HTML_PAGE = r"""<!DOCTYPE html>
<html lang="en" data-theme="dark">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>MANDAL Downloader</title>
<link rel="preconnect" href="https://fonts.googleapis.com"/>
<link href="https://fonts.googleapis.com/css2?family=Bebas+Neue&family=Space+Mono:wght@400;700&family=Sora:wght@300;400;500;600;700&display=swap" rel="stylesheet"/>
<style>
/* ══ TOKENS ═══════════════════════════════════════════════════════════════ */
:root{
  --r:14px;
  --ease:cubic-bezier(.4,0,.2,1);
  --sidebar-w:300px;
}
[data-theme="dark"]{
  --bg:#080810;
  --bg2:#0e0e1a;
  --surface:#141420;
  --card:#1a1a28;
  --card2:#20203a;
  --border:#2c2c45;
  --border2:#3a3a58;
  --accent:#e040fb;
  --accent2:#00e5ff;
  --green:#00e676;
  --gold:#ffb300;
  --red:#ff5252;
  --text:#eeeef8;
  --text2:#a0a0c0;
  --muted:#55556a;
  --shadow:rgba(0,0,0,.7);
  --overlay:rgba(8,8,16,.85);
}
[data-theme="light"]{
  --bg:#f0f0f8;
  --bg2:#e8e8f4;
  --surface:#ffffff;
  --card:#f8f8ff;
  --card2:#ededff;
  --border:#d0d0e8;
  --border2:#b8b8d8;
  --accent:#9c27b0;
  --accent2:#0097a7;
  --green:#2e7d32;
  --gold:#e65100;
  --red:#c62828;
  --text:#1a1a2e;
  --text2:#44446a;
  --muted:#8888a8;
  --shadow:rgba(0,0,0,.15);
  --overlay:rgba(240,240,248,.88);
}

/* ══ RESET ════════════════════════════════════════════════════════════════ */
*{box-sizing:border-box;margin:0;padding:0;transition:background-color .3s var(--ease),border-color .3s var(--ease),color .2s var(--ease)}
html{scroll-behavior:smooth}
body{
  font-family:'Sora',sans-serif;
  background:var(--bg);
  color:var(--text);
  min-height:100vh;
  overflow-x:hidden;
}

/* ══ GLOW BG ══════════════════════════════════════════════════════════════ */
.glow-bg{
  position:fixed;inset:0;pointer-events:none;z-index:0;
  background:
    radial-gradient(ellipse 70% 55% at 15% 0%,rgba(224,64,251,.12) 0%,transparent 65%),
    radial-gradient(ellipse 55% 45% at 85% 100%,rgba(0,229,255,.10) 0%,transparent 65%),
    radial-gradient(ellipse 40% 35% at 55% 50%,rgba(0,230,118,.05) 0%,transparent 60%);
  transition:none;
}

/* ══ TOPBAR ═══════════════════════════════════════════════════════════════ */
.topbar{
  position:fixed;top:0;left:0;right:0;z-index:100;
  height:62px;
  display:flex;align-items:center;justify-content:space-between;
  padding:0 20px;
  background:var(--bg2);
  border-bottom:1px solid var(--border);
  backdrop-filter:blur(20px);
}
.hamburger{
  display:flex;flex-direction:column;justify-content:center;
  gap:5px;width:40px;height:40px;
  cursor:pointer;border-radius:10px;padding:8px;
  border:none;background:transparent;
}
.hamburger span{
  display:block;height:2px;border-radius:2px;
  background:var(--text2);
  transition:transform .35s var(--ease),opacity .25s,width .3s var(--ease),background .2s;
}
.hamburger span:nth-child(2){width:70%}
.hamburger:hover span{background:var(--accent)}
.hamburger.open span:nth-child(1){transform:translateY(7px) rotate(45deg)}
.hamburger.open span:nth-child(2){opacity:0;width:0}
.hamburger.open span:nth-child(3){transform:translateY(-7px) rotate(-45deg)}

.topbar-title{
  font-family:'Bebas Neue',sans-serif;
  font-size:26px;
  letter-spacing:3px;
  background:linear-gradient(135deg,var(--accent),var(--accent2));
  -webkit-background-clip:text;-webkit-text-fill-color:transparent;
  background-clip:text;
}
.topbar-right{display:flex;align-items:center;gap:8px}

/* ── Settings Btn ── */
.settings-btn{
  width:40px;height:40px;
  display:flex;align-items:center;justify-content:center;
  border:none;border-radius:10px;
  background:var(--card);border:1px solid var(--border);
  cursor:pointer;color:var(--text2);
  font-size:19px;
  transition:all .2s;
}
.settings-btn:hover{background:var(--card2);color:var(--accent);border-color:var(--accent);transform:rotate(30deg)}

/* ══ SETTINGS PANEL ═══════════════════════════════════════════════════════ */
.settings-panel{
  position:fixed;top:70px;right:16px;z-index:200;
  width:260px;
  background:var(--surface);
  border:1px solid var(--border2);
  border-radius:16px;
  box-shadow:0 20px 60px var(--shadow);
  padding:8px;
  transform:translateY(-12px) scale(.95);
  opacity:0;pointer-events:none;
  transition:all .25s var(--ease);
}
.settings-panel.open{transform:translateY(0) scale(1);opacity:1;pointer-events:all}
.settings-head{
  font-size:11px;letter-spacing:2px;text-transform:uppercase;
  color:var(--muted);padding:10px 12px 6px;
  font-family:'Space Mono',monospace;
}
.setting-row{
  display:flex;align-items:center;justify-content:space-between;
  padding:10px 12px;border-radius:10px;
  cursor:pointer;
  user-select:none;
}
.setting-row:hover{background:var(--card)}
.setting-label{display:flex;align-items:center;gap:10px;font-size:14px;font-weight:500}
.setting-label svg{opacity:.7}

/* ── Toggle Switch ── */
.toggle{
  width:44px;height:24px;
  background:var(--border);border-radius:99px;
  position:relative;cursor:pointer;flex-shrink:0;
  transition:background .3s;
}
.toggle::after{
  content:'';position:absolute;top:3px;left:3px;
  width:18px;height:18px;border-radius:50%;
  background:#fff;
  transition:transform .3s var(--ease),background .3s;
  box-shadow:0 1px 4px rgba(0,0,0,.3);
}
.toggle.on{background:var(--accent)}
.toggle.on::after{transform:translateX(20px)}

/* ══ SIDEBAR ══════════════════════════════════════════════════════════════ */
.sidebar-overlay{
  position:fixed;inset:0;z-index:149;
  background:var(--overlay);
  opacity:0;pointer-events:none;
  transition:opacity .35s var(--ease);
  backdrop-filter:blur(4px);
}
.sidebar-overlay.open{opacity:1;pointer-events:all}

.sidebar{
  position:fixed;top:0;left:0;bottom:0;z-index:150;
  width:var(--sidebar-w);
  background:var(--bg2);
  border-right:1px solid var(--border);
  display:flex;flex-direction:column;
  transform:translateX(-100%);
  transition:transform .38s var(--ease);
  will-change:transform;
}
.sidebar.open{transform:translateX(0)}

.sidebar-header{
  display:flex;align-items:center;justify-content:space-between;
  padding:20px 18px 14px;
  border-bottom:1px solid var(--border);
  flex-shrink:0;
}
.sidebar-title{
  font-size:13px;font-weight:600;letter-spacing:2px;
  text-transform:uppercase;color:var(--text2);
  font-family:'Space Mono',monospace;
}
.sidebar-close{
  width:32px;height:32px;border:none;border-radius:8px;
  background:var(--card);color:var(--text2);cursor:pointer;
  font-size:16px;display:flex;align-items:center;justify-content:center;
}
.sidebar-close:hover{background:var(--red);color:#fff}

.sidebar-list{
  flex:1;overflow-y:auto;padding:10px 10px 20px;
  scrollbar-width:thin;scrollbar-color:var(--border) transparent;
}
.sidebar-list::-webkit-scrollbar{width:4px}
.sidebar-list::-webkit-scrollbar-thumb{background:var(--border);border-radius:4px}

.history-empty{
  text-align:center;padding:40px 20px;
  color:var(--muted);font-size:13px;
  font-family:'Space Mono',monospace;
  line-height:1.8;
}

/* ── History Item ── */
.hist-item{
  background:var(--card);
  border:1px solid var(--border);
  border-radius:12px;
  margin-bottom:10px;
  overflow:hidden;
  transition:border-color .2s,transform .2s;
  animation:fadeIn .3s var(--ease);
}
.hist-item:hover{border-color:var(--border2);transform:translateX(3px)}
.hist-thumb-row{display:flex;gap:10px;padding:10px;align-items:flex-start}
.hist-thumb{
  width:68px;height:42px;border-radius:7px;
  object-fit:cover;flex-shrink:0;
  background:var(--card2);
}
.hist-no-thumb{
  width:68px;height:42px;border-radius:7px;flex-shrink:0;
  background:var(--card2);display:flex;align-items:center;justify-content:center;
  font-size:20px;
}
.hist-info{flex:1;min-width:0}
.hist-name{
  font-size:12px;font-weight:600;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis;
  color:var(--text);margin-bottom:3px;
}
.hist-meta{font-size:10px;color:var(--muted);font-family:'Space Mono',monospace}

.hist-actions{
  display:flex;gap:6px;padding:0 10px 10px;
}
.hist-btn{
  flex:1;padding:6px 4px;font-size:11px;font-weight:600;
  border:none;border-radius:7px;cursor:pointer;
  font-family:'Space Mono',monospace;
  display:flex;align-items:center;justify-content:center;gap:4px;
  transition:all .18s;letter-spacing:.5px;
}
.hist-btn.dl{background:rgba(0,230,118,.12);color:var(--green);border:1px solid rgba(0,230,118,.2)}
.hist-btn.dl:hover{background:var(--green);color:#000}
.hist-btn.ren{background:rgba(0,229,255,.1);color:var(--accent2);border:1px solid rgba(0,229,255,.2)}
.hist-btn.ren:hover{background:var(--accent2);color:#000}
.hist-btn.del{background:rgba(255,82,82,.1);color:var(--red);border:1px solid rgba(255,82,82,.2)}
.hist-btn.del:hover{background:var(--red);color:#fff}

.sidebar-clear{
  flex-shrink:0;padding:12px;
  border-top:1px solid var(--border);
}
.clear-all-btn{
  width:100%;padding:10px;border:1px solid rgba(255,82,82,.3);
  border-radius:10px;background:rgba(255,82,82,.06);
  color:var(--red);font-size:12px;font-weight:600;
  cursor:pointer;font-family:'Space Mono',monospace;
  letter-spacing:1px;
  transition:all .2s;
}
.clear-all-btn:hover{background:var(--red);color:#fff;border-color:var(--red)}

/* ══ MAIN CONTENT ═════════════════════════════════════════════════════════ */
.main{
  position:relative;z-index:1;
  padding:90px 20px 80px;
  min-height:100vh;
  display:flex;flex-direction:column;
  align-items:center;
}

/* ── Hero heading ── */
.hero{text-align:center;margin-bottom:36px}
.hero-badge{
  display:inline-block;
  font-family:'Space Mono',monospace;
  font-size:10px;letter-spacing:3px;text-transform:uppercase;
  color:var(--accent);background:rgba(224,64,251,.1);
  border:1px solid rgba(224,64,251,.25);
  padding:5px 14px;border-radius:99px;margin-bottom:14px;
}
.hero-title{
  font-family:'Bebas Neue',sans-serif;
  font-size:clamp(44px,9vw,80px);
  letter-spacing:5px;line-height:1;
  color:var(--text);
}
.hero-title span{
  background:linear-gradient(135deg,var(--accent) 20%,var(--accent2) 80%);
  -webkit-background-clip:text;-webkit-text-fill-color:transparent;
  background-clip:text;
}
.hero-sub{
  font-size:13px;color:var(--text2);margin-top:10px;
  font-weight:300;letter-spacing:.5px;
}

/* ── Card container ── */
.dl-card{
  width:100%;max-width:640px;
  background:var(--surface);
  border:1px solid var(--border);
  border-radius:20px;
  padding:24px;
  box-shadow:0 8px 40px var(--shadow);
}

/* ── URL Input ── */
.url-row{display:flex;gap:10px;margin-bottom:14px}
.url-wrap{
  flex:1;
  display:flex;align-items:center;gap:10px;
  background:var(--bg2);
  border:1.5px solid var(--border);
  border-radius:12px;
  padding:0 14px;
  transition:border-color .2s;
}
.url-wrap:focus-within{border-color:var(--accent);box-shadow:0 0 0 3px rgba(224,64,251,.1)}
.url-icon{font-size:16px;opacity:.5;flex-shrink:0}
#urlInput{
  flex:1;background:none;border:none;outline:none;
  font-family:'Space Mono',monospace;font-size:13px;
  color:var(--text);padding:14px 0;
}
#urlInput::placeholder{color:var(--muted)}
.fetch-btn{
  padding:0 22px;height:52px;
  background:linear-gradient(135deg,var(--accent),#8b00cc);
  border:none;border-radius:12px;
  color:#fff;font-family:'Sora',sans-serif;
  font-weight:700;font-size:14px;
  cursor:pointer;flex-shrink:0;
  display:flex;align-items:center;gap:8px;
  transition:opacity .2s,transform .15s,box-shadow .2s;
  box-shadow:0 4px 20px rgba(224,64,251,.35);
  white-space:nowrap;
}
.fetch-btn:hover{opacity:.9;transform:translateY(-2px);box-shadow:0 8px 28px rgba(224,64,251,.45)}
.fetch-btn:active{transform:translateY(0)}
.fetch-btn:disabled{opacity:.4;cursor:not-allowed;transform:none;box-shadow:none}

/* ── platform pills ── */
.platforms{
  display:flex;flex-wrap:wrap;gap:6px;margin-bottom:18px;
}
.plat{
  font-size:11px;font-family:'Space Mono',monospace;
  color:var(--text2);background:var(--bg2);
  border:1px solid var(--border);
  padding:4px 10px;border-radius:99px;
  cursor:pointer;transition:all .2s;
}
.plat:hover{border-color:var(--accent2);color:var(--accent2)}

/* ── Status bar ── */
#statusBar{
  font-family:'Space Mono',monospace;
  font-size:12px;color:var(--text2);
  text-align:center;min-height:20px;
  margin:10px 0;
  display:flex;align-items:center;justify-content:center;gap:8px;
}
#statusBar.err{color:var(--red)}
#statusBar.ok{color:var(--green)}

/* ══ INFO CARD ════════════════════════════════════════════════════════════ */
#infoCard{
  display:none;
  border-top:1px solid var(--border);
  margin-top:18px;padding-top:20px;
  animation:fadeIn .4s var(--ease);
}
@keyframes fadeIn{from{opacity:0;transform:translateY(14px)}to{opacity:1;transform:translateY(0)}}

.video-row{display:flex;gap:14px;margin-bottom:18px}
.vid-thumb{
  width:120px;height:70px;border-radius:10px;
  object-fit:cover;flex-shrink:0;
  background:var(--card2);
}
.vid-info{flex:1;min-width:0;padding-top:2px}
.vid-title{
  font-size:14px;font-weight:600;line-height:1.4;
  display:-webkit-box;-webkit-line-clamp:2;
  -webkit-box-orient:vertical;overflow:hidden;
  margin-bottom:6px;
}
.vid-meta{
  font-family:'Space Mono',monospace;
  font-size:11px;color:var(--text2);
}

/* ── Progress bar ── */
.prog-bar{
  height:3px;border-radius:3px;
  background:var(--border);
  margin-bottom:16px;overflow:hidden;
}
.prog-fill{
  height:100%;width:0%;
  background:linear-gradient(90deg,var(--accent),var(--accent2));
  border-radius:3px;
  transition:width .4s var(--ease);
}

/* ── Quality section ── */
.qlabel{
  font-size:10px;letter-spacing:2.5px;text-transform:uppercase;
  color:var(--muted);font-family:'Space Mono',monospace;
  margin-bottom:10px;
}
.quality-grid{
  display:grid;
  grid-template-columns:repeat(auto-fill,minmax(105px,1fr));
  gap:8px;
}
.qbtn{
  background:var(--card);
  border:1.5px solid var(--border);
  border-radius:11px;
  padding:10px 8px;
  cursor:pointer;
  text-align:center;
  transition:all .2s var(--ease);
  position:relative;overflow:hidden;
  color:var(--text);
}
.qbtn::before{
  content:'';position:absolute;inset:0;
  background:linear-gradient(135deg,var(--accent),var(--accent2));
  opacity:0;transition:opacity .2s;
}
.qbtn:hover{border-color:var(--accent2);transform:translateY(-3px);box-shadow:0 6px 20px rgba(0,229,255,.15)}
.qbtn:hover::before{opacity:.08}
.qbtn.audio{border-color:rgba(255,179,0,.3)}
.qbtn.audio:hover{border-color:var(--gold);box-shadow:0 6px 20px rgba(255,179,0,.15)}
.qbtn.loading{opacity:.5;cursor:not-allowed;transform:none !important;animation:blink .8s infinite}
@keyframes blink{0%,100%{opacity:.5}50%{opacity:.25}}

.qbtn-label{
  font-size:13px;font-weight:700;position:relative;
  display:flex;align-items:center;justify-content:center;gap:5px;
}
.qbtn-size{
  display:block;font-size:10px;color:var(--muted);
  font-family:'Space Mono',monospace;margin-top:3px;position:relative;
}

/* ══ RENAME MODAL ═════════════════════════════════════════════════════════ */
.modal-overlay{
  position:fixed;inset:0;z-index:300;
  background:var(--overlay);
  backdrop-filter:blur(8px);
  display:flex;align-items:center;justify-content:center;
  opacity:0;pointer-events:none;
  transition:opacity .25s;
}
.modal-overlay.open{opacity:1;pointer-events:all}
.modal-box{
  width:90%;max-width:380px;
  background:var(--surface);
  border:1px solid var(--border2);
  border-radius:18px;
  padding:26px;
  box-shadow:0 30px 80px var(--shadow);
  transform:scale(.9);
  transition:transform .25s var(--ease);
}
.modal-overlay.open .modal-box{transform:scale(1)}
.modal-title{font-size:16px;font-weight:700;margin-bottom:4px}
.modal-sub{font-size:12px;color:var(--muted);margin-bottom:18px;font-family:'Space Mono',monospace}
.modal-input{
  width:100%;background:var(--bg2);
  border:1.5px solid var(--border);
  border-radius:10px;
  padding:12px 14px;
  font-family:'Sora',sans-serif;
  font-size:14px;color:var(--text);
  outline:none;margin-bottom:16px;
  transition:border-color .2s;
}
.modal-input:focus{border-color:var(--accent)}
.modal-btns{display:flex;gap:8px}
.modal-btn{
  flex:1;padding:11px;border:none;border-radius:10px;
  font-size:13px;font-weight:600;cursor:pointer;
  font-family:'Sora',sans-serif;transition:all .2s;
}
.modal-btn.cancel{background:var(--card);color:var(--text2)}
.modal-btn.cancel:hover{background:var(--card2)}
.modal-btn.save{background:linear-gradient(135deg,var(--accent2),#0077ff);color:#fff}
.modal-btn.save:hover{opacity:.88;transform:translateY(-1px)}

/* ══ SPINNER ══════════════════════════════════════════════════════════════ */
.spin{
  display:inline-block;width:13px;height:13px;
  border:2px solid rgba(255,255,255,.25);
  border-top-color:currentColor;
  border-radius:50%;animation:spinning .65s linear infinite;
}
@keyframes spinning{to{transform:rotate(360deg)}}

/* ══ FOOTER ═══════════════════════════════════════════════════════════════ */
footer{
  position:relative;z-index:1;
  text-align:center;
  font-family:'Space Mono',monospace;
  font-size:10px;color:var(--muted);
  padding:20px;letter-spacing:1px;
}
footer span{color:var(--accent)}

/* ══ SCROLLBAR ════════════════════════════════════════════════════════════ */
::-webkit-scrollbar{width:5px}
::-webkit-scrollbar-thumb{background:var(--border);border-radius:5px}

/* ══ RESPONSIVE ═══════════════════════════════════════════════════════════ */
@media(max-width:520px){
  .quality-grid{grid-template-columns:repeat(3,1fr)}
  .video-row{flex-direction:column}
  .vid-thumb{width:100%;height:160px}
}
</style>
</head>
<body>

<div class="glow-bg"></div>

<!-- ══ TOPBAR ══════════════════════════════════════════════════════════════ -->
<div class="topbar">
  <button class="hamburger" id="hamburger" onclick="toggleSidebar()" title="History">
    <span></span><span></span><span></span>
  </button>
  <div class="topbar-title">MANDAL DL</div>
  <div class="topbar-right">
    <button class="settings-btn" id="settingsBtn" onclick="toggleSettings()" title="Settings">⚙️</button>
  </div>
</div>

<!-- ══ SETTINGS PANEL ══════════════════════════════════════════════════════ -->
<div class="settings-panel" id="settingsPanel">
  <div class="settings-head">Settings</div>

  <div class="setting-row" onclick="toggleDark()">
    <div class="setting-label">
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
        <path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/>
      </svg>
      Dark Mode
    </div>
    <div class="toggle on" id="darkToggle"></div>
  </div>

  <div class="setting-row" onclick="toggleSidebar();closeSettings()">
    <div class="setting-label">
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
        <path d="M12 20h9M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z"/>
      </svg>
      View History
    </div>
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M9 18l6-6-6-6"/></svg>
  </div>
</div>

<!-- ══ SIDEBAR ═════════════════════════════════════════════════════════════ -->
<div class="sidebar-overlay" id="sidebarOverlay" onclick="toggleSidebar()"></div>
<aside class="sidebar" id="sidebar">
  <div class="sidebar-header">
    <span class="sidebar-title">📂 Download History</span>
    <button class="sidebar-close" onclick="toggleSidebar()">✕</button>
  </div>
  <div class="sidebar-list" id="historyList">
    <div class="history-empty">No downloads yet.<br/>Paste a URL and<br/>start downloading!</div>
  </div>
  <div class="sidebar-clear">
    <button class="clear-all-btn" onclick="clearAllHistory()">🗑️ Clear All History</button>
  </div>
</aside>

<!-- ══ RENAME MODAL ════════════════════════════════════════════════════════ -->
<div class="modal-overlay" id="renameModal">
  <div class="modal-box">
    <div class="modal-title">✏️ Rename</div>
    <div class="modal-sub" id="renameModalSub">Edit the display name</div>
    <input class="modal-input" id="renameInput" type="text" placeholder="Enter new name..." maxlength="120"/>
    <div class="modal-btns">
      <button class="modal-btn cancel" onclick="closeRenameModal()">Cancel</button>
      <button class="modal-btn save" onclick="saveRename()">Save</button>
    </div>
  </div>
</div>

<!-- ══ MAIN ════════════════════════════════════════════════════════════════ -->
<main class="main">

  <div class="hero">
    <div class="hero-badge">✦ Universal Downloader</div>
    <h1 class="hero-title">DOWNLOAD<br/><span>VIDEO</span></h1>
    <p class="hero-sub">YouTube • Instagram • TikTok • Twitter • Facebook & more</p>
  </div>

  <div class="dl-card">
    <!-- URL Row -->
    <div class="url-row">
      <div class="url-wrap">
        <span class="url-icon">🔗</span>
        <input id="urlInput" type="url"
          placeholder="Paste video URL here..."
          autocomplete="off" spellcheck="false"/>
      </div>
      <button class="fetch-btn" id="fetchBtn" onclick="fetchInfo()">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M21 21l-4.35-4.35M17 11A6 6 0 1 1 5 11a6 6 0 0 1 12 0z"/></svg>
        Fetch
      </button>
    </div>

    <!-- Platform pills -->
    <div class="platforms">
      <span class="plat" onclick="demoFill('yt')">🎬 YouTube</span>
      <span class="plat" onclick="demoFill('ig')">📸 Instagram</span>
      <span class="plat" onclick="demoFill('tt')">🎵 TikTok</span>
      <span class="plat" onclick="demoFill('tw')">🐦 Twitter/X</span>
      <span class="plat" onclick="demoFill('fb')">📘 Facebook</span>
    </div>

    <!-- Status -->
    <div id="statusBar"></div>

    <!-- Info Card -->
    <div id="infoCard">
      <!-- Video preview row -->
      <div class="video-row" id="videoRow">
        <img class="vid-thumb" id="vidThumb" src="" alt=""/>
        <div class="vid-info">
          <div class="vid-title" id="vidTitle"></div>
          <div class="vid-meta" id="vidMeta"></div>
        </div>
      </div>

      <!-- Progress -->
      <div class="prog-bar"><div class="prog-fill" id="progFill"></div></div>

      <!-- Quality grid -->
      <div class="qlabel">Select Quality</div>
      <div class="quality-grid" id="qualityGrid"></div>
    </div>
  </div>
</main>

<footer>Built by <span>@MANDAL4482</span> &nbsp;·&nbsp; yt-dlp + ffmpeg + FastAPI</footer>

<!-- ══ SCRIPT ══════════════════════════════════════════════════════════════ -->
<script>
// ─── IndexedDB ────────────────────────────────────────────────────────────
const DB_NAME = 'mandal_dl', DB_VER = 1, STORE = 'history';
let db = null;

function openDB(){
  return new Promise((res,rej)=>{
    const req = indexedDB.open(DB_NAME, DB_VER);
    req.onupgradeneeded = e => e.target.result.createObjectStore(STORE,{keyPath:'id'});
    req.onsuccess = e => { db = e.target.result; res(db); };
    req.onerror   = e => rej(e);
  });
}
async function dbGet(id){
  await openDB();
  return new Promise((res,rej)=>{
    const tx = db.transaction(STORE,'readonly');
    const req = tx.objectStore(STORE).get(id);
    req.onsuccess = ()=>res(req.result);
    req.onerror   = ()=>rej(req.error);
  });
}
async function dbPut(item){
  await openDB();
  return new Promise((res,rej)=>{
    const tx = db.transaction(STORE,'readwrite');
    tx.objectStore(STORE).put(item);
    tx.oncomplete = ()=>res();
    tx.onerror    = ()=>rej(tx.error);
  });
}
async function dbDelete(id){
  await openDB();
  return new Promise((res,rej)=>{
    const tx = db.transaction(STORE,'readwrite');
    tx.objectStore(STORE).delete(id);
    tx.oncomplete = ()=>res();
    tx.onerror    = ()=>rej(tx.error);
  });
}
async function dbAll(){
  await openDB();
  return new Promise((res,rej)=>{
    const tx  = db.transaction(STORE,'readonly');
    const req = tx.objectStore(STORE).getAll();
    req.onsuccess = ()=>res(req.result || []);
    req.onerror   = ()=>rej(req.error);
  });
}
async function dbClear(){
  await openDB();
  return new Promise((res,rej)=>{
    const tx = db.transaction(STORE,'readwrite');
    tx.objectStore(STORE).clear();
    tx.oncomplete = ()=>res();
    tx.onerror    = ()=>rej(tx.error);
  });
}

// ─── Theme ────────────────────────────────────────────────────────────────
const htmlEl = document.documentElement;
let darkMode = localStorage.getItem('dark') !== 'false';
applyTheme();

function applyTheme(){
  htmlEl.setAttribute('data-theme', darkMode ? 'dark' : 'light');
  const tog = document.getElementById('darkToggle');
  if(tog) tog.className = 'toggle ' + (darkMode ? 'on' : '');
}
function toggleDark(){
  darkMode = !darkMode;
  localStorage.setItem('dark', darkMode);
  applyTheme();
}

// ─── Settings panel ───────────────────────────────────────────────────────
let settingsOpen = false;
function toggleSettings(){
  settingsOpen = !settingsOpen;
  document.getElementById('settingsPanel').classList.toggle('open', settingsOpen);
}
function closeSettings(){
  settingsOpen = false;
  document.getElementById('settingsPanel').classList.remove('open');
}
document.addEventListener('click', e=>{
  const sp = document.getElementById('settingsPanel');
  const sb = document.getElementById('settingsBtn');
  if(settingsOpen && !sp.contains(e.target) && !sb.contains(e.target)) closeSettings();
});

// ─── Sidebar ──────────────────────────────────────────────────────────────
let sidebarOpen = false;
function toggleSidebar(){
  sidebarOpen = !sidebarOpen;
  document.getElementById('sidebar').classList.toggle('open', sidebarOpen);
  document.getElementById('sidebarOverlay').classList.toggle('open', sidebarOpen);
  document.getElementById('hamburger').classList.toggle('open', sidebarOpen);
  if(sidebarOpen) renderHistory();
}

// ─── History render ───────────────────────────────────────────────────────
async function renderHistory(){
  const list = document.getElementById('historyList');
  const items = await dbAll();
  if(!items.length){
    list.innerHTML = '<div class="history-empty">No downloads yet.<br/>Paste a URL and<br/>start downloading!</div>';
    return;
  }
  // Sort newest first
  items.sort((a,b)=>b.ts-a.ts);
  list.innerHTML = items.map(it => `
    <div class="hist-item" id="hitem-${it.id}">
      <div class="hist-thumb-row">
        ${it.thumbnail
          ? `<img class="hist-thumb" src="${it.thumbnail}" alt="" onerror="this.style.display='none'"/>`
          : `<div class="hist-no-thumb">🎬</div>`}
        <div class="hist-info">
          <div class="hist-name" title="${esc(it.name)}">${esc(it.name)}</div>
          <div class="hist-meta">${it.quality} · ${it.platform}<br/>${relTime(it.ts)}</div>
        </div>
      </div>
      <div class="hist-actions">
        <button class="hist-btn dl" onclick="reDownload('${it.id}')">⬇ DL</button>
        <button class="hist-btn ren" onclick="openRenameModal('${it.id}')">✏ Rename</button>
        <button class="hist-btn del" onclick="deleteHistItem('${it.id}')">🗑 Del</button>
      </div>
    </div>
  `).join('');
}

function esc(s){ return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;') }
function relTime(ts){
  const diff = Date.now()-ts;
  if(diff<60000) return 'Just now';
  if(diff<3600000) return Math.floor(diff/60000)+'m ago';
  if(diff<86400000) return Math.floor(diff/3600000)+'h ago';
  return Math.floor(diff/86400000)+'d ago';
}

async function deleteHistItem(id){
  await dbDelete(id);
  const el = document.getElementById('hitem-'+id);
  if(el){ el.style.opacity='0';el.style.transform='translateX(-20px)';el.style.transition='all .3s';setTimeout(()=>el.remove(),300) }
  const all = await dbAll();
  if(!all.length) document.getElementById('historyList').innerHTML = '<div class="history-empty">No downloads yet.<br/>Paste a URL and<br/>start downloading!</div>';
}

async function clearAllHistory(){
  if(!confirm('All history delete ho jayegi. Sure?')) return;
  await dbClear();
  renderHistory();
}

async function reDownload(id){
  const item = await dbGet(id);
  if(!item) return;
  document.getElementById('urlInput').value = item.url;
  toggleSidebar();
  fetchInfo();
}

// ─── Rename Modal ─────────────────────────────────────────────────────────
let renameTarget = null;
function openRenameModal(id){
  renameTarget = id;
  dbGet(id).then(it=>{
    if(!it) return;
    document.getElementById('renameInput').value = it.name;
    document.getElementById('renameModalSub').textContent = 'Rename: ' + it.name.slice(0,40);
    document.getElementById('renameModal').classList.add('open');
    document.getElementById('renameInput').focus();
  });
}
function closeRenameModal(){
  renameTarget = null;
  document.getElementById('renameModal').classList.remove('open');
}
async function saveRename(){
  if(!renameTarget) return;
  const newName = document.getElementById('renameInput').value.trim();
  if(!newName) return;
  const it = await dbGet(renameTarget);
  if(!it) return;
  it.name = newName;
  await dbPut(it);
  closeRenameModal();
  renderHistory();
}
document.getElementById('renameInput').addEventListener('keydown',e=>{ if(e.key==='Enter') saveRename() });
document.getElementById('renameModal').addEventListener('click',e=>{ if(e.target===document.getElementById('renameModal')) closeRenameModal() });

// ─── Fetch info ───────────────────────────────────────────────────────────
let currentUrl='', currentData=null;

function setStatus(msg, type=''){
  const el = document.getElementById('statusBar');
  el.innerHTML = msg;
  el.className = type;
}

function fmtSize(b){
  if(!b) return '';
  if(b>1073741824) return (b/1073741824).toFixed(1)+' GB';
  if(b>1048576)    return (b/1048576).toFixed(1)+' MB';
  return (b/1024).toFixed(0)+' KB';
}
function fmtDur(s){
  if(!s) return '';
  const m=Math.floor(s/60), sec=s%60;
  return m+'m '+String(sec).padStart(2,'0')+'s';
}
function detectPlatform(url){
  const u=url.toLowerCase();
  if(u.includes('youtube')||u.includes('youtu.be')) return 'YouTube';
  if(u.includes('instagram')) return 'Instagram';
  if(u.includes('tiktok'))    return 'TikTok';
  if(u.includes('twitter')||u.includes('x.com')) return 'Twitter/X';
  if(u.includes('facebook')||u.includes('fb.')) return 'Facebook';
  return 'Web';
}

async function fetchInfo(){
  const url = document.getElementById('urlInput').value.trim();
  if(!url){ setStatus('⚠️ URL paste karo pehle!','err'); return; }
  currentUrl = url;

  const btn = document.getElementById('fetchBtn');
  btn.disabled = true;
  btn.innerHTML = '<span class="spin"></span> Fetching...';
  document.getElementById('infoCard').style.display = 'none';
  setStatus('<span class="spin"></span> &nbsp;Video info aa rahi hai...');
  setProg(0);

  try{
    setProg(20);
    const res  = await fetch('/info?url='+encodeURIComponent(url));
    const data = await res.json();
    setProg(60);

    if(!res.ok || !data.ok){
      setStatus('❌ &nbsp;'+(data.detail||data.error||'Info nahi mili'),'err');
      return;
    }
    currentData = data;

    // thumbnail
    const thumb = document.getElementById('vidThumb');
    if(data.thumbnail){ thumb.src=data.thumbnail; thumb.style.display='block'; }
    else thumb.style.display='none';

    document.getElementById('vidTitle').textContent = data.title||'Untitled';
    document.getElementById('vidMeta').textContent  =
      [data.uploader, fmtDur(data.duration)].filter(Boolean).join(' · ');

    // Build quality buttons
    const grid = document.getElementById('qualityGrid');
    grid.innerHTML = '';
    data.formats.forEach(f=>{
      const btn = document.createElement('button');
      btn.className = 'qbtn'+(f.quality==='audio'?' audio':'');
      btn.dataset.quality = f.quality;
      btn.dataset.label   = f.label;
      const sz = fmtSize(f.filesize);
      btn.innerHTML = `
        <div class="qbtn-label">${f.quality==='audio'?'🎵':''} ${f.label}</div>
        ${sz?`<span class="qbtn-size">${sz}</span>`:''}`;
      btn.onclick = ()=> startDownload(f.quality, btn, f.label);
      grid.appendChild(btn);
    });

    setProg(100);
    setTimeout(()=>setProg(0,false),600);
    document.getElementById('infoCard').style.display = 'block';
    setStatus('✅ &nbsp;'+data.formats.length+' options — quality choose karo!','ok');

  }catch(e){
    setStatus('❌ &nbsp;Network error: '+e.message,'err');
  }finally{
    btn.disabled = false;
    btn.innerHTML = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M21 21l-4.35-4.35M17 11A6 6 0 1 1 5 11a6 6 0 0 1 12 0z"/></svg> Fetch';
  }
}

function setProg(val, animate=true){
  const fill = document.getElementById('progFill');
  fill.style.transition = animate ? 'width .4s cubic-bezier(.4,0,.2,1)' : 'none';
  fill.style.width = val+'%';
}

// ─── Download ─────────────────────────────────────────────────────────────
async function startDownload(quality, btnEl, label){
  document.querySelectorAll('.qbtn').forEach(b=>{ b.classList.add('loading'); b.disabled=true; });
  btnEl.innerHTML = `<div class="qbtn-label"><span class="spin"></span></div><span class="qbtn-size">downloading</span>`;

  setProg(5);
  setStatus(`⬇️ &nbsp;${label} download ho rahi hai...`);

  let pct = 5;
  const timer = setInterval(()=>{
    pct = Math.min(pct + Math.random()*5, 88);
    setProg(pct);
  }, 700);

  try{
    const dlUrl = `/download?url=${encodeURIComponent(currentUrl)}&quality=${encodeURIComponent(quality)}`;
    const res   = await fetch(dlUrl);

    if(!res.ok){
      const err = await res.json().catch(()=>({detail:'Unknown error'}));
      throw new Error(err.detail||'Download failed');
    }

    const blob = await res.blob();
    const cd   = res.headers.get('Content-Disposition')||'';
    const fnM  = cd.match(/filename="?([^"]+)"?/);
    const filename = fnM ? fnM[1] : (quality==='audio'?'audio.mp3':'video.mp4');

    const a = document.createElement('a');
    a.href  = URL.createObjectURL(blob);
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(a.href);

    // Save to history
    const histId = Date.now().toString();
    await dbPut({
      id: histId,
      url: currentUrl,
      name: currentData?.title || 'Untitled',
      thumbnail: currentData?.thumbnail || null,
      quality: label,
      platform: detectPlatform(currentUrl),
      ts: Date.now(),
    });

    clearInterval(timer);
    setProg(100);
    setTimeout(()=>setProg(0,false),700);
    setStatus('✅ &nbsp;Download complete! File save ho gayi.','ok');

  }catch(e){
    clearInterval(timer);
    setProg(0,false);
    setStatus('❌ &nbsp;'+e.message,'err');
  }

  document.querySelectorAll('.qbtn').forEach(b=>{ b.classList.remove('loading'); b.disabled=false; });
  // Restore labels
  document.querySelectorAll('.qbtn').forEach(b=>{
    if(b.dataset.quality===quality){
      const lbl = b.dataset.label;
      b.innerHTML = `<div class="qbtn-label">${quality==='audio'?'🎵':''} ${lbl}</div>`;
    }
  });
}

// ─── Platform demo fill ───────────────────────────────────────────────────
function demoFill(p){
  const el = document.getElementById('urlInput');
  const samples = {
    yt:'https://www.youtube.com/watch?v=dQw4w9WgXcQ',
    ig:'https://www.instagram.com/p/example/',
    tt:'https://www.tiktok.com/@user/video/example',
    tw:'https://twitter.com/i/status/example',
    fb:'https://www.facebook.com/watch?v=example',
  };
  el.value = samples[p]||'';
  el.focus();
}

// ─── Enter key ────────────────────────────────────────────────────────────
document.getElementById('urlInput').addEventListener('keydown',e=>{ if(e.key==='Enter') fetchInfo() });

// ─── Init ─────────────────────────────────────────────────────────────────
openDB().catch(console.warn);
</script>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse)
async def root():
    return HTMLResponse(content=HTML_PAGE)

if __name__ == "__main__":
    import uvicorn
    import os
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port)
