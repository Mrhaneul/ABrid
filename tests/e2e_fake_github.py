"""
Full login flows against a fake GitHub: no network, no real GitHub OAuth app needed.

GitHub is replaced in two places:
  - browser side (github.com/login/oauth/authorize): the browser resolves github.com to a local HTTPS
    server; it applies GitHub's default redirect_uri rule (exact match with a registered callback URL)
  - server side (token exchange, /user, /user/repos): httpx and httpx2 requests to github.com and
    api.github.com are answered in-process

Checks:
  1. agent login through FastMCP's OAuth proxy (dynamic registration, consent page, GitHub, token
     exchange), then tools work under the GitHub login; a GitHub user not on the allow list gets in
     through OAuth but every tool refuses
  2. web login for people: allowed user sees the board, not-allowed user and a forged state are refused
  3. Codex CLI: `codex mcp login` completes against the bridge (separate CODEX_HOME, user config untouched)
  4. Claude Code: `claude mcp list` sees the server as needing authentication (discovery works)

  python tests/e2e_fake_github.py
"""
import asyncio
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit

PORT = 8790
BASE = f"http://localhost:{PORT}"
CALLBACK = f"{BASE}/auth/callback"   # first registered callback URL (agents)
CALLBACKS = [CALLBACK, f"{BASE}/auth/callback/web"]  # as in README step 1
TMP = Path(tempfile.mkdtemp(prefix="bridge-e2e-"))
os.environ.update(BRIDGE_DATA=str(TMP / "data"), BRIDGE_SECRET=secrets.token_hex(32),
                  BRIDGE_ALLOWED_USERS="Mrhaneul,kip", BRIDGE_PUBLIC_URL=BASE,
                  GITHUB_CLIENT_ID="fake-client-id", GITHUB_CLIENT_SECRET="fake-client-secret")
os.environ.pop("BRIDGE_DEV_TOKENS", None)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402
import httpx2  # noqa: E402
import uvicorn  # noqa: E402
from playwright.async_api import async_playwright  # noqa: E402

# ------------------------------------------------------------------ fake GitHub

github = {"next_login": "Mrhaneul", "codes": {}, "tokens": {}, "log": []}


def github_authorize(url):
    """Browser side. Returns (status, headers, body) like github.com would."""
    q = dict(parse_qsl(urlsplit(url).query))
    github["log"].append(("authorize", q.get("redirect_uri")))
    if q.get("client_id") != "fake-client-id":
        return 404, {}, "unknown client_id"
    ru = q.get("redirect_uri", CALLBACK)
    if ru not in CALLBACKS:
        return 400, {}, f"redirect_uri mismatch: {ru}"
    r = urlsplit(ru)
    code = secrets.token_hex(8)
    github["codes"][code] = (github["next_login"], ru)
    sep = "&" if r.query else "?"
    return 302, {"Location": f"{ru}{sep}{urlencode({'code': code, 'state': q.get('state', '')})}"}, ""


def github_api(mod, request):
    """Server side: token exchange and the user API."""
    url, path = request.url, request.url.path
    github["log"].append((request.method, f"{url.host}{path}"))
    if url.host == "github.com" and path == "/login/oauth/access_token":
        form = dict(parse_qsl(request.content.decode()))
        auth = request.headers.get("authorization", "")
        if auth.startswith("Basic "):
            import base64
            cid, _, csec = base64.b64decode(auth[6:]).decode().partition(":")
            form.setdefault("client_id", cid)
            form.setdefault("client_secret", csec)
        if (form.get("client_id"), form.get("client_secret")) != ("fake-client-id", "fake-client-secret"):
            return mod.Response(200, json={"error": "incorrect_client_credentials"}, request=request)
        entry = github["codes"].pop(form.get("code", ""), None)
        if entry is None or ("redirect_uri" in form and form["redirect_uri"] != entry[1]):
            return mod.Response(200, json={"error": "bad_verification_code"}, request=request)
        token = "gho_" + secrets.token_hex(12)
        github["tokens"][token] = entry[0]
        return mod.Response(200, json={"access_token": token, "token_type": "bearer", "scope": "read:user"},
                            request=request)
    login = github["tokens"].get(request.headers.get("authorization", "").removeprefix("Bearer ").removeprefix("token "))
    if url.host == "api.github.com" and login is None:
        return mod.Response(401, json={"message": "Bad credentials"}, request=request)
    if url.host == "api.github.com" and path == "/user":
        return mod.Response(200, json={"login": login, "id": abs(hash(login)) % 10**8, "name": login,
                                       "email": None, "avatar_url": ""}, request=request)
    if url.host == "api.github.com" and path == "/user/repos":
        return mod.Response(200, json=[], headers={"X-OAuth-Scopes": "read:user"}, request=request)
    return mod.Response(404, json={"message": "Not Found"}, request=request)


