#!/usr/bin/env python3
"""Tiny LLM proxy in front of LM Studio.

职责（全部在"加工层"，不触碰任何原始 OCR 记录）：
1. 去掉 LM Studio 不支持的 response_format: json_object
2. 剥掉模型输出的 markdown 围栏，保证 memfab 能严格 JSON 解析
3. 中文专有名词保真约束（防模型在总结时改写实体名）
4. **输入预算裁剪**：tesseract 每块原文约 3000 字，timeline 一次塞 40 块会爆
   LM Studio 的 8192 上下文（n_keep >= n_ctx）。这里把每个 text 字段压到预算内，
   保住 JSON 结构，只砍正文长度。
"""
import json
import re
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

UPSTREAM = "http://192.168.110.71:1234"

# 输入总字符预算（实测该 tokenizer 约 0.78 token/字符，8192 ctx 下留足余量）
CTX_CHAR_BUDGET = 8500
TEXT_LIMIT_START = 400      # 单个 text 字段初始上限
TEXT_LIMIT_MIN = 60

FIDELITY = (
    "\n\n【中文输出硬性要求】\n"
    "1. 所有中文专有名词（公司名、股票名、人名、地名、产品名、行业术语）必须逐字原样照抄原始文本，"
    "禁止改写、简化、音译、翻译、纠错或用近似字替换。\n"
    "2. 原文中疑似 OCR 错字也保持原样输出，不要自行\"修正\"。\n"
    "3. 输出正文一律使用简体中文；原有 JSON/结构化格式要求保持不变。\n"
)

TEXT_RE = re.compile(r'"text"\s*:\s*"((?:[^"\\]|\\.)*)"')
BBOX_RE = re.compile(r',"bbox":\s*\[[^\]]*\]')   # 坐标对模型无用，整段删除


def _strip_bbox(s):
    return BBOX_RE.sub("", s)


def _shrink_text_fields(s, limit):
    """把 JSON 文本里每个 "text":"..." 字段压到 limit 字以内，保持 JSON 结构合法。"""
    def rep(m):
        raw = m.group(1)
        try:
            val = json.loads('"' + raw + '"')
        except Exception:
            return m.group(0)
        if len(val) <= limit:
            return m.group(0)
        kept = val[:limit]
        new = kept + "…[省略%d字]" % (len(val) - limit)
        return '"text":' + json.dumps(new, ensure_ascii=False)
    return TEXT_RE.sub(rep, s)


def _msg_text(m):
    c = m.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return "".join(p.get("text", "") for p in c if isinstance(p, dict))
    return ""


def _set_msg_text(m, text):
    c = m.get("content")
    if isinstance(c, str):
        m["content"] = text
    elif isinstance(c, list):
        texts = [p for p in c if isinstance(p, dict) and p.get("type") == "text"]
        if texts:
            texts[0]["text"] = text


def fit_budget(msgs):
    """把 messages 压进上下文预算，返回 (原始字符数, 压缩后字符数, 用的单块上限)。"""
    for m in msgs:                       # 第一步：无脑删坐标，信息零损失
        t = _msg_text(m)
        if t:
            nt = _strip_bbox(t)
            if nt != t:
                _set_msg_text(m, nt)
    total = sum(len(_msg_text(m)) for m in msgs)
    if total <= CTX_CHAR_BUDGET:
        return total, total, None
    limit = TEXT_LIMIT_START
    while limit >= TEXT_LIMIT_MIN:
        for m in msgs:
            t = _msg_text(m)
            if len(t) > limit:
                nt = _shrink_text_fields(t, limit)
                if nt == t:  # 没有 text 字段可压：退化为头尾保留
                    nt = t[: int(len(t) * 0.6)] + "\n…[中间省略]…\n" + t[-int(len(t) * 0.25):]
                _set_msg_text(m, nt)
        now = sum(len(_msg_text(m)) for m in msgs)
        if now <= CTX_CHAR_BUDGET:
            return total, now, limit
        limit //= 2
    return total, sum(len(_msg_text(m)) for m in msgs), limit


class Handler(BaseHTTPRequestHandler):
    def _proxy(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else None
        info = {"inject": False, "msgs": -1, "raw": 0, "sent": 0, "limit": None}
        if body:
            try:
                data = json.loads(body)
                rf = data.get("response_format")
                if rf and rf.get("type") == "json_object":
                    data.pop("response_format", None)
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
                    raw_len, sent_len, limit = fit_budget(msgs)
                    info.update(inject=FIDELITY.splitlines()[1][:6] in json.dumps(data, ensure_ascii=False),
                                msgs=len(msgs), raw=raw_len, sent=sent_len, limit=limit)
                body = json.dumps(data).encode()
            except Exception as e:
                print("%s preprocess_error=%s" % (time.strftime("%H:%M:%S"), e), flush=True)

        req = urllib.request.Request(UPSTREAM + self.path, body,
                                     {"Content-Type": "application/json"}, method=self.command)
        try:
            r = urllib.request.urlopen(req, timeout=180)
            code, payload = r.status, r.read()
        except urllib.error.HTTPError as e:
            code, payload = e.code, e.read()
        except Exception as e:
            code = 502
            payload = json.dumps({"error": {"message": str(e)}}).encode()

        # 上游报错（如 n_keep 超限）转成标准 OpenAI 错误结构，方便 memfab 记录真实原因
        note = ""
        try:
            up = json.loads(payload)
            if isinstance(up.get("error"), str):
                note = up["error"][:120]
                payload = json.dumps({"error": {"message": up["error"], "type": "upstream_error"}}).encode()
                code = 400 if code == 200 else code
            else:
                content = up["choices"][0]["message"]["content"]
                stripped = content.strip()
                if stripped.startswith("```"):
                    stripped = stripped.split("```")[1] if "```" in stripped else stripped
                    if stripped.startswith("json"):
                        stripped = stripped[4:]
                    stripped = stripped.strip()
                if stripped.startswith("{") or stripped.startswith("["):
                    json.loads(stripped)
                    up["choices"][0]["message"]["content"] = stripped
                    payload = json.dumps(up).encode()
        except Exception:
            pass

        print("%s msgs=%d chars %d->%d limit=%s code=%s %s" % (
            time.strftime("%H:%M:%S"), info["msgs"], info["raw"], info["sent"],
            info["limit"], code, note), flush=True)

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
