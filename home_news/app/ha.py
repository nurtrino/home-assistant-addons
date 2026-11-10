"""Home Assistant bridge: publish sensors, send notifications, and watch the room mode."""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Awaitable, Callable

import aiohttp

log = logging.getLogger("ha")


class HomeAssistant:
    def __init__(self, base_url: str, token: str):
        self.base = base_url.rstrip("/")
        self.headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        self.enabled = bool(token)

    async def set_state(self, session: aiohttp.ClientSession, entity_id: str, state: str, attributes: dict) -> bool:
        if not self.enabled:
            return False
        url = f"{self.base}/api/states/{entity_id}"
        try:
            async with session.post(url, headers=self.headers, data=json.dumps({"state": state, "attributes": attributes}),
                                    timeout=aiohttp.ClientTimeout(total=20)) as r:
                if r.status not in (200, 201):
                    log.warning("set_state %s -> HTTP %s %s", entity_id, r.status, (await r.text())[:200])
                    return False
                return True
        except Exception as e:
            log.warning("set_state %s failed: %s", entity_id, e)
            return False

    async def notify(self, session: aiohttp.ClientSession, target: str, title: str, message: str) -> None:
        if not (self.enabled and target):
            return
        url = f"{self.base}/api/services/notify/{target}"
        try:
            async with session.post(url, headers=self.headers, data=json.dumps({"title": title, "message": message}),
                                    timeout=aiohttp.ClientTimeout(total=20)) as r:
                if r.status != 200:
                    log.warning("notify -> HTTP %s", r.status)
        except Exception as e:
            log.warning("notify failed: %s", e)

    async def watch_state(self, entity_id: str, on_change: Callable[[str, str], Awaitable[None]]) -> None:
        """Reconnecting WebSocket subscription to state changes of one entity."""
        if not (self.enabled and entity_id):
            return
        ws_url = self.base.replace("http", "ws", 1) + "/api/websocket"
        token = self.headers["Authorization"].split(" ", 1)[1]
        while True:
            try:
                async with aiohttp.ClientSession() as session, session.ws_connect(ws_url, heartbeat=30) as ws:
                    await ws.receive_json()
                    await ws.send_json({"type": "auth", "access_token": token})
                    if (await ws.receive_json()).get("type") != "auth_ok":
                        log.error("websocket auth failed")
                        return
                    await ws.send_json({"id": 1, "type": "subscribe_trigger",
                                        "trigger": {"platform": "state", "entity_id": entity_id}})
                    log.info("watching %s", entity_id)
                    async for msg in ws:
                        if msg.type != aiohttp.WSMsgType.TEXT:
                            continue
                        data = json.loads(msg.data)
                        if data.get("type") != "event":
                            continue
                        trig = (data.get("event") or {}).get("variables", {}).get("trigger", {})
                        old = (trig.get("from_state") or {}).get("state", "")
                        new = (trig.get("to_state") or {}).get("state", "")
                        if old != new:
                            await on_change(old, new)
            except Exception as e:
                log.warning("websocket dropped (%s); reconnecting in 30 s", e)
            await asyncio.sleep(30)