for mod in (httpx, httpx2):
    def _patch(mod=mod):
        orig = mod.AsyncClient.send

        async def send(self, request, *a, **k):
            if request.url.host in ("github.com", "api.github.com"):
                return github_api(mod, request)
            return await orig(self, request, *a, **k)
        mod.AsyncClient.send = send
    _patch()

import server  # noqa: E402  (after env and patches)
from fastmcp import Client  # noqa: E402
from fastmcp.client.auth import OAuth  # noqa: E402
import fastmcp.client.auth.oauth as client_oauth  # noqa: E402

results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok)))
    print(("PASS " if ok else "FAIL ") + name + (f"  ({detail})" if detail else ""))


FAKE_GH_PORT = 8899  # the browser resolves github.com to this HTTPS server (self-signed cert)


async def fake_github_browser_app(scope, receive, send):
    from starlette.requests import Request
    from starlette.responses import Response
    req = Request(scope, receive)
    if req.url.path == "/login/oauth/authorize":
        status, headers, body = github_authorize(str(req.url))
    else:
        status, headers, body = 404, {}, "fake github: not found"
    await Response(body, status_code=status, headers=headers)(scope, receive, send)


async def launch(p):
    return await p.chromium.launch(args=[f"--host-resolver-rules=MAP github.com 127.0.0.1:{FAKE_GH_PORT}"])


async def new_ctx(browser):
    return await browser.new_context(ignore_https_errors=True)


async def complete_in_browser(ctx, url):
    """Open an authorization URL, approve the consent page if shown, follow to the end."""
    page = await ctx.new_page()
    await page.goto(url)
    if "/consent" in page.url:
        btn = page.get_by_role("button", name=re.compile("allow|approve|authorize|continue", re.I)).first
        await btn.click()
    await page.wait_for_load_state()
    final = page.url
    text = (await page.text_content("body") or "")[:200]
    await page.close()
    return final, text


async def agent_login(ctx, login):
    github["next_login"] = login
    urls = asyncio.Queue()
    client_oauth.webbrowser.open = lambda u, *a, **k: urls.put_nowait(u) or True
    client = Client(f"{BASE}/mcp", auth=OAuth(client_name="e2e agent", callback_port=None))

    async def drive():
        url = await asyncio.wait_for(urls.get(), 30)
        return await complete_in_browser(ctx, url)
    driver = asyncio.create_task(drive())
    await asyncio.wait_for(client.__aenter__(), 60)
    await driver
    return client


