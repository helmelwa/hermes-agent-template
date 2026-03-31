"""
Hermes Agent Railway wrapper.

Runs a thin aiohttp HTTP server on $PORT that:
  - Always responds to /health (Railway health check)
  - Proxies /v1/* to the internal Hermes API server (OpenAI-compatible)
  - Manages the Hermes gateway process as a subprocess
"""

import asyncio
import os
import sys
from pathlib import Path

import aiohttp
from aiohttp import web

# ── Config ────────────────────────────────────────────────────────────────────
HERMES_HOME = Path(os.environ.get("HERMES_HOME", "/data/.hermes"))
HERMES_INTERNAL_PORT = 8642  # fixed internal port for hermes API server
PORT = int(os.environ.get("PORT", 8080))

gateway_process: asyncio.subprocess.Process | None = None


# ── Setup ─────────────────────────────────────────────────────────────────────
def setup_hermes_config() -> None:
    HERMES_HOME.mkdir(parents=True, exist_ok=True)

    # Build .env from Railway env vars
    pairs = {
        "LLM_MODEL":               os.environ.get("LLM_MODEL", "openai/gpt-4o-mini"),
        "OPENROUTER_API_KEY":      os.environ.get("OPENROUTER_API_KEY", ""),
        "OPENAI_API_KEY":          os.environ.get("OPENAI_API_KEY", ""),
        "ANTHROPIC_API_KEY":       os.environ.get("ANTHROPIC_API_KEY", ""),
        "EXA_API_KEY":             os.environ.get("EXA_API_KEY", ""),
        "FIRECRAWL_API_KEY":       os.environ.get("FIRECRAWL_API_KEY", ""),
        "PARALLEL_API_KEY":        os.environ.get("PARALLEL_API_KEY", ""),
        "FAL_KEY":                 os.environ.get("FAL_KEY", ""),
        "HONCHO_API_KEY":          os.environ.get("HONCHO_API_KEY", ""),
        "GITHUB_TOKEN":            os.environ.get("GITHUB_TOKEN", ""),
        "BROWSERBASE_API_KEY":     os.environ.get("BROWSERBASE_API_KEY", ""),
        "BROWSERBASE_PROJECT_ID":  os.environ.get("BROWSERBASE_PROJECT_ID", ""),
        "TERMINAL_ENV":            os.environ.get("TERMINAL_ENV", "local"),
        "TERMINAL_TIMEOUT":        os.environ.get("TERMINAL_TIMEOUT", "60"),
        "GATEWAY_ALLOW_ALL_USERS": os.environ.get("GATEWAY_ALLOW_ALL_USERS", "true"),
        # Internal API server binds to a fixed port; our wrapper proxies it
        "API_SERVER_HOST":         "127.0.0.1",
        "API_SERVER_PORT":         str(HERMES_INTERNAL_PORT),
    }

    with open(HERMES_HOME / ".env", "w") as f:
        for k, v in pairs.items():
            if v:
                f.write(f"{k}={v}\n")

    model = pairs["LLM_MODEL"]
    terminal = pairs["TERMINAL_ENV"]
    config_yaml = f"""\
model:
  default: "{model}"
  provider: "auto"

terminal:
  backend: "{terminal}"
  timeout: 60
  cwd: "/tmp"

agent:
  max_iterations: 50
  reasoning_effort: "medium"

platforms:
  api_server:
    enabled: true
    host: "127.0.0.1"
    port: {HERMES_INTERNAL_PORT}
    cors_origins:
      - "*"

data_dir: "{HERMES_HOME}"
"""
    with open(HERMES_HOME / "config.yaml", "w") as f:
        f.write(config_yaml)

    print(f"[server] Config written to {HERMES_HOME}", flush=True)


# ── Gateway subprocess ────────────────────────────────────────────────────────
async def start_gateway() -> None:
    global gateway_process
    print("[server] Starting Hermes gateway...", flush=True)
    gateway_process = await asyncio.create_subprocess_exec(
        sys.executable, "/opt/hermes/cli.py", "--gateway",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        cwd="/opt/hermes",
    )
    asyncio.create_task(_stream_logs(gateway_process))
    print(f"[server] Gateway started (pid={gateway_process.pid})", flush=True)


async def _stream_logs(proc: asyncio.subprocess.Process) -> None:
    assert proc.stdout
    async for line in proc.stdout:
        print(f"[hermes] {line.decode().rstrip()}", flush=True)
    rc = await proc.wait()
    print(f"[server] Gateway exited with code {rc}", flush=True)


# ── HTTP handlers ─────────────────────────────────────────────────────────────
async def handle_health(request: web.Request) -> web.Response:
    running = gateway_process is not None and gateway_process.returncode is None
    return web.json_response({
        "status": "ok",
        "gateway": "running" if running else "starting",
    })


async def handle_proxy(request: web.Request) -> web.Response:
    path = request.match_info.get("path", "")
    target_url = f"http://127.0.0.1:{HERMES_INTERNAL_PORT}/{path}"
    if request.query_string:
        target_url += f"?{request.query_string}"

    # Strip headers that would confuse the upstream
    skip = {"host", "content-length", "transfer-encoding", "connection"}
    headers = {k: v for k, v in request.headers.items() if k.lower() not in skip}

    async with aiohttp.ClientSession() as session:
        try:
            upstream = await session.request(
                method=request.method,
                url=target_url,
                headers=headers,
                data=await request.read(),
                allow_redirects=False,
            )
            body = await upstream.read()
            resp_headers = {
                k: v for k, v in upstream.headers.items()
                if k.lower() not in {"transfer-encoding", "content-encoding", "connection"}
            }
            return web.Response(status=upstream.status, headers=resp_headers, body=body)
        except aiohttp.ClientConnectorError:
            return web.json_response(
                {"error": "Hermes gateway not ready yet — please retry in a moment"},
                status=503,
            )


# ── App lifecycle ─────────────────────────────────────────────────────────────
async def on_startup(app: web.Application) -> None:
    setup_hermes_config()
    await start_gateway()


async def on_cleanup(app: web.Application) -> None:
    if gateway_process and gateway_process.returncode is None:
        print("[server] Shutting down gateway...", flush=True)
        gateway_process.terminate()
        try:
            await asyncio.wait_for(gateway_process.wait(), timeout=10)
        except asyncio.TimeoutError:
            gateway_process.kill()


def main() -> None:
    app = web.Application()
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)

    app.router.add_get("/health", handle_health)
    app.router.add_route("*", "/{path:.*}", handle_proxy)

    print(f"[server] Listening on 0.0.0.0:{PORT}", flush=True)
    web.run_app(app, host="0.0.0.0", port=PORT, access_log=None)


if __name__ == "__main__":
    main()
