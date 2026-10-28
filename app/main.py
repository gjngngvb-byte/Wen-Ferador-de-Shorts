from __future__ import annotations

import asyncio
import os
import re
import shutil
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

APP = "NoAdsDL → WenClips Bridge"
JOB_DIR = Path(os.getenv("JOB_DIR", "/tmp/noadsdl-bridge"))
JOB_DIR.mkdir(parents=True, exist_ok=True)
ENGINE = os.getenv("WENCLIPS_ENGINE_URL", "https://wenclips-engine.onrender.com").rstrip("/")
jobs: dict[str, dict[str, Any]] = {}
lock = asyncio.Lock()
worker = asyncio.Lock()

app = FastAPI(title=APP, version="0.2.4")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


class Start(BaseModel):
    url: str = Field(min_length=8, max_length=2048)


def valid_url(raw: str) -> str:
    url = raw.strip()
    parsed = urlparse(url)
    allowed = {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be", "www.youtube-nocookie.com"}
    if parsed.scheme not in ("http", "https") or (parsed.hostname or "").lower() not in allowed:
        raise HTTPException(400, "Informe um link válido do YouTube.")
    return url


async def update(jid: str, **values: Any) -> None:
    async with lock:
        if jid in jobs:
            jobs[jid].update(values)


async def first_visible(locators):
    for locator in locators:
        try:
            for i in range(min(await locator.count(), 12)):
                item = locator.nth(i)
                if await item.is_visible() and await item.is_enabled():
                    return item
        except Exception:
            continue
    return None


async def click_named(page, patterns) -> None:
    for pattern in patterns:
        for role in ("button", "link"):
            try:
                item = page.get_by_role(role, name=pattern).first
                if await item.count() and await item.is_visible() and await item.is_enabled():
                    await item.click(timeout=15000)
                    return
            except Exception:
                continue
    raise RuntimeError("Não encontrei o botão 'Continue to available formats' no NoAdsDL. A interface pode ter mudado.")


async def click_largest_video_option(page):
    await page.wait_for_timeout(3000)
    resolution = re.compile(r"(?<!\d)(?:144|240|360|480|540|576|720|1080|1440|2160|4320)\s*p\b|\b(?:4k|8k)\b", re.I)
    locator = page.locator('a, button, [role="button"], input[type="radio"], label')
    candidates = []
    try:
        count = min(await locator.count(), 600)
    except Exception:
        count = 0

    for i in range(count):
        element = locator.nth(i)
        try:
            if not await element.is_visible() or not await element.is_enabled():
                continue
            tag = await element.evaluate("(e) => e.tagName.toLowerCase()")
            text = " ".join(filter(None, [
                (await element.inner_text())[:120] if tag != "input" else "",
                await element.get_attribute("value"),
                await element.get_attribute("aria-label"),
                await element.get_attribute("title"),
            ])).strip().replace("\n", " ")
            if not resolution.search(text):
                continue
            if len(text) > 90 or "?" in text or re.search(r"\b(why|faq|longer|take longer|audio|mp3|only audio|learn more|support)\b", text, re.I):
                continue
            candidates.append((element, text[:90]))

        except Exception:
            continue

    if not candidates:
        body = ""
        try:
            body = (await page.locator("body").inner_text())[:900]
        except Exception:
            pass
        raise RuntimeError(
            "Não encontrei uma opção real de formato de vídeo após clicar em 'Continue to available formats'. "
            "Texto visível na página: " + repr(body)
        )

    # Seleciona a última opção de vídeo exibida, conforme solicitado.
    option, description = candidates[-1]
    context = page.context
    popup_pages = []
    context.on("page", lambda new_page: popup_pages.append(new_page))

    # Algumas opções já iniciam o download; outras apenas selecionam a resolução.
    try:
        async with page.expect_download(timeout=8000) as download_info:
            await option.click(timeout=15000)
        return await download_info.value, description
    except PlaywrightTimeoutError:
        pass

    await page.wait_for_timeout(1500)
    targets = [p for p in popup_pages if not p.is_closed()] + [page]
    download_names = [
        re.compile(r"^download(?:\s+video)?(?:\s+mp4)?$", re.I),
        re.compile(r"^download\s+now$", re.I),
        re.compile(r"^get\s+video$", re.I),
        re.compile(r"^save\s+video$", re.I),
        re.compile(r"^download\s+file$", re.I),
    ]
    last_error = None

    for target in targets:
        for pattern in download_names:
            for role in ("button", "link"):
                try:
                    button = target.get_by_role(role, name=pattern).first
                    if not await button.count() or not await button.is_visible() or not await button.is_enabled():
                        continue
                    try:
                        async with target.expect_download(timeout=45000) as download_info:
                            await button.click(timeout=15000)
                        return await download_info.value, description
                    except PlaywrightTimeoutError as exc:
                        last_error = exc
                        await target.wait_for_timeout(1200)
                except Exception as exc:
                    last_error = exc

    # Some sites turn the selected format into a direct download only after the page updates.
    try:
        async with page.expect_download(timeout=12000) as download_info:
            await option.click(timeout=5000)
        return await download_info.value, description
    except Exception as exc:
        body = ""
        try:
            body = (await page.locator("body").inner_text())[:900]
        except Exception:
            pass
        raise RuntimeError(
            f"Encontrei a opção de vídeo {description}, mas o NoAdsDL não iniciou o download. "
            f"URL atual: {page.url}. Texto visível: {body!r}"
        ) from (last_error or exc)


async def download_from_noadsdl(url: str, target: Path) -> None:
    timeout = int(os.getenv("BROWSER_TIMEOUT_MS", "120000"))
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
        )
        context = await browser.new_context(
            accept_downloads=True,
            viewport={"width": 1365, "height": 900},
            locale="en-US",
        )
        page = await context.new_page()
        page.set_default_timeout(timeout)
        browser_diagnostics: list[str] = []
        page.on("console", lambda msg: browser_diagnostics.append(f"console[{msg.type}]: {msg.text[:180]}") if msg.type == "error" else None)
        page.on("pageerror", lambda exc: browser_diagnostics.append(f"pageerror: {str(exc)[:180]}"))
        page.on("requestfailed", lambda req: browser_diagnostics.append(f"requestfailed: {req.method} {req.url[:180]} :: {(req.failure or '')[:100]}"))
        page.on("response", lambda res: browser_diagnostics.append(f"HTTP {res.status}: {res.url[:180]}") if res.status >= 400 else None)
        try:
            await page.goto("https://noadsdl.com/", wait_until="domcontentloaded", timeout=timeout)
            await page.wait_for_timeout(1800)
            field = await first_visible([
                page.locator('input:not([type="hidden"]):not([type="submit"]):not([type="button"]):not([disabled])'),
                page.locator('textarea:not([disabled])'),
                page.locator('[contenteditable="true"]'),
                page.locator('[role="textbox"]:not([aria-readonly="true"])'),
            ])
            if not field:
                raise RuntimeError("Não encontrei um campo editável para o link no NoAdsDL.")
            editable = await field.evaluate(
                "(el) => el.isContentEditable || ['input','textarea'].includes(el.tagName.toLowerCase()) || el.getAttribute('role') === 'textbox'"
            )
            if not editable:
                raise RuntimeError("O seletor encontrou um elemento não editável; download cancelado.")
            await field.fill(url)
            await click_named(page, [
                re.compile(r"extract formats", re.I),
                re.compile(r"continue to available formats", re.I),
                re.compile(r"available formats|show formats|formatos disponíveis", re.I),
            ])
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=20000)
            except PlaywrightTimeoutError:
                pass

            # NoAdsDL may show "Reading video info…" while it resolves the source.
            deadline = asyncio.get_running_loop().time() + 75
            last_body = ""
            while asyncio.get_running_loop().time() < deadline:
                try:
                    last_body = (await page.locator("body").inner_text())[:2500]
                except Exception:
                    last_body = ""
                reading = re.search(r"reading video info|extracting formats|please wait", last_body, re.I)
                found_format = await page.locator(
                    'a, button, [role="button"], input[type="radio"], label'
                ).evaluate_all("""els => els.some(e => {
                    const t = [e.innerText, e.value, e.getAttribute('aria-label'), e.title].filter(Boolean).join(' ');
                    return /(?:144|240|360|480|540|576|720|1080|1440|2160|4320)\\s*p|\\b(?:4k|8k)\\b/i.test(t)
                      && t.length < 90 && !/[?]/.test(t)
                      && !/why|faq|longer|audio|mp3|learn more|support/i.test(t);
                })""")
                if found_format and not reading:
                    break
                await page.wait_for_timeout(1000)
            else:
                diagnostic_tail = " | Diagnóstico do navegador: " + repr(browser_diagnostics[-12:])
                raise RuntimeError(
                    "O NoAdsDL não terminou de extrair os formatos em 75 segundos. "
                    "Último texto visível: " + repr(last_body[:900]) + diagnostic_tail
                )
            download, _description = await click_largest_video_option(page)
            await download.save_as(str(target))
        finally:
            await context.close()
            await browser.close()

    if not target.is_file() or target.stat().st_size < 100000:
        raise RuntimeError("O NoAdsDL não produziu um arquivo de vídeo válido.")

    process = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", str(target),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    try:
        duration = float(stdout.decode().strip() or 0)
    except ValueError:
        duration = 0
    if process.returncode or duration < 1:
        raise RuntimeError("O arquivo baixado não parece ser um vídeo válido: " + stderr.decode(errors="replace")[-300:])