async def main():
    config = uvicorn.Config(server.app, host="127.0.0.1", port=PORT, log_level="warning")
    srv = uvicorn.Server(config)
    task = asyncio.create_task(srv.serve())
    cert, key = TMP / "gh.crt", TMP / "gh.key"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1", "-subj", "/CN=github.com",
                    "-keyout", str(key), "-out", str(cert)], check=True, capture_output=True)
    gh = uvicorn.Server(uvicorn.Config(fake_github_browser_app, host="127.0.0.1", port=FAKE_GH_PORT,
                                       ssl_certfile=str(cert), ssl_keyfile=str(key), log_level="warning"))
    gh_task = asyncio.create_task(gh.serve())
    while not (srv.started and gh.started):
        await asyncio.sleep(0.1)

    async with async_playwright() as p:
        browser = await launch(p)
        ctx = await new_ctx(browser)

        # 1. agent login through the OAuth proxy
        h = await agent_login(ctx, "Mrhaneul")
        who = (await h.call_tool("whoami")).structured_content
        check("agent OAuth login -> whoami is the GitHub login", who["login"] == "Mrhaneul", who["login"])
        check("authorize used the registered callback", ("authorize", CALLBACK) in github["log"])
        tid = (await h.call_tool("create_thread", {"title": "oauth thread", "first_message": "hi"})).structured_content["thread_id"]
        kctx = await new_ctx(browser)
        k = await agent_login(kctx, "kip")
        waiter = asyncio.create_task(k.call_tool("wait_for_messages", {"after_id": 0, "thread_id": tid, "timeout_seconds": 10}))
        got = (await waiter).structured_content
        check("second agent (kip) logs in and reads the thread", [m["body"] for m in got["messages"]] == ["hi"])
        r = (await k.call_tool("post_message", {"thread_id": tid, "body": "hello from kip"})).structured_content
        check("kip's agent posts", r["message_id"] > 0)

        mctx = await new_ctx(browser)
        m = await agent_login(mctx, "mallory")
        try:
            await m.call_tool("whoami")
            check("not-allowed GitHub user is refused by tools", False)
        except Exception as e:
            check("not-allowed GitHub user is refused by tools", "not allowed" in str(e), str(e)[:60])
        for c in (h, k, m):
            await c.__aexit__(None, None, None)

        # 2. web login
        wctx = await new_ctx(browser)
        page = await wctx.new_page()
        github["next_login"] = "kip"
        await page.goto(BASE + "/")
        await page.click("text=Log in with GitHub")
        await page.wait_for_selector("#who", timeout=10000)
        await page.wait_for_selector(".msg", timeout=10000)
        check("web login (allowed user) shows the board", "kip" in (await page.text_content("#who")),
              await page.text_content("#who"))
        check("web redirect_uri is the second registered callback (exact match)", ("authorize", f"{BASE}/auth/callback/web") in github["log"])
        check("web page shows the agents' messages", await page.locator(".msg").count() >= 2)
        cookies = {c["name"]: c for c in await wctx.cookies()}
        check("session cookie is HttpOnly", cookies.get("bridge_session", {}).get("httpOnly") is True)

        bctx = await new_ctx(browser)
        page = await bctx.new_page()
        github["next_login"] = "mallory"
        await page.goto(BASE + "/web/login")
        body = await page.text_content("body")
        check("web login (not-allowed user) refused", "not on this bridge's allow list" in body, body[:60])
        resp = await page.goto(BASE + "/auth/callback/web?code=x&state=forged")
        check("web callback with forged state refused", resp.status == 400)
        await browser.close()

        # 3. Codex CLI login (separate CODEX_HOME)
        codex = shutil.which("codex")
        if codex:
            home = TMP / "codex-home"
            home.mkdir()
            (home / "config.toml").write_text('mcp_oauth_credentials_store = "file"\n')
            env = dict(os.environ, CODEX_HOME=str(home), BROWSER="true", DISPLAY="")
            subprocess.run([codex, "mcp", "add", "agent-bridge", "--url", f"{BASE}/mcp"], env=env,
                           check=True, capture_output=True, timeout=60)
            github["next_login"] = "kip"
            proc = await asyncio.create_subprocess_exec(codex, "mcp", "login", "agent-bridge", env=env,
                                                        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
            out, url = [], None
            async def read_lines():
                nonlocal url
                async for line in proc.stdout:
                    s = line.decode(errors="replace")
                    out.append(s)
                    mm = re.search(r"https?://\S+/authorize\S*", s)
                    if mm and not url:
                        url = mm.group(0)
                        b2 = await launch(p)
                        c2 = await new_ctx(b2)
                        final, text = await complete_in_browser(c2, url)
                        out.append(f"[browser ended at {final}: {text[:80]}]\n")
                        await b2.close()
            try:
                await asyncio.wait_for(asyncio.gather(read_lines(), proc.wait()), 90)
            except asyncio.TimeoutError:
                proc.kill()
            ok = proc.returncode == 0 and url is not None
            check("codex mcp login completes", ok, "".join(out)[-300:].replace("\n", " | "))
            # the token Codex stored must work for tool calls
            stored = [f for f in home.rglob("*") if f.is_file() and "access_token" in f.read_text(errors="ignore")]
            token = None
            for f in stored:
                mm = re.search(r'"access_token"\s*:\s*"([^"]+)"', f.read_text(errors="ignore"))
                token = token or (mm and mm.group(1))
            if token:
                from fastmcp.client.auth import BearerAuth
                async with Client(f"{BASE}/mcp", auth=BearerAuth(token)) as cx:
                    w = (await cx.call_tool("whoami")).structured_content
                check("token stored by Codex works for tool calls", w["login"] == "kip", w["login"])
            else:
                check("token stored by Codex found", False, str([f.name for f in home.rglob("*")])[:200])
            lst = subprocess.run([codex, "mcp", "list"], env=env, capture_output=True, text=True, timeout=60)
            check("codex mcp list shows the server logged in", "OAuth" in lst.stdout and "Not logged in" not in lst.stdout,
                  lst.stdout.strip().splitlines()[-1] if lst.stdout.strip() else lst.stderr[:100])
        else:
            check("codex installed", False, "skipped")

        # 4. Claude Code discovery
        cdir = TMP / "cc"
        cdir.mkdir()
        claude = shutil.which("claude")
        if claude:
            subprocess.run([claude, "mcp", "add", "--transport", "http", "agent-bridge-e2e", f"{BASE}/mcp"],
                           cwd=cdir, capture_output=True, timeout=60)
            lst = await asyncio.create_subprocess_exec(claude, "mcp", "list", cwd=str(cdir),
                                                       stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
            text = (await asyncio.wait_for(lst.communicate(), 120))[0].decode()
            line = next((s for s in text.splitlines() if "agent-bridge-e2e" in s), text[-200:])
            check("claude mcp list: server found, asks for authentication", "auth" in line.lower(), line.strip())
            subprocess.run([claude, "mcp", "remove", "agent-bridge-e2e"], cwd=cdir, capture_output=True, timeout=60)

    srv.should_exit = gh.should_exit = True
    await task
    await gh_task
    shutil.rmtree(TMP, ignore_errors=True)
    failed = [n for n, ok in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed" + (f"; FAILED: {failed}" if failed else ""))
    sys.exit(1 if failed else 0)


asyncio.run(main())
