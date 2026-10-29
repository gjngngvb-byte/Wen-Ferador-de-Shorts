from __future__ import annotations

import asyncio
import os
import re
import shutil
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

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

app = FastAPI(title=APP, version="0.3.1")
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


async def add_trace(jid: str, message: str) -> None:
    async with lock:
        if jid in jobs:
            events = jobs[jid].setdefault("trace", [])
            events.append(message[:700])
            jobs[jid]["trace"] = events[-100:]


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


async def click_named(page, patterns, trace=None) -> None:
    attempted = []
    for pattern in patterns:
        for role in ("button", "link"):
            item = page.get_by_role(role, name=pattern).first
            try:
                if not await item.count() or not await item.is_visible() or not await item.is_enabled():
                    continue
                label = (await item.inner_text()).strip().replace("\\n", " ")[:180]
                if any(label == old_label for old_label in attempted):
                    continue
                attempted.append(label)
                if trace:
                    await trace(f"CLIQUE ÚNICO: {role} «{label or str(pattern)[:100]}». Executando clique…")
                try:
                    await item.click(timeout=5000)
                    click_method = "Playwright normal"
                except Exception as normal_exc:
                    if trace:
                        await trace(f"CLIQUE NORMAL FALHOU; tentando clique forçado: {type(normal_exc).__name__}: {str(normal_exc)[:160]}")
                    try:
                        await item.click(timeout=5000, force=True)
                        click_method = "Playwright force=True"
                    except Exception as force_exc:
                        if trace:
                            await trace(f"CLIQUE FORÇADO FALHOU; tentando evento DOM: {type(force_exc).__name__}: {str(force_exc)[:160]}")
                        await item.evaluate("(e) => e.click()")
                        click_method = "evento DOM element.click()"
                if trace:
                    await trace(f"CLIQUE EXECUTADO via {click_method}; botão «{label or str(pattern)[:100]}».")
                # Dispara também o evento DOM e registra a estrutura real do botão.
                try:
                    details = await item.evaluate("""e => ({
                        html:e.outerHTML.slice(0,1200), disabled:e.disabled,
                        ariaDisabled:e.getAttribute('aria-disabled'),
                        parent:e.parentElement ? e.parentElement.outerHTML.slice(0,1600) : null
                    })""")
                    if trace:
                        await trace("ESTRUTURA DO BOTÃO APÓS UM ÚNICO CLIQUE: " + repr(details))
                        await trace("Não foi disparado clique DOM adicional para evitar reiniciar/cancelar a extração.")
                except Exception as dom_exc:
                    if trace:
                        await trace(f"EVENTO DOM click() falhou: {type(dom_exc).__name__}: {str(dom_exc)[:160]}")
                await page.wait_for_timeout(2500)
                body_after = (await page.locator("body").inner_text())[:900]
                if trace:
                    await trace(f"RESULTADO DO CLIQUE: «{label or str(pattern)[:100]}»; URL={page.url}; texto visível={body_after!r}")
                return
            except Exception as exc:
                if trace:
                    await trace(f"FALHA AO CLICAR em «{str(pattern)[:100]}»: {type(exc).__name__}: {str(exc)[:180]}")
    raise RuntimeError("Nenhum botão de extração pôde ser acionado. Tentativas: " + repr(attempted))