async def send_to_engine(video: Path):
    async with httpx.AsyncClient(timeout=httpx.Timeout(240, connect=30), follow_redirects=True) as client:
        with video.open("rb") as file:
            response = await client.post(
                ENGINE + "/api/jobs/upload",
                files={"file": ("source.mp4", file, "video/mp4")},
            )
    if response.status_code not in (200, 202):
        raise RuntimeError(f"WenClips recusou o arquivo (HTTP {response.status_code}): {response.text[:700]}")
    return response.json()


async def run_job(jid: str, url: str) -> None:
    folder = JOB_DIR / jid
    folder.mkdir(parents=True, exist_ok=True)
    video = folder / "source.mp4"
    async with worker:
        try:
            await update(jid, status="downloading", progress=10, message="Abrindo NoAdsDL e preparando o download…")
            await download_from_noadsdl(url, video)
            await update(
                jid, status="uploading", progress=65,
                message=f"Vídeo baixado ({round(video.stat().st_size / 1048576, 1)} MB). Enviando ao WenClips…",
            )
            result = await send_to_engine(video)
            await update(
                jid, status="submitted", progress=100,
                message="Vídeo enviado ao WenClips. O processamento começou.",
                wenclips_job=result, original_deleted=True,
            )
        except Exception as exc:
            await update(jid, status="failed", progress=100, message="A automação não foi concluída.", error=str(exc)[-1500:])
        finally:
            shutil.rmtree(folder, ignore_errors=True)


@app.get("/", include_in_schema=False)
def home():
    return FileResponse(Path(__file__).parent / "static" / "index.html", media_type="text/html")


@app.get("/health")
def health():
    return {"ok": True, "service": APP, "version": "0.2.4", "wenclips_engine": ENGINE}


@app.post("/api/jobs", status_code=202)
async def create_job(payload: Start):
    url = valid_url(payload.url)
    jid = uuid.uuid4().hex
    async with lock:
        jobs[jid] = {"id": jid, "status": "queued", "progress": 0, "message": "Tarefa adicionada à fila."}
    asyncio.create_task(run_job(jid, url))
    return {"id": jid, "status": "queued", "status_url": f"/api/jobs/{jid}"}


@app.get("/api/jobs/{jid}")
async def get_job(jid: str):
    async with lock:
        job = jobs.get(jid)
        if not job:
            raise HTTPException(404, "Tarefa não encontrada. O serviço pode ter reiniciado.")
        return dict(job)
