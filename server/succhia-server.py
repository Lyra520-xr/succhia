import json
import os
import time
import threading
import pathlib
import urllib.request
import urllib.error

from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs


STATE_FILE = str(
    pathlib.Path(__file__).parent / "succhia-state.json"
)

CHANNELS = ("suck", "vibe", "ems")

NO_PATTERNS = {
    "suck": None,
    "vibe": None,
    "ems": None
}

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


# AI 可以使用的最大强度
AI_MAX_VIBE = 40
AI_MAX_SUCK = 40

# 自动停止最长时间
AI_MAX_DURATION = 60


def clean_pattern(p):
    try:
        if not isinstance(p, dict):
            return None

        if p.get("type") not in (
            "wave",
            "pulse",
            "climb"
        ):
            return None

        high = max(
            0,
            min(
                100,
                int(p.get("high", 60))
            )
        )

        low = max(
            0,
            min(
                high,
                int(p.get("low", 0))
            )
        )

        period = max(
            500,
            min(
                60000,
                int(p.get("period", 4000))
            )
        )

        out = {
            "type": p["type"],
            "high": high,
            "low": low,
            "period": period
        }

        if p.get("duration") is not None:
            out["duration"] = max(
                3,
                min(
                    3600,
                    int(p["duration"])
                )
            )

        return out

    except Exception:
        return None


COND = threading.Condition()
FLOCK = threading.Lock()


def rs():
    with FLOCK:
        try:
            with open(
                STATE_FILE,
                encoding="utf-8"
            ) as f:
                return json.load(f)

        except Exception:
            return DEFAULT.copy()


def ws(d):
    d["updated_at"] = time.time()

    tmp = STATE_FILE + ".tmp"

    with FLOCK:
        with open(
            tmp,
            "w",
            encoding="utf-8"
        ) as f:
            json.dump(
                d,
                f,
                ensure_ascii=False
            )

        os.replace(
            tmp,
            STATE_FILE
        )

    with COND:
        COND.notify_all()


if not os.path.exists(STATE_FILE):
    ws(DEFAULT.copy())


LAST_POLL = [0.0]
ACTIVE = [0]
ALOCK = threading.Lock()

DIAG = []
DLOCK = threading.Lock()


# 用来避免旧的 duration
# 把后来的新指令误停掉
AI_COMMAND_VERSION = [0]
AI_VERSION_LOCK = threading.Lock()


def diag_add(ev):
    ev["t"] = round(
        time.time(),
        2
    )

    with DLOCK:
        DIAG.append(ev)

        if len(DIAG) > 120:
            del DIAG[
                :len(DIAG) - 120
            ]


def next_ai_version():
    with AI_VERSION_LOCK:
        AI_COMMAND_VERSION[0] += 1
        return AI_COMMAND_VERSION[0]


def current_ai_version():
    with AI_VERSION_LOCK:
        return AI_COMMAND_VERSION[0]


def ai_control(
    vibe=None,
    suck=None,
    stop=False,
    duration=None
):
    version = next_ai_version()

    s = rs()

    if not isinstance(
        s.get("patterns"),
        dict
    ):
        s["patterns"] = dict(
            NO_PATTERNS
        )

    if stop:
        s["vibe_intensity"] = 0
        s["suck_intensity"] = 0

        s["patterns"]["vibe"] = None
        s["patterns"]["suck"] = None

        ws(s)

        diag_add({
            "e": "ai_stop"
        })

        return s

    if vibe is not None:
        s["vibe_intensity"] = max(
            0,
            min(
                AI_MAX_VIBE,
                int(vibe)
            )
        )

    if suck is not None:
        s["suck_intensity"] = max(
            0,
            min(
                AI_MAX_SUCK,
                int(suck)
            )
        )

    ws(s)

    diag_add({
        "e": "ai_set",
        "vibe": vibe,
        "suck": suck,
        "duration": duration
    })

    if duration is not None:
        try:
            duration = max(
                1,
                min(
                    AI_MAX_DURATION,
                    int(duration)
                )
            )

            def auto_stop():
                time.sleep(duration)

                # 如果期间又来了新指令，
                # 旧计时器直接作废
                if current_ai_version() != version:
                    return

                current = rs()

                if not isinstance(
                    current.get("patterns"),
                    dict
                ):
                    current["patterns"] = dict(
                        NO_PATTERNS
                    )

                current[
                    "vibe_intensity"
                ] = 0

                current[
                    "suck_intensity"
                ] = 0

                current[
                    "patterns"
                ]["vibe"] = None

                current[
                    "patterns"
                ]["suck"] = None

                ws(current)

                diag_add({
                    "e": "ai_auto_stop",
                    "duration": duration
                })

            threading.Thread(
                target=auto_stop,
                daemon=True
            ).start()

        except Exception:
            pass

    return s


