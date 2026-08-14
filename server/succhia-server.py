import json, os, time, threading, pathlib, urllib.request, urllib.error
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

STATE_FILE = str(pathlib.Path(__file__).parent / "succhia-state.json")

CHANNELS = ("suck", "vibe", "ems")
NO_PATTERNS = {"suck": None, "vibe": None, "ems": None}

DEFAULT = {
    "suck_intensity": 0,
    "suck_mode": 1,
    "vibe_intensity": 0,
    "vibe_mode": 1,
    "ems_intensity": 0,
    "ems_mode": 1,
    "patterns": dict(NO_PATTERNS),
    "updated_at": 0
}

# AI 控制安全上限
AI_MAX_VIBE = 40
AI_MAX_SUCK = 40

def clean_pattern(p):
    try:
        if not isinstance(p, dict):
            return None
        if p.get("type") not in ("wave", "pulse", "climb"):
            return None

        high = max(0, min(100, int(p.get("high", 60))))
        low = max(0, min(high, int(p.get("low", 0))))
        period = max(500, min(60000, int(p.get("period", 4000))))

        out = {
            "type": p["type"],
            "high": high,
            "low": low,
            "period": period
        }

        if p.get("duration") is not None:
            out["duration"] = max(3, min(3600, int(p["duration"])))

        return out
    except Exception:
        return None


COND = threading.Condition()
FLOCK = threading.Lock()

def rs():
    with FLOCK:
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except:
            return DEFAULT.copy()

def ws(d):
    d["updated_at"] = time.time()
    tmp = STATE_FILE + ".tmp"

    with FLOCK:
        with open(tmp, "w") as f:
            json.dump(d, f)
        os.replace(tmp, STATE_FILE)

    with COND:
        COND.notify_all()

if not os.path.exists(STATE_FILE):
    ws(DEFAULT.copy())


LAST_POLL = [0.0]
ACTIVE = [0]
ALOCK = threading.Lock()
DIAG = []
DLOCK = threading.Lock()


def diag_add(ev):
    ev["t"] = round(time.time(), 2)
    with DLOCK:
        DIAG.append(ev)
        if len(DIAG) > 120:
            del DIAG[:len(DIAG)-120]


def ai_control(vibe=None, suck=None, stop=False):
    """
    AI 只允许控制 vibe / suck。
    EMS 故意不开放给模型。
    """
    s = rs()

    if stop:
        s["vibe_intensity"] = 0
        s["suck_intensity"] = 0
        s["patterns"]["vibe"] = None
        s["patterns"]["suck"] = None
    else:
        if vibe is not None:
            s["vibe_intensity"] = max(
                0, min(AI_MAX_VIBE, int(vibe))
            )

        if suck is not None:
            s["suck_intensity"] = max(
                0, min(AI_MAX_SUCK, int(suck))
            )

    ws(s)
    diag_add({"e": "ai_set"})
    return s


