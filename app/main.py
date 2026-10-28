from __future__ import annotations
import asyncio, os, re, shutil, uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

APP="NoAdsDL → WenClips Bridge"
JOB_DIR=Path(os.getenv("JOB_DIR","/tmp/noadsdl-bridge")); JOB_DIR.mkdir(parents=True,exist_ok=True)
ENGINE=os.getenv("WENCLIPS_ENGINE_URL","https://wenclips-engine.onrender.com").rstrip("/")
jobs: dict[str,dict[str,Any]]={}
lock=asyncio.Lock()
worker=asyncio.Lock()
app=FastAPI(title=APP,version="0.2.2")
app.add_middleware(CORSMiddleware,allow_origins=["*"],allow_methods=["GET","POST","OPTIONS"],allow_headers=["*"])

class Start(BaseModel):
    url:str=Field(min_length=8,max_length=2048)

def valid_url(raw:str)->str:
    u=raw.strip(); p=urlparse(u)
    if p.scheme not in ("http","https") or (p.hostname or "").lower() not in {"youtube.com","www.youtube.com","m.youtube.com","youtu.be","www.youtube-nocookie.com"}:
        raise HTTPException(400,"Informe um link válido do YouTube.")
    return u

async def update(jid:str,**kw):
    async with lock:
        if jid in jobs: jobs[jid].update(kw)

async def first_visible(locators):
    for locator in locators:
        try:
            for i in range(min(await locator.count(),12)):
                x=locator.nth(i)
                if await x.is_visible() and await x.is_enabled(): return x
        except Exception: pass
    return None

async def click_named(page, patterns, roles=("button","link")):
    for pattern in patterns:
        for role in roles:
            try:
                x=page.get_by_role(role,name=pattern).first
                if await x.count() and await x.is_visible() and await x.is_enabled():
                    await x.click(timeout=15000); return
            except Exception: pass
    raise RuntimeError("Não encontrei o botão esperado no NoAdsDL. A interface pode ter mudado ou a página pode ter bloqueado a automação.")

async def click_largest_video_option(page):
    # The site's available video formats are listed as options; the user confirmed
    # the highest-resolution video is the final option, not a literal "largest resolution" button.
    await page.wait_for_timeout(1800)
    candidates=[]
    locator=page.locator('a, button, [role="button"], input[type="radio"], label')
    try:
        count=min(await locator.count(),500)
    except Exception:
        count=0
    resolution=re.compile(r'(?<!\d)(?:144|240|360|480|540|576|720|1080|1440|2160|4320)\s*p\b|\b(?:4k|8k)\b',re.I)
    for i in range(count):
        el=locator.nth(i)
        try:
            if not await el.is_visible() or not await el.is_enabled(): continue
            tag=await el.evaluate("(e)=>e.tagName.toLowerCase()")
            if tag=="input":
                text=" ".join(filter(None,[await el.get_attribute("value"),await el.get_attribute("aria-label"),await el.get_attribute("title")]))
            else:
                text=" ".join(filter(None,[(await el.inner_text())[:250],await el.get_attribute("aria-label"),await el.get_attribute("title")]))
            if not resolution.search(text): continue
            if re.search(r'\baudio\b|\bmp3\b|\bonly audio\b',text,re.I): continue
            candidates.append((el,text.strip().replace("\n"," ")[:140]))
        except Exception: continue
    if not candidates:
        raise RuntimeError("A lista de formatos abriu, mas não consegui identificar opções de vídeo com resolução (ex.: 720p/1080p). Copie esta mensagem e envie para diagnóstico.")
    # Last resolution-bearing clickable option is the highest video option per the site's ordering.
    el,description=candidates[-1]
    try:
        async with page.expect_download(timeout=60000) as info:
            if await el.evaluate("(e)=>e.tagName.toLowerCase()")=="input":
                await el.check(timeout=15000)
                parent=el.locator("xpath=..")
                try: await parent.click(timeout=5000)
                except Exception: pass
            else:
                await el.click(timeout=20000)
        dl=await info.value
        return dl,description
    except PlaywrightTimeoutError as e:
        raise RuntimeError(f"Selecionei a última opção de vídeo encontrada ({description}), mas o NoAdsDL não iniciou o download. A página pode abrir o link em outra aba ou exigir outra ação.") from e

