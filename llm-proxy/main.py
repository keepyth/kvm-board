#!/usr/bin/env python3
"""Tiny LLM proxy: strips response_format json_object (unsupported by LM Studio)
before forwarding to the real LM Studio server."""
import json
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

UPSTREAM = "http://192.168.110.71:1234"

# 中文保真指令：小模型（3B）在总结时会改写/吞掉中文实体名（实测「东山精密」→「东山若」），
# 这里在代理层给 system prompt 追加硬性约束，避免改 memfab 内部提示词。
FIDELITY = (
    "\n\n【中文输出硬性要求】\n"
    "1. 所有中文专有名词（公司名、股票名、人名、地名、产品名、行业术语）必须逐字原样照抄原始文本，"
    "禁止改写、简化、音译、翻译、纠错或用近似字替换。\n"
    "2. 原文中疑似 OCR 错字也保持原样输出，不要自行\"修正\"。\n"
    "3. 输出正文一律使用简体中文；原有 JSON/结构化格式要求保持不变。\n"
)


class Handler(BaseHTTPRequestHandler):
    def _proxy(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else None
        if body:
            try:
                data = json.loads(body)
                rf = data.get("response_format")
                if rf and rf.get("type") == "json_object":
                    data.pop("response_format", None)  # prompt 已要求返回 JSON，去掉即可
                msgs = data.get("messages")
                if isinstance(msgs, list) and msgs:
                    for m in msgs:
                        if isinstance(m, dict) and m.get("role") in ("system", "developer"):
                            c = m.get("content")
                            if isinstance(c, str):
                                m["content"] = c + FIDELITY
                                break
                            if isinstance(c, list):
                                for part in c:
                                    if isinstance(part, dict) and part.get("type") == "text":
                                        part["text"] += FIDELITY
                                        break
                                break
                    else:
                        msgs.insert(0, {"role": "system", "content": FIDELITY})
                body = json.dumps(data).encode()
                print("%s inject=%s messages=%d" % (
                    time.strftime("%H:%M:%S"),
                    "【中文输出硬性要求】" in json.dumps(data, ensure_ascii=False),
                    len(msgs) if isinstance(msgs, list) else -1), flush=True)
            except Exception:
                pass
        req = urllib.request.Request(UPSTREAM + self.path, body,
                                     {"Content-Type": "application/json"}, method=self.command)
        try:
            r = urllib.request.urlopen(req, timeout=120)
            code, payload = r.status, r.read()
        except urllib.error.HTTPError as e:
            code, payload = e.code, e.read()
        except Exception as e:
            code = 502
            payload = json.dumps({"error": str(e)}).encode()
        # memfab 对 content 做严格 JSON 解析：剥掉模型可能加的 markdown 围栏
        try:
            resp = json.loads(payload)
            content = resp["choices"][0]["message"]["content"]
            stripped = content.strip()
            if stripped.startswith("```"):
                stripped = stripped.split("```")[1] if "```" in stripped else stripped
                if stripped.startswith("json"):
                    stripped = stripped[4:]
                stripped = stripped.strip()
            if stripped.startswith("{") or stripped.startswith("["):
                json.loads(stripped)  # 校验
                resp["choices"][0]["message"]["content"] = stripped
                payload = json.dumps(resp).encode()
        except Exception:
            pass
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    do_GET = do_POST = _proxy

    def log_message(self, fmt, *args):
        pass

if __name__ == "__main__":
    HTTPServer(("127.0.0.1", 1235), Handler).serve_forever()