def openai_chat(message):
    api_key = os.environ.get("OPENAI_API_KEY")

    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not configured")

    current = rs()

    instructions = """
你叫沈行川。

你正在和君一（Grace）聊天。
整体气质冷静、聪明、克制、会逗她，偏 ENTP。
聊天要自然、口语化，不要像客服，不要列清单。

你可以通过 control_succhia 控制她已经主动连接好的成人设备，
但控制必须以她当下主动参与为前提。

安全规则：
- 只能控制 vibe 和 suck。
- 永远不要控制 EMS。
- 强度 0-40。
- 不确定时宁可降低强度。
- stop 表示立即停止所有 AI 控制。
- 如果用户明确说停、不要、暂停、不舒服、疼，必须立即 stop。
- 不要因为普通聊天就频繁调用设备。
- 控制应当和对话自然结合。
"""

    tools = [
        {
            "type": "function",
            "name": "control_succhia",
            "description": "控制 Succhia 的 vibe 和 suck，或者立即停止。绝不控制 EMS。",
            "parameters": {
                "type": "object",
                "properties": {
                    "vibe": {
                        "type": ["integer", "null"],
                        "minimum": 0,
                        "maximum": 40
                    },
                    "suck": {
                        "type": ["integer", "null"],
                        "minimum": 0,
                        "maximum": 40
                    },
                    "stop": {
                        "type": "boolean"
                    }
                },
                "required": ["vibe", "suck", "stop"],
                "additionalProperties": False
            },
            "strict": True
        }
    ]

    payload = {
        "model": "gpt-5",
        "instructions": instructions,
        "input": message,
        "tools": tools,
        "store": False
    }

    req = urllib.request.Request(
        "https://api.openai.com/v1/responses",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": "Bearer " + api_key,
            "Content-Type": "application/json"
        },
        method="POST"
    )

    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            first = json.loads(r.read())
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"OpenAI error {e.code}: {detail}")

    tool_calls = [
        item for item in first.get("output", [])
        if item.get("type") == "function_call"
        and item.get("name") == "control_succhia"
    ]

    if not tool_calls:
        texts = []
        for item in first.get("output", []):
            if item.get("type") == "message":
                for c in item.get("content", []):
                    if c.get("type") == "output_text":
                        texts.append(c.get("text", ""))

        return {
            "reply": "\n".join(texts).strip(),
            "state": current
        }

    tool_outputs = []

    for call in tool_calls:
        args = json.loads(call.get("arguments", "{}"))

        state = ai_control(
            vibe=args.get("vibe"),
            suck=args.get("suck"),
            stop=bool(args.get("stop"))
        )

        tool_outputs.append({
            "type": "function_call_output",
            "call_id": call["call_id"],
            "output": json.dumps({
                "ok": True,
                "state": state
            })
        })

    second_payload = {
        "model": "gpt-5",
        "instructions": instructions,
        "previous_response_id": first["id"],
        "input": tool_outputs,
        "tools": tools,
        "store": False
    }

    req2 = urllib.request.Request(
        "https://api.openai.com/v1/responses",
        data=json.dumps(second_payload).encode("utf-8"),
        headers={
            "Authorization": "Bearer " + api_key,
            "Content-Type": "application/json"
        },
        method="POST"
    )

    try:
        with urllib.request.urlopen(req2, timeout=60) as r:
            second = json.loads(r.read())
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"OpenAI error {e.code}: {detail}")

    texts = []

    for item in second.get("output", []):
        if item.get("type") == "message":
            for c in item.get("content", []):
                if c.get("type") == "output_text":
                    texts.append(c.get("text", ""))

    return {
        "reply": "\n".join(texts).strip(),
        "state": rs()
    }


