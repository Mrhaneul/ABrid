"""End-to-end check against a dev-mode server (BRIDGE_DEV_TOKENS=tokH:Mrhaneul,tokK:kip,tokX:mallory).

  python tests/test_bridge.py http://127.0.0.1:8765
"""
import asyncio
import sys

import httpx
from fastmcp import Client
from fastmcp.client.auth import BearerAuth

URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8765"


def data(r):
    return r.structured_content if r.structured_content is not None else r.data


async def main():
    h = Client(f"{URL}/mcp", auth=BearerAuth("tokH"))
    k = Client(f"{URL}/mcp", auth=BearerAuth("tokK"))
    x = Client(f"{URL}/mcp", auth=BearerAuth("tokX"))
    async with h, k:
        tools = {t.name for t in await h.list_tools()}
        assert {"whoami", "list_threads", "create_thread", "read_thread", "post_message", "wait_for_messages"} <= tools, tools
        print("whoami:", data(await h.call_tool("whoami")))
        tid = data(await h.call_tool("create_thread", {"title": "test thread", "first_message": "hello from H"}))["thread_id"]

        # K waits; H posts while K waits: K must wake up with H's message, never its own
        async def k_wait():
            return data(await k.call_tool("wait_for_messages", {"after_id": 1, "thread_id": tid, "timeout_seconds": 20}))
        waiter = asyncio.create_task(k_wait())
        await asyncio.sleep(1)
        await h.call_tool("post_message", {"thread_id": tid, "body": "second from H"})
        got = await waiter
        assert [m["body"] for m in got["messages"]] == ["second from H"], got
        print("long-poll woke with:", got["messages"][0]["label"], got["messages"][0]["body"])

        await k.call_tool("post_message", {"thread_id": tid, "body": "reply from K"})
        own = data(await k.call_tool("wait_for_messages", {"after_id": got["last_id"], "thread_id": tid, "timeout_seconds": 2}))
        assert own["timed_out"] and not own["messages"], own
        print("own messages filtered: ok")

        # web: person logs in, reads, posts, pauses
        async with httpx.AsyncClient(base_url=URL, follow_redirects=False) as web:
            r = await web.get("/api/threads")
            assert r.status_code == 401
            r = await web.get("/web/login", params={"as": "Mrhaneul"})
            web.cookies = r.cookies
            msgs = (await web.get(f"/api/threads/{tid}/messages")).json()["messages"]
            assert len(msgs) == 3, msgs
            r = await web.post(f"/api/threads/{tid}/messages", json={"body": "person here"})
            assert r.status_code == 403, "POST without x-bridge header must fail"
            hdr = {"x-bridge": "1"}
            assert (await web.post(f"/api/threads/{tid}/messages", json={"body": "person here"}, headers=hdr)).status_code == 200
            assert (await web.post(f"/api/threads/{tid}/pause", json={"paused": True}, headers=hdr)).status_code == 200
            r = await web.get("/web/login", params={"as": "mallory"})
            assert r.status_code == 403
        try:
            await k.call_tool("post_message", {"thread_id": tid, "body": "should fail"})
            raise AssertionError("post to paused thread succeeded")
        except AssertionError:
            raise
        except Exception as e:
            print("paused thread rejects agent:", str(e)[:80])
        last = data(await h.call_tool("read_thread", {"thread_id": tid}))
        print("kinds:", [m["kind"] for m in last["messages"]])
        assert last["thread"]["paused"]

    async with x:
        try:
            await x.call_tool("whoami")
            raise AssertionError("not-allowed user got in")
        except AssertionError:
            raise
        except Exception as e:
            print("not-allowed user rejected:", str(e)[:80])
    async with httpx.AsyncClient() as c:
        r = await c.post(f"{URL}/mcp", json={})
        print("no token:", r.status_code, r.headers.get("www-authenticate", "")[:80])
        assert r.status_code == 401
    print("ALL OK")


asyncio.run(main())
