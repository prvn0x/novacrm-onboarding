"""`VOICE_MODE=web`: the same Vapi assistant, reached through the browser instead of the phone.

The fallback if the free voice tier cannot call an Indian number (assumption A9). It is a real
voice conversation with the same assistant, transcript and decision code; only the transport
differs. `place_call` opens a local page; the AE clicks "Answer" and talks; the page reports the
Vapi call id back, and from then on the result is read from the Vapi API exactly as for a phone
call. Needs VAPI_PUBLIC_KEY (the browser key) as well as the private key.
"""

from __future__ import annotations

import json
import os
import secrets
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from ..domain import CallResult, CallStatus
from .vapi import VapiCaller

PORT = int(os.environ.get("WEBCALL_PORT", "8765"))
# A phone stops ringing after ~45 s; an unanswered call page does the same.
RING_SECONDS = float(os.environ.get("WEBCALL_RING_SECONDS", "45"))

_PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>NovaCRM call</title>
<style>body{font:18px system-ui;margin:3rem;max-width:36rem}button{font-size:1.2rem;
padding:.8rem 1.6rem}</style></head><body>
<h2>Incoming call: NovaCRM onboarding assistant</h2>
<p>About the deal with <b>__CUSTOMER__</b>. Click to answer, then speak.</p>
<button id="answer">Answer</button> <button id="hangup" disabled>Hang up</button>
<p id="status"></p>
<script type="module">
import Vapi from "https://esm.sh/@vapi-ai/web";
const vapi = new Vapi("__PUBLIC_KEY__");
const status = (t) => document.getElementById("status").textContent = t;
document.getElementById("answer").onclick = async () => {
  status("Connecting...");
  const call = await vapi.start("__ASSISTANT__", {variableValues: __VARIABLES__});
  await fetch("/started?token=__TOKEN__&call_id=" + encodeURIComponent(call.id));
  status("Connected. Talk to the assistant.");
  document.getElementById("hangup").disabled = false;
};
document.getElementById("hangup").onclick = () => vapi.stop();
vapi.on("call-end", () => status("Call ended. You can close this tab."));
</script></body></html>"""


class WebCaller:
    def __init__(self, api_key: str, assistant_id: str) -> None:
        self._api = VapiCaller(api_key, assistant_id, phone_number_id="")
        self._assistant = assistant_id
        self._public_key = os.environ.get("VAPI_PUBLIC_KEY", "")
        self._pages: dict[str, str] = {}
        self._call_ids: dict[str, str] = {}
        self._placed: dict[str, float] = {}
        self._server: ThreadingHTTPServer | None = None

    def check(self) -> None:
        self._api.check_assistant()
        if not self._public_key:
            raise RuntimeError("VAPI_PUBLIC_KEY is not set (needed for VOICE_MODE=web)")

    def place_call(self, phone: str, variables: dict[str, str]) -> str:
        self._serve()
        token = secrets.token_urlsafe(8)
        self._pages[token] = (
            _PAGE.replace("__PUBLIC_KEY__", self._public_key)
            .replace("__ASSISTANT__", self._assistant)
            .replace("__VARIABLES__", json.dumps(variables))
            .replace("__CUSTOMER__", variables.get("customer_name", ""))
            .replace("__TOKEN__", token)
        )
        self._placed[token] = time.monotonic()
        url = f"http://127.0.0.1:{PORT}/call/{token}"
        print(f"[voice] web call for the AE: {url}")
        webbrowser.open(url)
        return f"web:{token}"

    def get_result(self, call_id: str) -> CallResult | None:
        token = call_id.removeprefix("web:")
        vapi_id = self._call_ids.get(token)
        if vapi_id is None:
            placed = self._placed.get(token)
            if placed is None or time.monotonic() - placed < RING_SECONDS:
                return None  # still "ringing"
            self._pages.pop(token, None)  # the page can no longer be answered
            return CallResult(
                call_id, CallStatus.NO_ANSWER, (), f"not answered within {RING_SECONDS:.0f} s"
            )
        result = self._api.get_result(vapi_id)
        return (
            None
            if result is None
            else CallResult(call_id, result.status, result.turns, result.detail)
        )

    def _serve(self) -> None:
        if self._server is not None:
            return
        caller = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                url = urlparse(self.path)
                if url.path.startswith("/call/") and url.path[6:] in caller._pages:
                    self._send(200, caller._pages[url.path[6:]], "text/html")
                elif url.path == "/started":
                    q = parse_qs(url.query)
                    caller._call_ids[q["token"][0]] = q["call_id"][0]
                    self._send(200, "ok", "text/plain")
                else:
                    self._send(404, "not found", "text/plain")

            def _send(self, code: int, body: str, kind: str) -> None:
                self.send_response(code)
                self.send_header("Content-Type", f"{kind}; charset=utf-8")
                self.end_headers()
                self.wfile.write(body.encode())

            def log_message(self, *args: Any) -> None:
                return None

        self._server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