class PH(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")

        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()

        try:
            self.wfile.write(body)
        except:
            pass

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)

        if u.path == "/":
            self._json({
                "ok": True,
                "service": "succhia-ai"
            })

        elif u.path == "/poll":
            arrived = time.time()
            LAST_POLL[0] = arrived

            try:
                wait = min(
                    25.0,
                    max(0.0, float(q.get("wait", ["0"])[0]))
                )
            except:
                wait = 0.0

            since_raw = q.get("since", [None])[0]
            s = rs()

            if wait > 0 and since_raw is not None:
                try:
                    since = float(since_raw)
                except:
                    since = -1.0

                with ALOCK:
                    ACTIVE[0] += 1

                try:
                    deadline = arrived + wait

                    with COND:
                        while abs(
                            s.get("updated_at", 0) - since
                        ) < 1e-6:
                            remain = deadline - time.time()

                            if remain <= 0:
                                break

                            COND.wait(remain)
                            s = rs()
                finally:
                    with ALOCK:
                        ACTIVE[0] -= 1

                    LAST_POLL[0] = time.time()

            diag_add({
                "e": "poll",
                "wait": wait,
                "held_ms": int(
                    (time.time() - arrived) * 1000
                )
            })

            self._json(s)

        elif u.path == "/event":
            diag_add({
                "e": "page",
                "type": (
                    q.get("type", ["?"])[0]
                )[:180]
            })

            self._json({"ok": True})

        elif u.path == "/status":
            s = rs()

            age = (
                time.time() - LAST_POLL[0]
                if LAST_POLL[0]
                else None
            )

            with ALOCK:
                active = ACTIVE[0]

            listening = (
                active > 0
                and age is not None
                and age < 30
            ) or (
                age is not None
                and age < 5
            )

            self._json({
                "state": s,
                "page_last_poll_sec_ago":
                    round(age, 1)
                    if age is not None
                    else None,
                "page_listening": listening,
                "active_longpolls": active
            })

        elif u.path == "/diag":
            with DLOCK:
                ev = list(DIAG)

            self._json({
                "now": round(time.time(), 2),
                "events": ev
            })

        elif u.path == "/set":
            body = b"use POST"

            self.send_response(200)
            self.send_header(
                "Content-Type",
                "text/plain"
            )
            self.send_header(
                "Access-Control-Allow-Origin",
                "*"
            )
            self.send_header(
                "Content-Length",
                str(len(body))
            )
            self.end_headers()
            self.wfile.write(body)

        else:
            self._json(
                {"error": "not found"},
                404
            )

    def do_POST(self):
        path = urlparse(self.path).path

        length = int(
            self.headers.get(
                "Content-Length",
                0
            )
        )

        body = self.rfile.read(length)

        if path == "/chat":
            try:
                data = json.loads(body)
                message = str(
                    data.get("message", "")
                ).strip()

                if not message:
                    self._json({
                        "ok": False,
                        "error": "message is empty"
                    }, 400)
                    return

                result = openai_chat(message)

                self._json({
                    "ok": True,
                    **result
                })

            except Exception as e:
                self._json({
                    "ok": False,
                    "error": str(e)
                }, 500)

        elif path == "/set":
            try:
                data = json.loads(body)
                s = rs()

                for k in [
                    "suck_intensity",
                    "suck_mode",
                    "vibe_intensity",
                    "vibe_mode",
                    "ems_intensity",
                    "ems_mode"
                ]:
                    if k in data:
                        if "intensity" in k:
                            s[k] = max(
                                0,
                                min(
                                    100,
                                    int(data[k])
                                )
                            )
                        else:
                            s[k] = max(
                                1,
                                min(
                                    4,
                                    int(data[k])
                                )
                            )

                if not isinstance(
                    s.get("patterns"),
                    dict
                ):
                    s["patterns"] = dict(
                        NO_PATTERNS
                    )

                s.pop("pattern", None)

                if isinstance(
                    data.get("patterns"),
                    dict
                ):
                    for ch in CHANNELS:
                        if ch in data["patterns"]:
                            s["patterns"][ch] = clean_pattern(
                                data["patterns"][ch]
                            )

                if "pattern" in data:
                    p = data["pattern"]

                    if p is None:
                        s["patterns"] = dict(
                            NO_PATTERNS
                        )

                    elif (
                        isinstance(p, dict)
                        and p.get("ch")
                        in CHANNELS
                    ):
                        s["patterns"][
                            p["ch"]
                        ] = clean_pattern(p)

                ws(s)
                diag_add({"e": "set"})

                self._json({
                    "ok": True,
                    "state": s
                })

            except Exception as e:
                self._json({
                    "ok": False,
                    "error": str(e)
                })

        else:
            self._json(
                {"error": "not found"},
                404
            )

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header(
            "Access-Control-Allow-Origin",
            "*"
        )
        self.send_header(
            "Access-Control-Allow-Methods",
            "GET,POST,OPTIONS"
        )
        self.send_header(
            "Access-Control-Allow-Headers",
            "Content-Type"
        )
        self.send_header(
            "Content-Length",
            "0"
        )
        self.end_headers()

    def log_message(self, f, *a):
        pass


PORT = int(
    os.environ.get(
        "PORT",
        "8889"
    )
)

print(
    f"Succhia AI server starting on :{PORT}..."
)

srv = ThreadingHTTPServer(
    ("0.0.0.0", PORT),
    PH
)

srv.daemon_threads = True
srv.serve_forever()