async def download_from_noadsdl(url:str,target:Path):
    timeout=int(os.getenv("BROWSER_TIMEOUT_MS","120000"))
    async with async_playwright() as p:
        browser=await p.chromium.launch(headless=True,args=["--no-sandbox","--disable-dev-shm-usage","--disable-gpu"])
        context=await browser.new_context(accept_downloads=True,viewport={"width":1365,"height":900},locale="en-US")
        page=await context.new_page(); page.set_default_timeout(timeout)
        try:
            await page.goto("https://noadsdl.com/",wait_until="domcontentloaded",timeout=timeout)
            await page.wait_for_timeout(1800)
            field=await first_visible([
                page.locator('input:not([type="hidden"]):not([type="submit"]):not([type="button"]):not([disabled])'),
                page.locator('textarea:not([disabled])'),
                page.locator('[contenteditable="true"]'),
                page.locator('[role="textbox"]:not([aria-readonly="true"])')
            ])
            if not field:
                raise RuntimeError("Não encontrei um campo editável para o link no NoAdsDL. A página pode ter mudado.")
            editable=await field.evaluate("(el) => el.isContentEditable || ['input','textarea'].includes(el.tagName.toLowerCase()) || el.getAttribute('role') === 'textbox'")
            if not editable: raise RuntimeError("O seletor encontrou um elemento não editável; download cancelado.")
            await field.fill(url)
            await click_named(page,[re.compile("continue to available formats",re.I),re.compile("available formats|show formats|formatos disponíveis",re.I)])
            await page.wait_for_load_state("domcontentloaded",timeout=20000)
            dl,description=await click_largest_video_option(page)
            await dl.save_as(str(target))
        finally:
            await context.close(); await browser.close()
    if not target.is_file() or target.stat().st_size<100000: raise RuntimeError("O NoAdsDL não produziu um arquivo de vídeo válido.")
    proc=await asyncio.create_subprocess_exec("ffprobe","-v","error","-show_entries","format=duration","-of","default=noprint_wrappers=1:nokey=1",str(target),stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
    out,err=await proc.communicate()
    if proc.returncode or float(out.decode().strip() or 0)<1: raise RuntimeError("O arquivo baixado não parece ser um vídeo válido: "+err.decode(errors="replace")[-300:])

async def send_to_engine(video:Path):
    async with httpx.AsyncClient(timeout=httpx.Timeout(240,connect=30),follow_redirects=True) as client:
        with video.open("rb") as f:
            r=await client.post(ENGINE+"/api/jobs/upload",files={"file":("source.mp4",f,"video/mp4")})
    if r.status_code not in (200,202): raise RuntimeError(f"WenClips recusou o arquivo (HTTP {r.status_code}): {r.text[:700]}")
    return r.json()

async def run_job(jid:str,url:str):
    folder=JOB_DIR/jid; folder.mkdir(parents=True,exist_ok=True); video=folder/"source.mp4"
    async with worker:
        try:
            await update(jid,status="downloading",progress=10,message="Abrindo NoAdsDL e preparando o download…")
            await download_from_noadsdl(url,video)
            await update(jid,status="uploading",progress=65,message=f"Vídeo baixado ({round(video.stat().st_size/1048576,1)} MB). Enviando ao WenClips…")
            result=await send_to_engine(video)
            await update(jid,status="submitted",progress=100,message="Vídeo enviado ao WenClips. O processamento começou.",wenclips_job=result,original_deleted=True)
        except Exception as e:
            await update(jid,status="failed",progress=100,message="A automação não foi concluída.",error=str(e)[-1500:])
        finally: shutil.rmtree(folder,ignore_errors=True)

@app.get("/",include_in_schema=False)
def home(): return FileResponse(Path(__file__).parent/"static"/"index.html",media_type="text/html")
@app.get("/health")
def health(): return {"ok":True,"service":APP,"version":"0.2.2","wenclips_engine":ENGINE}
@app.post("/api/jobs",status_code=202)
async def create_job(payload:Start):
    url=valid_url(payload.url); jid=uuid.uuid4().hex
    async with lock: jobs[jid]={"id":jid,"status":"queued","progress":0,"message":"Tarefa adicionada à fila."}
    asyncio.create_task(run_job(jid,url))
    return {"id":jid,"status":"queued","status_url":f"/api/jobs/{jid}"}
@app.get("/api/jobs/{jid}")
async def get_job(jid:str):
    async with lock:
        job=jobs.get(jid)
        if not job: raise HTTPException(404,"Tarefa não encontrada. O serviço pode ter reiniciado.")
        return dict(job)