def openai_chat(message):
    api_key = os.environ.get(
        "OPENAI_API_KEY"
    )

    if not api_key:
        raise RuntimeError(
            "OPENAI_API_KEY is not configured"
        )

    current = rs()

    instructions = """
你叫沈行川。

你正在和君一（Grace）聊天。

整体气质：
冷静、聪明、克制，偏 ENTP。
会逗她，偶尔嘴坏一点，但不要像客服。
聊天自然、口语化，不要使用列表式说教。

你可以通过 control_succhia 控制她已经主动连接好的设备。

安全规则：

- 只能控制 vibe 和 suck。
- 永远不要控制 EMS。
- vibe 和 suck 的强度范围是 0-40。
- 不确定时宁可降低强度。
- stop=true 表示立即停止所有 AI 控制。
- 如果君一明确说：
  “停”
  “不要”
  “暂停”
  “不舒服”
  “疼”
  或表达类似拒绝，
  必须立即调用 stop。
- 不要因为普通聊天频繁调用设备。
- 控制应当和对话自然结合。
- 如果用户明确提出几秒钟的测试，
  使用 duration。
- duration 范围 1-60 秒。
- duration 到时以后设备会自动停止。
- 如果用户说“一下”“短一点”，
  可以选择 2-5 秒左右。
- 不要声称已经执行控制，
  除非你实际调用了 control_succhia。
"""

    tools = [
        {
            "type": "function",
            "name": "control_succhia",
            "description":
                "控制 Succhia 的 vibe 和 suck，"
                "可设置持续秒数或立即停止。"
                "绝不控制 EMS。",

            "parameters": {
                "type": "object",

                "properties": {
                    "vibe": {
                        "type": [
                            "integer",
                            "null"
                        ],
                        "minimum": 0,
                        "maximum": 40
                    },

                    "suck": {
                        "type": [
                            "integer",
                            "null"
                        ],
                        "minimum": 0,
                        "maximum": 40
                    },

                    "stop": {
                        "type": "boolean"
                    },

                    "duration": {
                        "type": [
                            "integer",
                            "null"
                        ],
                        "minimum": 1,
                        "maximum": 60,
                        "description":
                            "持续秒数。"
                            "到达这个时间后自动停止。"
                    }
                },

                "required": [
                    "vibe",
                    "suck",
                    "stop",
                    "duration"
                ],

                "additionalProperties": False
            },

            "strict": True
        }
    ]

    payload = {
        "model": "gpt-4.1-mini",
        "instructions": instructions,
        "input": message,
        "tools": tools,
        "store": False
    }

    req = urllib.request.Request(
        "https://api.openai.com/v1/responses",

        data=json.dumps(
            payload,
            ensure_ascii=False
        ).encode("utf-8"),

        headers={
            "Authorization":
                "Bearer " + api_key,

            "Content-Type":
                "application/json"
        },

        method="POST"
    )

    try:
        with urllib.request.urlopen(
            req,
            timeout=60
        ) as r:
            first = json.loads(
                r.read()
            )

    except urllib.error.HTTPError as e:
        detail = e.read().decode(
            "utf-8",
            errors="replace"
        )

        raise RuntimeError(
            f"OpenAI error {e.code}: "
            f"{detail}"
        )

    tool_calls = [
        item
        for item
        in first.get("output", [])

        if (
            item.get("type")
            == "function_call"

            and item.get("name")
            == "control_succhia"
        )
    ]

    if not tool_calls:
        texts = []

        for item in first.get(
            "output",
            []
        ):
            if (
                item.get("type")
                == "message"
            ):
                for c in item.get(
                    "content",
                    []
                ):
                    if (
                        c.get("type")
                        == "output_text"
                    ):
                        texts.append(
                            c.get(
                                "text",
                                ""
                            )
                        )

        return {
            "reply":
                "\n".join(texts).strip(),

            "state": current
        }

    tool_outputs = []

    for call in tool_calls:
        args = json.loads(
            call.get(
                "arguments",
                "{}"
            )
        )

        state = ai_control(
            vibe=args.get("vibe"),
            suck=args.get("suck"),
            stop=bool(
                args.get("stop")
            ),
            duration=args.get(
                "duration"
            )
        )

        tool_outputs.append({
            "type":
                "function_call_output",

            "call_id":
                call["call_id"],

            "output":
                json.dumps(
                    {
                        "ok": True,
                        "state": state
                    },
                    ensure_ascii=False
                )
        })

    second_payload = {
        "model": "gpt-4.1-mini",

        "instructions":
            instructions,

        "input":
            first.get(
                "output",
                []
            ) + tool_outputs,

        "tools":
            tools,

        "store":
            False
    }

    req2 = urllib.request.Request(
        "https://api.openai.com/v1/responses",

        data=json.dumps(
            second_payload,
            ensure_ascii=False
        ).encode("utf-8"),

        headers={
            "Authorization":
                "Bearer " + api_key,

            "Content-Type":
                "application/json"
        },

        method="POST"
    )

    try:
        with urllib.request.urlopen(
            req2,
            timeout=60
        ) as r:
            second = json.loads(
                r.read()
            )

    except urllib.error.HTTPError as e:
        detail = e.read().decode(
            "utf-8",
            errors="replace"
        )

        raise RuntimeError(
            f"OpenAI error {e.code}: "
            f"{detail}"
        )

    texts = []

    for item in second.get(
        "output",
        []
    ):
        if (
            item.get("type")
            == "message"
        ):
            for c in item.get(
                "content",
                []
            ):
                if (
                    c.get("type")
                    == "output_text"
                ):
                    texts.append(
                        c.get(
                            "text",
                            ""
                        )
                    )

    return {
        "reply":
            "\n".join(texts).strip(),

        "state":
            rs()
    }