async def click_largest_video_option(page, trace=None):
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

    if trace:
        await trace("OPÇÕES DE RESOLUÇÃO ENCONTRADAS: " + (", ".join(repr(x[1]) for x in candidates) if candidates else "nenhuma opção de resolução visível/enabled"))
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

    # Prioriza 1080p MP4 mesmo quando o botão inclui tamanho, ícone ou texto extra.
    option, description = candidates[-1]
    preferred = None
    for candidate in candidates:
        normalized = re.sub(r"\s+", " ", candidate[1]).strip()
        if re.search(r"(?<!\d)1080p\b", normalized, re.I) and re.search(r"\bMP4\b", normalized, re.I):
            preferred = candidate
            # Se o texto for exatamente "1080p MP4", é a melhor correspondência.
            if re.fullmatch(r"1080p\s*MP4", normalized, re.I):
                break
    if preferred:
        option, description = preferred
    if trace:
        await trace(f"SELEÇÃO: clicarei em «{description}» (prioridade: 1080p MP4 quando disponível).")
    context = page.context
    popup_pages = []
    context.on("page", lambda new_page: popup_pages.append(new_page))

    # Algumas opções já iniciam o download; outras apenas selecionam a resolução.
    try:
        # O NoAdsDL pode levar vários segundos processando a opção antes de iniciar o download.
        async with page.expect_download(timeout=45000) as download_info:
            await option.click(timeout=15000)
        download = await download_info.value
        if trace:
            await trace(f"APÓS CLIQUE em «{description}»: o navegador iniciou download diretamente: {download.suggested_filename}")
        return download, description
    except PlaywrightTimeoutError:
        if trace:
            await trace(f"APÓS CLIQUE em «{description}»: nenhum evento de download em 45 s; vou procurar botão Download ou janela aberta.")

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
                        button_label = (await button.inner_text()).strip().replace("\\n", " ")[:120]
                        if trace:
                            await trace(f"BOTÃO ENCONTRADO: {role} «{button_label or str(pattern)}» em {target.url}. Vou clicar.")
                        async with target.expect_download(timeout=45000) as download_info:
                            await button.click(timeout=15000)
                        download = await download_info.value
                        if trace:
                            await trace(f"APÓS CLIQUE em «{button_label or str(pattern)}»: download iniciado: {download.suggested_filename}")
                        return download, description
                    except PlaywrightTimeoutError as exc:
                        last_error = exc
                        if trace:
                            await trace(f"APÓS CLIQUE em botão Download: nenhum evento de download após 45 s ({type(exc).__name__}: {str(exc)[:160]}).")
                        await target.wait_for_timeout(1200)
                except Exception as exc:
                    last_error = exc

    # Some sites turn the selected format into a direct download only after the page updates.
    try:
        async with page.expect_download(timeout=12000) as download_info:
            if trace:
                await trace(f"RETESTE: clicando novamente em «{description}».")
            await option.click(timeout=5000)
        download = await download_info.value
        if trace:
            await trace(f"APÓS SEGUNDO CLIQUE em «{description}»: download iniciado: {download.suggested_filename}")
        return download, description
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