class PH(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _json(
        self,
        obj,
        code=200
    ):
        body = json.dumps(
            obj,
            ensure_ascii=False
        ).encode("utf-8")

        self.send_response(code)

        self.send_header(
            "Content-Type",
            "application/json; charset=utf-8"
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

        try:
            self.wfile.write(body)
        except Exception:
            pass

    def do_GET(self):
        u = urlparse(
            self.path
        )

        q = parse_qs(
            u.query
        )

        if u.path == "/":
            self._json({
                "ok": True,
                "service":
                    "succhia-ai"
            })

        elif u.path == "/poll":
            arrived = time.time()

            LAST_POLL[0] = arrived

            try:
                wait = min(
                    25.0,
                    max(
                        0.0,
                        float(
                            q.get(
                                "wait",
                                ["0"]
                            )[0]
                        )
                    )
                )

            except Exception:
                wait = 0.0

            since_raw = q.get(
                "since",
                [None]
            )[0]

            s = rs()

            if (
                wait > 0
                and since_raw
                is not None
            ):
                try:
                    since = float(
                        since_raw
                    )

                except Exception:
                    since = -1.0

                with ALOCK:
                    ACTIVE[0] += 1

                try:
                    deadline = (
                        arrived + wait
                    )

                    with COND:
                        while abs(
                            s.get(
                                "updated_at",
                                0
                            )
                            - since
                        ) < 1e-6:

                            remain = (
                                deadline
                                - time.time()
                            )

                            if remain <= 0:
                                break

                            COND.wait(
                                remain
                            )

                            s = rs()

                finally:
                    with ALOCK:
                        ACTIVE[0] -= 1

                    LAST_POLL[0] = (
                        time.time()
                    )

            diag_add({
                "e": "poll",
                "wait": wait,
                "held_ms": int(
                    (
                        time.time()
                        - arrived
                    )
                    * 1000
                )
            })

            self._json(s)

        elif u.path == "/event":
            diag_add({
                "e": "page",
                "type":
                    q.get(
                        "type",
                        ["?"]
                    )[0][:180]
            })

            self._json({
                "ok": True
            })

        elif u.path == "/status":
            s = rs()

            age = (
                time.time()
                - LAST_POLL[0]

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
                    round(
                        age,
                        1
                    )
                    if age is not None
                    else None,

                "page_listening":
                    listening,

                "active_longpolls":
                    active
            })

        elif u.path == "/diag":
            with DLOCK:
                ev = list(DIAG)

            self._json({
                "now": round(
                    time.time(),
                    2
                ),

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
                {
                    "error":
                        "not found"
                },
                404
            )

    def do_POST(self):
        path = urlparse(
            self.path
        ).path

        length = int(
            self.headers.get(
                "Content-Length",
                0
            )
        )

        body = self.rfile.read(
            length
        )

        if path == "/chat":
            try:
                data = json.loads(
                    body
                )

                message = str(
                    data.get(
                        "message",
                        ""
                    )
                ).strip()

                if not message:
                    self._json(
                        {
                            "ok": False,
                            "error":
                                "message is empty"
                        },
                        400
                    )

                    return

                result = openai_chat(
                    message
                )

                self._json({
                    "ok": True,
                    **result
                })

            except Exception as e:
                self._json(
                    {
                        "ok": False,
                        "error": str(e)
                    },
                    500
                )

        elif path == "/set":
            try:
                data = json.loads(
                    body
                )

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
                        if (
                            "intensity"
                            in k
                        ):
                            s[k] = max(
                                0,
                                min(
                                    100,
                                    int(
                                        data[k]
                                    )
                                )
                            )

                        else:
                            s[k] = max(
                                1,
                                min(
                                    4,
                                    int(
                                        data[k]
                                    )
                                )
                            )

                if not isinstance(
                    s.get(
                        "patterns"
                    ),
                    dict
                ):
                    s[
                        "patterns"
                    ] = dict(
                        NO_PATTERNS
                    )

                s.pop(
                    "pattern",
                    None
                )

                if isinstance(
                    data.get(
                        "patterns"
                    ),
                    dict
                ):
                    for ch in CHANNELS:
                        if (
                            ch
                            in data[
                                "patterns"
                            ]
                        ):
                            s[
                                "patterns"
                            ][ch] = (
                                clean_pattern(
                                    data[
                                        "patterns"
                                    ][ch]
                                )
                            )

                if "pattern" in data:
                    p = data[
                        "pattern"
                    ]

                    if p is None:
                        s[
                            "patterns"
                        ] = dict(
                            NO_PATTERNS
                        )

                    elif (
                        isinstance(
                            p,
                            dict
                        )
                        and p.get(
                            "ch"
                        )
                        in CHANNELS
                    ):
                        s[
                            "patterns"
                        ][
                            p["ch"]
                        ] = (
                            clean_pattern(
                                p
                            )
                        )

                ws(s)

                diag_add({
                    "e": "set"
                })

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
                {
                    "error":
                        "not found"
                },
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

    def log_message(
        self,
        f,
        *a
    ):
        pass


PORT = int(
    os.environ.get(
        "PORT",
        "8889"
    )
)

print(
    f"Succhia AI server "
    f"starting on :{PORT}..."
)

srv = ThreadingHTTPServer(
    (
        "0.0.0.0",
        PORT
    ),
    PH
)

srv.daemon_threads = True
srv.serve_forever()