async def download_from_noadsdl(url: str, target: Path, jid: str) -> None:
    async def trace(message: str) -> None:
        await add_trace(jid, message)
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
        def record_diagnostic(message: str) -> None:
            browser_diagnostics.append(message[:360])
            del browser_diagnostics[:-30]
        def record_failed_request(req) -> None:
            request_url = req.url.lower()
            # Analytics/telemetry cancellations are common and obscure the real extraction failure.
            if any(host in request_url for host in ("google-analytics.com", "/api/analytics/", "/api/future-updates/", "/cdn-cgi/rum")):
                return
            record_diagnostic(f"requestfailed: {req.method} {req.url[:220]} :: {(req.failure or '')[:100]}")
        def record_bad_response(res) -> None:
            if res.status >= 400 or ("noadsdl.com/api/" in res.url.lower() and not any(x in res.url.lower() for x in ("/api/analytics/", "/api/future-updates/"))):
                record_diagnostic(f"HTTP {res.status}: {res.request.method} {res.url[:240]}")
        page.on("console", lambda msg: record_diagnostic(f"console[{msg.type}]: {msg.text[:240]}") if msg.type == "error" else None)
        page.on("pageerror", lambda exc: record_diagnostic(f"pageerror: {str(exc)[:240]}"))
        page.on("requestfailed", record_failed_request)
        page.on("response", record_bad_response)
        page.on("request", lambda req: record_diagnostic(f"request: {req.method} {req.url[:240]}") if ("noadsdl.com/api/" in req.url.lower() or req.method.upper() not in ("GET", "HEAD")) and not any(x in req.url.lower() for x in ("/api/analytics/", "/api/future-updates/", "google-analytics.com", "/cdn-cgi/rum")) else None)
        try:
            # Abre a página limpa. Passar ?url= faz o próprio site iniciar uma
            # leitura automática e deixar "Reading video info…" ativo antes do clique,
            # o que pode conflitar com o fluxo manual de Extract Formats.
            direct_url = "https://noadsdl.com/youtube-video-downloader-online"
            await trace(f"ABRINDO NoAdsDL SEM AUTOINÍCIO: {direct_url}")
            await page.goto(direct_url, wait_until="domcontentloaded", timeout=timeout)
            await page.wait_for_timeout(1800)
            await trace(f"PÁGINA ABERTA: URL={page.url}; título={(await page.title())[:160]}")
            try:
                controls = await page.locator('button, a, [role="button"], input[type="submit"]').evaluate_all("""els => els.filter(e => {
                    const s=getComputedStyle(e), r=e.getBoundingClientRect();
                    return s.visibility!=='hidden' && s.display!=='none' && r.width>0 && r.height>0;
                }).map(e => ({tag:e.tagName.toLowerCase(),text:(e.innerText||e.value||e.getAttribute('aria-label')||e.title||'').trim().replace(/\\s+/g,' ').slice(0,140),disabled:!!e.disabled})).filter(x=>x.text).slice(0,80)""")
                await trace("BOTÕES/LINKS VISÍVEIS NA ABERTURA: " + repr(controls))
            except Exception as exc:
                await trace(f"Não consegui listar os controles iniciais: {type(exc).__name__}: {str(exc)[:160]}")

            # Se a rota não iniciar sozinha, aciona o botão da própria ferramenta.
            initial_body = ""
            try:
                initial_body = (await page.locator("body").inner_text())[:2500]
            except Exception:
                pass
            already_processing = bool(re.search(
                r"reading video info|extracting formats|please wait", initial_body, re.I
            ))
            initial_formats = await page.locator(
                'a, button, [role="button"], input[type="radio"], label'
            ).evaluate_all("""els => els.some(e => {
                const t = [e.innerText, e.value, e.getAttribute('aria-label'), e.title].filter(Boolean).join(' ');
                return /(?:144|240|360|480|540|576|720|1080|1440|2160|4320)\\s*p|\\b(?:4k|8k)\\b/i.test(t)
                  && t.length < 90 && !/[?]/.test(t)
                  && !/why|faq|longer|audio|mp3|learn more|support/i.test(t);
            })""")
            await trace("ESTADO INICIAL: " + repr(initial_body[:900]))
            # A rota de destino nem sempre preenche o campo automaticamente pelo parâmetro ?url=.
            # Preenchemos o campo real antes de clicar, evitando que o site processe um valor vazio.
            try:
                url_input = page.locator('input[type="url"], input[placeholder*="youtube" i], input[aria-label*="youtube" i]').first
                if await url_input.count() and await url_input.is_visible() and await url_input.is_enabled():
                    current_value = (await url_input.input_value()).strip()
                    await url_input.fill(url, timeout=5000)
                    # Dispara explicitamente input/change para páginas que não reagem
                    # apenas à atribuição do valor no campo.
                    await url_input.evaluate("""e => {
                        e.dispatchEvent(new Event('input', {bubbles:true}));
                        e.dispatchEvent(new Event('change', {bubbles:true}));
                    }""")
                    await trace(f"CAMPO DE URL PREENCHIDO E EVENTOS DISPARADOS: antes={current_value[:120]!r}; depois={(await url_input.input_value())[:160]!r}")
                else:
                    await trace("CAMPO DE URL ESPECÍFICO NÃO ENCONTRADO; verificando inputs visíveis.")
                    visible_inputs = await page.locator("input").evaluate_all("""els => els.filter(e => {const r=e.getBoundingClientRect(),s=getComputedStyle(e);return s.display!=='none'&&s.visibility!=='hidden'&&r.width>0&&r.height>0;}).map(e=>({type:e.type,placeholder:e.placeholder,aria:e.getAttribute('aria-label'),value:e.value})).slice(0,15)""")
                    await trace("INPUTS VISÍVEIS: " + repr(visible_inputs))
            except Exception as input_exc:
                await trace(f"FALHA AO PREENCHER LINK: {type(input_exc).__name__}: {str(input_exc)[:180]}")
            if not initial_formats:
                await trace("Não há botão de formato visível ainda; procurando botão para extrair/mostrar formatos.")
                try:
                    # Preferir o ID estável do botão real do NoAdsDL.
                    extract_button = page.locator("#v3ExtractBtn")
                    if await extract_button.count() and await extract_button.is_visible() and await extract_button.is_enabled():
                        label = (await extract_button.inner_text()).strip().replace("\\n", " ")[:120]
                        await trace(f"CLIQUE ÚNICO DIRETO NO ID #v3ExtractBtn: «{label}».")
                        try:
                            await extract_button.click(timeout=5000)
                            await trace("CLIQUE NORMAL EM #v3ExtractBtn concluído.")
                        except Exception as normal_exc:
                            await trace(f"CLIQUE NORMAL NO ID FALHOU: {type(normal_exc).__name__}: {str(normal_exc)[:160]}; tentando force.")
                            await extract_button.click(timeout=5000, force=True)
                            await trace("CLIQUE FORÇADO EM #v3ExtractBtn concluído.")
                        await page.wait_for_timeout(1000)
                        await trace("APÓS CLIQUE DE EXTRAÇÃO: " + repr((await page.locator("body").inner_text())[:700]))
                    else:
                        await click_named(page, [
                            re.compile(r"show mp4\\s*&\\s*mp3 formats", re.I),
                            re.compile(r"extract formats", re.I),
                            re.compile(r"show formats", re.I),
                            re.compile(r"continue to available formats", re.I),
                            re.compile(r"available formats|formatos disponíveis", re.I),
                        ], trace=trace)
                except RuntimeError as click_error:
                    await trace(f"NENHUM BOTÃO DE EXTRAÇÃO CLICADO: {click_error}")
                    # A página pode já ter começado a processar o parâmetro url sem mostrar o botão.
                    pass
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=20000)
            except PlaywrightTimeoutError:
                pass

            # Aguarda os formatos reais aparecerem. O NoAdsDL pode manter o texto
            # "Reading video info…" mesmo depois de renderizar os botões.
            await trace("AGUARDANDO até 75 s por opções reais de vídeo MP4.")
            deadline = asyncio.get_running_loop().time() + 75
            last_body = ""
            while asyncio.get_running_loop().time() < deadline:
                try:
                    last_body = (await page.locator("body").inner_text())[:3000]
                except Exception:
                    last_body = ""
                found_format = await page.locator(
                    'a, button, [role="button"], input[type="radio"], label'
                ).evaluate_all("""els => els.some(e => {
                    const t = [e.innerText, e.value, e.getAttribute('aria-label'), e.title]
                      .filter(Boolean).join(' ').replace(/\\s+/g, ' ').trim();
                    const visible = !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length);
                    const enabled = !e.disabled && e.getAttribute('aria-disabled') !== 'true';
                    return visible && enabled
                      && /(?:144|240|360|480|540|576|720|1080|1440|2160|4320)\\s*p/i.test(t)
                      && /\\bMP4\\b/i.test(t)
                      && t.length < 120 && !/[?]/.test(t)
                      && !/why|faq|longer|audio|mp3|learn more|support/i.test(t);
                })""")
                if found_format:
                    try:
                        formats = await page.locator('a, button, [role="button"], input[type="radio"], label').evaluate_all("""els => els.filter(e => {
                            const t=[e.innerText,e.value,e.getAttribute('aria-label'),e.title].filter(Boolean).join(' ').replace(/\\s+/g,' ').trim();
                            const r=e.getBoundingClientRect(),s=getComputedStyle(e);
                            return s.visibility!=='hidden' && s.display!=='none' && r.width>0 && r.height>0 && !e.disabled && /(?:144|240|360|480|540|576|720|1080|1440|2160|4320)\\s*p/i.test(t) && /\\bMP4\\b/i.test(t) && t.length<120 && !/[?]/.test(t);
                        }).map(e=>({tag:e.tagName.toLowerCase(),text:[e.innerText,e.value,e.getAttribute('aria-label'),e.title].filter(Boolean).join(' ').replace(/\\s+/g,' ').trim().slice(0,120)})).slice(0,80)""")
                        await trace("FORMATOS MP4 ENCONTRADOS: " + repr(formats))
                    except Exception as exc:
                        await trace(f"Detectei opção MP4, mas falhou ao listar: {type(exc).__name__}: {str(exc)[:160]}")
                    break
                await page.wait_for_timeout(1000)
            else:
                await trace("RESULTADO DA ESPERA: nenhum botão MP4 elegível apareceu em 75 s.")
                diagnostic_tail = " | Diagnóstico do navegador: " + repr(browser_diagnostics[-12:])
                raise RuntimeError(
                    "O NoAdsDL não exibiu um botão de vídeo MP4 em 75 segundos. "
                    "Último texto visível: " + repr(last_body[:900]) + diagnostic_tail
                )
            download, _description = await click_largest_video_option(page, trace=trace)
            await trace(f"DOWNLOAD RECEBIDO PELO PLAYWRIGHT: nome={download.suggested_filename!r}; salvando arquivo temporário.")
            await download.save_as(str(target))
            await trace(f"ARQUIVO SALVO: {target.stat().st_size} bytes.")
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
            await download_from_noadsdl(url, video, jid)
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
    return {"ok": True, "service": APP, "version": "0.3.1", "wenclips_engine": ENGINE}


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
