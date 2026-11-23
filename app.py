#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
JD CK 助手 —— 配合青龙面板使用的京东 Cookie 一键提交工具

手机浏览器打开本页面 -> 粘贴抓到的 Cookie -> 自动清洗/校验 -> 通过青龙 Open API 更新环境变量。

零第三方依赖，仅用 Python 标准库。

配置（环境变量）:
  QL_URL             青龙面板地址，默认 http://127.0.0.1:5700
  QL_CLIENT_ID       青龙 应用设置 里创建应用得到的 Client ID
  QL_CLIENT_SECRET   对应的 Client Secret
  ENV_NAME           京东 CK 变量名，默认 JD_COOKIE
  MODE               multi=一账号一个变量(推荐)；merge=全部账号合并进一个变量(换行分隔)
  PORT               监听端口，默认 8787
  BIND_HOST          监听地址，默认 0.0.0.0（Docker 用），本地测试可设 127.0.0.1
  ACCESS_PASSWORD    访问密码；留空=不启用认证（纯内网）。通过 ddnsto 等暴露到
                     外网时务必设置强密码
"""
import base64
import hashlib
import hmac
import json
import os
import re
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib import request as urlreq
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote

QL_URL = os.environ.get("QL_URL", "http://127.0.0.1:5700").rstrip("/")
QL_CLIENT_ID = os.environ.get("QL_CLIENT_ID", "").strip()
QL_CLIENT_SECRET = os.environ.get("QL_CLIENT_SECRET", "").strip()
ENV_NAME = os.environ.get("ENV_NAME", "JD_COOKIE").strip() or "JD_COOKIE"
MODE = os.environ.get("MODE", "multi").strip().lower()
if MODE not in ("multi", "merge"):
    MODE = "multi"
PORT = int(os.environ.get("PORT", "8787"))
BIND_HOST = os.environ.get("BIND_HOST", "0.0.0.0").strip() or "0.0.0.0"
ACCESS_PASSWORD = os.environ.get("ACCESS_PASSWORD", "").strip()
AUTH_ENABLED = bool(ACCESS_PASSWORD)

# 登录会话：HMAC 签名 cookie，有效期 30 天
AUTH_SECRET = hashlib.sha256(("jd-ck-helper:" + ACCESS_PASSWORD).encode("utf-8")).digest()
AUTH_TTL = 30 * 86400
AUTH_COOKIE = "jdck_auth"

# 登录失败限速：ip -> [失败次数, 锁定截止时间戳]
LOGIN_FAILS = {}
LOGIN_LOCK = threading.Lock()
LOGIN_MAX_FAILS = 5
LOGIN_LOCK_SECS = 120

UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) "
      "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1")

# 京东扫码登录（qr.m.jd.com），可整体替换便于测试
QR_HOST = "https://qr.m.jd.com"
QR_APPID = "133"
QR_TTL = 180  # 二维码有效期（秒）
JD_ME_URL = "https://me-api.jd.com/user_new/info/GetJDUserInfoUnion"


# ---------------------------------------------------------------- 基础工具

def parse_ck(text):
    """从任意粘贴文本中提取 pt_key / pt_pin。

    返回 (ck, pin, err)：
      err 为 None  -> 正常，ck/pin 可用
      err 为字符串 -> 提取失败/内容异常的原因
    """
    if not text:
        return None, None, "没有内容"
    keys = re.findall(r"pt_key=([^;\s\"']+)", text)
    pins = re.findall(r"pt_pin=([^;\s\"']+)", text)
    if not keys or not pins:
        return None, None, None  # 未找到 pt_key/pt_pin，由调用方按"缺少字段"提示
    uniq_pins = list(dict.fromkeys(pins))
    if len(uniq_pins) > 1 or len(set(keys)) > 1:
        shown = "、".join(unquote(p) for p in uniq_pins[:4])
        return None, None, (
            "检测到 %d 个不同账号的 Cookie 混在一起（%s%s），"
            "请清空输入框后每次只粘贴一个账号" % (
                len(uniq_pins), shown, "…" if len(uniq_pins) > 4 else ""))
    ck = "pt_key=%s;pt_pin=%s;" % (keys[0], pins[0])
    return ck, pins[0], None


def http_json(method, url, headers=None, payload=None, timeout=12):
    data = None
    hdrs = {"User-Agent": UA}
    if headers:
        hdrs.update(headers)
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        hdrs["Content-Type"] = "application/json"
    req = urlreq.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urlreq.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "ignore")
    except HTTPError as e:
        body = e.read().decode("utf-8", "ignore")
    except (URLError, OSError) as e:
        raise RuntimeError("网络请求失败: %s" % e)
    try:
        return json.loads(body)
    except (ValueError, TypeError):
        return {"code": -1, "raw": body[:500]}


def _b64(data):
    return base64.b64encode(data).decode("ascii")


# ---------------------------------------------------------------- 京东 CK 校验

def check_jd(ck):
    """调用京东 me-api 校验 CK，返回 dict"""
    try:
        res = http_json("GET", JD_ME_URL,
                        headers={"Cookie": ck, "Referer": "https://home.m.jd.com/"})
    except RuntimeError as e:
        return {"ok": False, "msg": str(e)}
    raw = res if isinstance(res, dict) else {}
    data = raw.get("data") or {}
    base = (data.get("userInfo") or {}).get("baseInfo") or {}
    nickname = base.get("nickname")
    if nickname:
        return {"ok": True, "nickname": nickname,
                "curPin": base.get("curPin") or ""}
    # 未拿到昵称即视为失效
    hint = ""
    for key in ("msg", "message"):
        v = raw.get(key)
        if isinstance(v, str):
            hint = v
            break
    if not hint and "ret" in raw and str(raw["ret"]) not in ("0", '"0"'):
        hint = str(raw["ret"])
    if not hint and raw.get("raw"):
        hint = raw["raw"]
    return {"ok": False, "msg": hint or "CK 已失效或格式错误（京东返回无昵称）"}


# ---------------------------------------------------------------- 京东扫码登录

# sid -> {"cookies": "QRCodeKey=..;wlfstk_smdl=..", "token": wlfstk, "created": ts}
QR_SESSIONS = {}
QR_LOCK = threading.Lock()


def qr_new_session():
    """向京东申请登录二维码。返回 (sid, 图片bytes)。"""
    t = str(int(time.time() * 1000))
    url = "%s/show?appid=%s&size=300&t=%s" % (QR_HOST, QR_APPID, t)
    req = urlreq.Request(url, headers={
        "User-Agent": UA, "Referer": "https://passport.jd.com/"})
    with urlreq.urlopen(req, timeout=15) as resp:
        img = resp.read()
        cookie_pairs = []
        for line in (resp.headers.get_all("Set-Cookie") or []):
            pair = line.split(";")[0].strip()
            if "=" in pair:
                cookie_pairs.append(pair)
    jar = dict(p.split("=", 1) for p in cookie_pairs if "=" in p)
    if "wlfstk_smdl" not in jar:
        raise RuntimeError("京东二维码接口异常：未返回 wlfstk_smdl")
    sid = uuid.uuid4().hex[:16]
    with QR_LOCK:
        # 顺手清理过期会话
        now = time.time()
        for k in [k for k, v in QR_SESSIONS.items() if now - v["created"] > QR_TTL + 30]:
            QR_SESSIONS.pop(k, None)
        QR_SESSIONS[sid] = {
            "cookies": "; ".join(cookie_pairs),
            "token": jar["wlfstk_smdl"],
            "created": now,
        }
    return sid, img


def qr_poll(sid):
    """轮询扫码状态。返回前端可直接使用的结果 dict。

    京东 check 返回码：200 已确认(附 url) / 201 未扫描 / 176 已扫待确认 / 其它视为过期或异常
    """
    with QR_LOCK:
        ses = QR_SESSIONS.get(sid)
    if not ses:
        return {"ok": False, "status": "gone",
                "msg": "二维码会话不存在或已过期，请重新生成"}
    if time.time() - ses["created"] > QR_TTL:
        with QR_LOCK:
            QR_SESSIONS.pop(sid, None)
        return {"ok": False, "status": "expired",
                "msg": "二维码已过期，请重新生成"}

    t = str(int(time.time() * 1000))
    url = ("%s/check?callback=cb&appid=%s&token=%s&_=%s"
           % (QR_HOST, QR_APPID, quote(ses["token"]), t))
    req = urlreq.Request(url, method="POST", headers={
        "User-Agent": UA, "Referer": "https://passport.jd.com/",
        "Cookie": ses["cookies"]})
    try:
        with urlreq.urlopen(req, timeout=15) as resp:
            body = resp.read().decode("utf-8", "ignore")
    except (HTTPError, URLError, OSError) as e:
        return {"ok": False, "status": "waiting", "msg": "查询扫码状态失败，稍后重试: %s" % e}

    m = re.search(r"\((\{.*\})\)", body, re.S)
    try:
        data = json.loads(body)
    except ValueError:
        data = None
    if data is None and m:
        try:
            data = json.loads(m.group(1))
        except ValueError:
            data = None
    if not isinstance(data, dict):
        return {"ok": False, "status": "expired",
                "msg": "京东返回格式异常，请重新生成二维码"}

    code = data.get("code")
    if code == 200 and data.get("url"):
        with QR_LOCK:
            QR_SESSIONS.pop(sid, None)  # 会话已用掉
        login_url = str(data["url"])
        m_key = re.search(r"pt_key=([^;&\s]+)", login_url)
        m_pin = re.search(r"pt_pin=([^;&\s]+)", login_url)
        if not m_key or not m_pin:
            return {"ok": False, "status": "error",
                    "msg": "扫码成功但未从返回中解析到 pt_key/pt_pin，请改用粘贴方式"}
        ck = "pt_key=%s;pt_pin=%s;" % (m_key.group(1), m_pin.group(1))
        pin = unquote(m_pin.group(1))
        chk = check_jd(ck)
        if not chk["ok"]:
            return {"ok": False, "status": "error",
                    "msg": "扫码得到的 CK 校验未通过：%s" % chk.get("msg", "")}
        try:
            detail = push_to_ql(ck, m_pin.group(1), chk.get("nickname") or "")
        except RuntimeError as e:
            return {"ok": False, "status": "error",
                    "msg": "CK 校验通过但写入青龙失败：%s" % e, "pin": pin}
        return {"ok": True, "status": "confirmed", "nickname": chk.get("nickname"),
                "pin": pin, "detail": detail}
    if code == 201:
        return {"ok": False, "status": "waiting", "msg": "等待扫码"}
    if code == 176:
        return {"ok": False, "status": "scanned", "msg": "已扫描，请在手机上点「确认登录」"}
    # 202/257 等均视为过期/异常
    return {"ok": False, "status": "expired",
            "msg": "二维码已失效（%s），请重新生成" % data.get("msg", code)}


# ---------------------------------------------------------------- 青龙 Open API

def ql_token():
    if not QL_CLIENT_ID or not QL_CLIENT_SECRET:
        raise RuntimeError("未配置 QL_CLIENT_ID / QL_CLIENT_SECRET，请先在青龙『系统设置-应用设置』创建应用")
    url = "%s/open/auth/token?client_id=%s&client_secret=%s" % (
        QL_URL, quote(QL_CLIENT_ID), quote(QL_CLIENT_SECRET))
    res = http_json("GET", url)
    if res.get("code") == 200 and (res.get("data") or {}).get("token"):
        return res["data"]["token"]
    raise RuntimeError("获取青龙 Token 失败: %s" % json.dumps(res, ensure_ascii=False)[:300])


def ql_api(method, path, token, payload=None):
    res = http_json(method, QL_URL + path,
                    headers={"Authorization": "Bearer " + token,
                             "Content-Type": "application/json"},
                    payload=payload)
    code = res.get("code")
    if code not in (200, 0):
        detail = json.dumps(res, ensure_ascii=False)[:300]
        raw = res.get("raw") if isinstance(res, dict) else None
        if isinstance(raw, str) and "Cannot " in raw:
            detail = "青龙没有 %s %s 这个接口（请求方法或路径不匹配）" % (method, path)
        raise RuntimeError("青龙接口 %s %s 返回异常: %s" % (method, path, detail))
    return res


def ql_enable(token, ids):
    """启用环境变量：青龙 Open API 是 PUT /open/envs/enable（不是 POST）。

    这一步是 best-effort：新建的变量默认就是启用状态，个别青龙版本
    路由差异或权限不足时，不应该让整个提交变成失败。
    """
    try:
        ql_api("PUT", "/open/envs/enable", token, list(ids))
        return True
    except RuntimeError:
        return False


def pin_of(value):
    m = re.search(r"pt_pin=([^;\s\"']+)", value or "")
    return m.group(1) if m else ""


def list_envs(token):
    res = ql_api("GET", "/open/envs?searchValue=%s" % quote(ENV_NAME), token)
    items = res.get("data") or []
    return [e for e in items if e.get("name") == ENV_NAME]


def split_merge_value(value):
    """合并模式下把一个变量的值按行拆成多账号"""
    return [x.strip() for x in re.split(r"[\n&]+", value or "") if x.strip()]


def accounts_overview(token):
    """返回当前账号列表（pin、备注、状态），同时兼容两种模式"""
    envs = list_envs(token)
    accounts = []
    if MODE == "merge":
        for env in envs:
            for line in split_merge_value(env.get("value")):
                accounts.append({
                    "pin": unquote(pin_of(line)),
                    "remarks": env.get("remarks") or "",
                    "enabled": env.get("status") == 0,
                })
    else:
        for env in envs:
            accounts.append({
                "pin": unquote(pin_of(env.get("value"))),
                "remarks": env.get("remarks") or "",
                "enabled": env.get("status") == 0,
            })
    return accounts


def account_pins(token):
    """读取面板当前所有 JD 账号的 pin 列表（去重保序）"""
    envs = list_envs(token)
    pins = []
    if MODE == "merge":
        for env in envs:
            for line in split_merge_value(env.get("value")):
                pins.append(unquote(pin_of(line)))
    else:
        for env in envs:
            pins.append(unquote(pin_of(env.get("value"))))
    return list(dict.fromkeys(p for p in pins if p))


def _summary_msg(token):
    """提交后核对：把面板当前账号数写进结果，覆盖/丢失立刻可见"""
    try:
        pins = account_pins(token)
    except RuntimeError:
        return ""  # 核对失败不影响提交结果
    if not pins:
        return ""
    return "核对：面板现有 %d 个账号（%s）" % (len(pins), "、".join(pins[:6]) + ("…" if len(pins) > 6 else ""))


def push_to_ql(ck, pin, nickname):
    """把清洗后的 ck 推送到青龙。返回操作说明文字。"""
    token = ql_token()
    remarks = nickname or unquote(pin)
    msgs = []

    if MODE == "merge":
        envs = list_envs(token)
        env = envs[0] if envs else None
        if env is None:
            ql_api("POST", "/open/envs", token,
                   [{"name": ENV_NAME, "value": ck, "remarks": remarks}])
            msgs.append("已创建变量 %s（合并模式）" % ENV_NAME)
        else:
            lines = split_merge_value(env.get("value"))
            replaced = False
            for i, line in enumerate(lines):
                if pin_of(line) == pin:
                    lines[i] = ck
                    replaced = True
            if not replaced:
                lines.append(ck)
            ql_api("PUT", "/open/envs", token, {
                "id": env["id"], "name": ENV_NAME,
                "value": "\n".join(lines),
                "remarks": env.get("remarks") or remarks})
            msgs.append("已更新%s账号（合并模式，共 %d 个账号）" % (
                "该" if replaced else "并新增", len(lines)))
        msgs.append(_summary_msg(token))
        return "; ".join(m for m in msgs if m)

    # multi 模式：一账号一个变量
    envs = list_envs(token)
    target = None
    for env in envs:
        if pin_of(env.get("value")) == pin:
            target = env
            break
    if target:
        ql_api("PUT", "/open/envs", token, {
            "id": target["id"], "name": ENV_NAME, "value": ck,
            "remarks": target.get("remarks") or remarks})
        msgs.append("已覆盖更新该账号变量 #id=%s" % target["id"])
        if target.get("status") != 0:
            if ql_enable(token, [target["id"]]):
                msgs.append("该账号原来是禁用状态，已重新启用")
            else:
                msgs.append("⚠️ 自动启用未成功，若青龙面板里仍是禁用状态请手动点启用")
    else:
        res = ql_api("POST", "/open/envs", token,
                     [{"name": ENV_NAME, "value": ck, "remarks": remarks}])
        created = res.get("data") or [{}]
        new_id = created[0].get("id")
        msgs.append("新增账号变量成功%s" % ("（#id=%s）" % new_id if new_id else ""))
        # 青龙新建变量默认即启用；这里只做兜底，失败不影响提交结果
        if new_id:
            ql_enable(token, [new_id])
    msgs.append(_summary_msg(token))
    return "; ".join(m for m in msgs if m)


# ---------------------------------------------------------------- 访问认证

def _auth_sign(exp):
    return hmac.new(AUTH_SECRET, str(exp).encode("utf-8"),
                    hashlib.sha256).hexdigest()[:32]


def _auth_token():
    exp = int(time.time()) + AUTH_TTL
    return "%d.%s" % (exp, _auth_sign(exp))


def _auth_ok(cookie_header):
    if not AUTH_ENABLED:
        return True
    if not cookie_header:
        return False
    for part in cookie_header.split(";"):
        name, _, value = part.strip().partition("=")
        if name != AUTH_COOKIE:
            continue
        exp, _, sig = value.partition(".")
        if not exp.isdigit() or not sig:
            return False
        if int(exp) < time.time():
            return False
        return hmac.compare_digest(sig, _auth_sign(exp))
    return False


def _login_allowed(ip):
    with LOGIN_LOCK:
        rec = LOGIN_FAILS.get(ip)
        if rec and rec[0] >= LOGIN_MAX_FAILS and time.time() < rec[1]:
            return False, "失败次数过多，已临时锁定，请 %d 秒后再试" % int(rec[1] - time.time())
    return True, ""


def _login_fail(ip):
    with LOGIN_LOCK:
        n, _ = LOGIN_FAILS.get(ip, (0, 0))
        n += 1
        LOGIN_FAILS[ip] = (n, time.time() + LOGIN_LOCK_SECS if n >= LOGIN_MAX_FAILS else 0)


def _login_ok(ip):
    with LOGIN_LOCK:
        LOGIN_FAILS.pop(ip, None)


LOGIN_PAGE = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>JD CK 助手 · 登录</title>
<style>
  body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
       background:linear-gradient(135deg,#e1251b,#ff5f52);font:-apple-system,BlinkMacSystemFont,
       "PingFang SC","Segoe UI",Roboto,sans-serif}
  .box{background:#fff;border-radius:16px;padding:28px 22px;width:86%;max-width:340px;
       box-shadow:0 12px 40px rgba(0,0,0,.25)}
  h1{margin:0 0 4px;font-size:20px;color:#1c2330}
  p{margin:0 0 18px;font-size:13px;color:#7a8496}
  input{width:100%;box-sizing:border-box;border:1px solid #e7ebf1;border-radius:10px;
        padding:12px;font-size:16px;background:#fbfcfe;color:#1c2330}
  input:focus{outline:2px solid #ffc9c5;border-color:#e1251b}
  button{margin-top:12px;width:100%;border:0;border-radius:10px;padding:12px;
         font-size:15px;font-weight:600;background:#e1251b;color:#fff;cursor:pointer}
  button:active{transform:scale(.98)}
  .err{margin-top:10px;font-size:13px;color:#b32017;display:none}
</style>
</head>
<body>
<div class="box">
  <h1>🍪 JD CK 助手</h1>
  <p>请输入访问密码</p>
  <form onsubmit="return go(event)">
    <input id="pw" type="password" placeholder="访问密码" autocomplete="current-password">
    <button type="submit">登 录</button>
  </form>
  <div class="err" id="err"></div>
</div>
<script>
function go(e){
  e.preventDefault();
  fetch('/api/login',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({pw:document.getElementById('pw').value})})
    .then(r=>r.json())
    .then(d=>{
      if(d.ok){
        // 保留 #ck= 片段（书签回跳时带到首页继续自动提交）
        location.href='/'+location.hash;
      }else{
        const el=document.getElementById('err');
        el.style.display='block';el.textContent='❌ '+(d.msg||'密码错误');
      }
    })
    .catch(()=>{const el=document.getElementById('err');
      el.style.display='block';el.textContent='❌ 网络错误，请重试';});
  return false;
}
</script>
</body>
</html>
"""


# ---------------------------------------------------------------- HTTP 服务

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        sys.stderr.write("[jd-ck] %s - %s\n" % (self.address_string(), fmt % args))

    # ---------- helpers ----------
    def _json(self, obj, status=200):
        self._send(status, json.dumps(obj, ensure_ascii=False))

    def _body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        raw = self.rfile.read(n) if n > 0 else b"{}"
        try:
            return json.loads(raw.decode("utf-8", "ignore") or "{}")
        except (ValueError, TypeError):
            return {}

    # ---------- auth ----------
    def _authed(self):
        return _auth_ok(self.headers.get("Cookie"))

    def _guard(self):
        """未认证时返回 True 并已发送响应（页面跳登录页，接口返回 401）"""
        if self._authed():
            return False
        if self.path.split("?")[0].startswith("/api/"):
            self._json({"ok": False, "msg": "未登录或登录已过期"}, 401)
        else:
            self._send(302, "", "text/plain; charset=utf-8",
                       extra=[("Location", "/login")])
        return True

    def _send(self, status, content, ctype="application/json; charset=utf-8",
              extra=None):
        data = content.encode("utf-8") if isinstance(content, str) else content
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or []):
            self.send_header(k, v)
        self.end_headers()
        if status not in (204, 302):
            self.wfile.write(data)

    # ---------- routes ----------
    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/login":
            if self._authed():
                self._send(302, "", "text/plain; charset=utf-8",
                           extra=[("Location", "/")])
            else:
                self._send(200, LOGIN_PAGE, "text/html; charset=utf-8")
            return
        if path == "/logout":
            self._send(302, "", "text/plain; charset=utf-8", extra=[
                ("Location", "/login"),
                ("Set-Cookie",
                 "%s=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax" % AUTH_COOKIE)])
            return
        if AUTH_ENABLED and self._guard():
            return
        if path in ("/", "/index.html"):
            html = PAGE.replace("__ENV_NAME__", ENV_NAME).replace("__MODE__", MODE)
            html = html.replace("__AUTH__", "1" if AUTH_ENABLED else "0")
            self._send(200, html, "text/html; charset=utf-8")
        elif path == "/api/status":
            self._json({
                "ql_configured": bool(QL_CLIENT_ID and QL_CLIENT_SECRET),
                "ql_url": QL_URL, "env_name": ENV_NAME, "mode": MODE,
            })
        elif path == "/api/accounts":
            try:
                token = ql_token()
                self._json({"ok": True, "accounts": accounts_overview(token)})
            except RuntimeError as e:
                self._json({"ok": False, "msg": str(e)})
        elif path == "/api/qrcode":
            try:
                sid, img = qr_new_session()
                self._json({"ok": True, "sid": sid,
                            "img": "data:image/png;base64," + _b64(img)})
            except (RuntimeError, HTTPError, URLError, OSError) as e:
                self._json({"ok": False, "msg": "获取二维码失败：%s" % e})
        elif path == "/api/qrcheck":
            sid = (self.path.split("sid=", 1)[1].split("&")[0]
                   if "sid=" in self.path else "")
            self._json(qr_poll(sid))
        else:
            self._send(404, "not found", "text/plain; charset=utf-8")

    def do_POST(self):
        path = self.path.split("?")[0]
        body = self._body()
        if path == "/api/login":
            ip = self.address_string()
            allowed, why = _login_allowed(ip)
            if not allowed:
                self._json({"ok": False, "msg": why}, 429)
                return
            pw = body.get("pw", "")
            if isinstance(pw, str) and pw and hmac.compare_digest(
                    hashlib.sha256(pw.encode("utf-8")).hexdigest(),
                    hashlib.sha256(ACCESS_PASSWORD.encode("utf-8")).hexdigest()):
                _login_ok(ip)
                self._send(200, '{"ok": true}', "application/json; charset=utf-8",
                           extra=[("Set-Cookie",
                                   "%s=%s; Path=/; Max-Age=%d; HttpOnly; SameSite=Lax"
                                   % (AUTH_COOKIE, _auth_token(), AUTH_TTL))])
            else:
                _login_fail(ip)
                self._json({"ok": False, "msg": "密码错误"}, 401)
            return
        if AUTH_ENABLED and self._guard():
            return
        if path == "/api/check":
            ck, pin, err = parse_ck(body.get("ck", ""))
            if err:
                self._json({"ok": False, "msg": err})
                return
            if not ck:
                self._json({"ok": False, "msg": "未找到 pt_key / pt_pin，请粘贴完整的 Cookie"})
                return
            result = check_jd(ck)
            result["pin"] = unquote(pin)
            result["ck"] = ck
            self._json(result)
        elif path == "/api/push":
            ck, pin, err = parse_ck(body.get("ck", ""))
            if err:
                self._json({"ok": False, "msg": err})
                return
            if not ck:
                self._json({"ok": False, "msg": "未找到 pt_key / pt_pin，请粘贴完整的 Cookie"})
                return
            # 提交前先校验，避免把失效 CK 推进青龙
            chk = check_jd(ck)
            if not chk["ok"]:
                self._json({"ok": False,
                            "msg": "该 CK 校验未通过（%s），未提交到青龙。请重新获取。"
                                   % chk.get("msg", "")})
                return
            try:
                detail = push_to_ql(ck, pin, chk.get("nickname") or "")
                self._json({"ok": True, "nickname": chk.get("nickname"),
                            "detail": detail})
            except RuntimeError as e:
                self._json({"ok": False, "msg": str(e)})
        else:
            self._send(404, "not found", "text/plain; charset=utf-8")


# ---------------------------------------------------------------- 前端页面

PAGE = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="JD CK助手">
<title>JD CK 助手</title>
<style>
  :root{--bg:#f5f7fa;--card:#fff;--ink:#1c2330;--sub:#7a8496;--brand:#e1251b;
        --ok:#0aa06e;--warn:#d9822b;--line:#e7ebf1;--radius:14px}
  *{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
  body{margin:0;background:var(--bg);color:var(--ink);
       font:-apple-system,BlinkMacSystemFont,"PingFang SC","Segoe UI",Roboto,sans-serif;
       padding-bottom:calc(24px + env(safe-area-inset-bottom))}
  header{background:linear-gradient(135deg,#e1251b,#ff5f52);color:#fff;
         padding:20px 18px 16px}
  header h1{margin:0;font-size:20px;letter-spacing:.5px}
  header p{margin:6px 0 0;font-size:12px;opacity:.85}
  main{max-width:520px;margin:0 auto;padding:0 14px}
  .card{background:var(--card);border-radius:var(--radius);
        box-shadow:0 1px 3px rgba(16,24,40,.06);padding:14px;margin-top:14px}
  .card h2{font-size:15px;margin:0 0 10px}
  .row{display:flex;align-items:center;gap:8px}
  .pill{font-size:12px;padding:3px 10px;border-radius:999px;background:#fdecea;color:var(--brand)}
  .pill.ok{background:#e6f6f0;color:var(--ok)}
  textarea{width:100%;min-height:96px;border:1px solid var(--line);border-radius:10px;
           padding:10px;font-size:13px;font-family:ui-monospace,Menlo,monospace;
           background:#fbfcfe;color:var(--ink);resize:vertical}
  textarea:focus{outline:2px solid #ffc9c5;border-color:var(--brand)}
  .btn{appearance:none;border:0;border-radius:10px;padding:12px 16px;font-size:15px;
       font-weight:600;width:100%;cursor:pointer}
  .btn.primary{background:var(--brand);color:#fff}
  .btn.ghost{background:#fff;color:var(--brand);border:1px solid #ffc9c5}
  .btn:active{transform:scale(.98)}
  .btn[disabled]{opacity:.55}
  .grid2{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:10px}
  .preview{margin-top:10px;font-size:13px;color:var(--sub);word-break:break-all}
  .preview b{color:var(--ink)}
  .msg{margin-top:10px;font-size:13px;line-height:1.6;padding:10px 12px;border-radius:10px;display:none}
  .msg.ok{display:block;background:#e6f6f0;color:#077a55}
  .msg.err{display:block;background:#fdecea;color:#b32017}
  .tabs{display:flex;gap:8px;margin-bottom:10px}
  .tab{flex:1;text-align:center;padding:8px 0;border-radius:9px;font-size:13px;
       background:#f1f3f7;color:var(--sub);cursor:pointer}
  .tab.on{background:var(--brand);color:#fff;font-weight:600}
  ol{margin:8px 0 0;padding-left:20px;font-size:13.5px;line-height:1.9;color:#333c4e}
  ol li b{color:var(--ink)}
  .tip{font-size:12px;color:var(--sub);margin-top:8px;line-height:1.6}
  .acc{display:flex;justify-content:space-between;align-items:center;
       padding:9px 2px;border-bottom:1px dashed var(--line);font-size:13px}
  .acc:last-child{border-bottom:0}
  .acc .name{font-weight:600;max-width:70%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .acc .meta{color:var(--sub);font-size:12px;margin-left:8px;font-weight:400}
  .dot{width:8px;height:8px;border-radius:50%;display:inline-block;margin-right:6px}
  .dot.on{background:var(--ok)}.dot.off{background:#c2c8d3}
  .spin{display:inline-block;animation:r 1s linear infinite}
  @keyframes r{to{transform:rotate(360deg)}}
  code.k{background:#f1f3f7;border-radius:5px;padding:1px 5px;font-size:12px}
  #qrBox{text-align:center;margin:8px 0}
  #qrBox img{width:220px;height:220px;border:1px solid var(--line);border-radius:10px;
             background:#fff;padding:6px}
  #qrMsg{font-size:13px;margin-top:6px;color:var(--sub);min-height:20px}
  #qrMsg.ok{color:var(--ok);font-weight:600}
  #qrMsg.err{color:#b32017}
  .qrsteps{font-size:13px;color:#333c4e;line-height:1.8;margin:4px 0 2px}
</style>
</head>
<body>
<header>
  <h1>🍪 JD CK 助手</h1>
  <p>变量 <span id="envName">__ENV_NAME__</span> · 模式 <span id="modeName">__MODE__</span>
     <a id="logoutLink" href="/logout" style="display:none;color:#fff;opacity:.85;margin-left:8px;font-size:12px">退出登录</a></p>
</header>
<main>

  <div class="card">
    <div class="row" style="justify-content:space-between">
      <h2 style="margin:0">青龙面板状态</h2>
      <button class="pill" id="qlStatus" onclick="loadAccounts()">检查中…</button>
    </div>
    <div id="accList" class="tip">已提交账号：加载中…</div>
  </div>

  <div class="card">
    <h2>① 扫码登录（身边有两台设备时最方便）</h2>
    <div class="qrsteps">点「生成二维码」→ 手机京东 App 里 <b>扫一扫</b>（首页右上角 +/- 号进入）→ 手机上点 <b>确认登录</b> → 自动校验并提交青龙。<b>换账号</b>：切换京东 App 登录的账号（或换家人手机）后重新扫码即可。</div>
    <div class="grid2">
      <button class="btn primary" id="qrBtn" onclick="startQr()">生成二维码</button>
      <button class="btn ghost" onclick="startQr()">刷新二维码</button>
    </div>
    <div id="qrBox" style="display:none"><img id="qrImg" alt="京东登录二维码"></div>
    <div id="qrMsg"></div>
  </div>

  <div class="card">
    <h2>② 短信验证码登录 · 一键提取（单手机推荐）</h2>
    <div class="qrsteps">1. 手机浏览器打开 <b>m.jd.com</b> → 选择 <b>「手机验证码登录」</b>（偶尔出滑块验证，正常拖过即可）<br>2. 登录成功后，停在京东任意页面，点一下收藏夹里的 <b>「提取CK」</b> 书签<br>3. 自动跳回本页并提交青龙，<b>全程无需复制粘贴</b></div>
    <div style="margin-top:10px"><textarea id="bmCode" readonly style="min-height:64px;font-size:11px"></textarea></div>
    <div class="grid2">
      <button class="btn ghost" onclick="copyBm()">复制书签代码</button>
      <button class="btn primary" onclick="gotoJd()">去 m.jd.com 登录</button>
    </div>
    <div id="bmMsg" class="tip" style="color:var(--brand)"></div>
    <div class="tip"><b>首次配置书签（iOS Safari）：</b>随便把任何页面「添加到书签」→ 打开「编辑书签」→ 清空网址栏，粘贴上面复制的那段代码，名称改为「提取CK」。Alook / 安卓浏览器同理（添加书签时直接粘贴这段代码作为网址）。</div>
  </div>

  <div class="card">
    <h2>③ 手动粘贴 Cookie（备用）</h2>
    <textarea id="ckInput" placeholder="把整段 Cookie 粘贴到这里（乱七八糟没关系，会自动只保留 pt_key / pt_pin）"></textarea>
    <div class="preview" id="preview">识别到账号：<b>—</b></div>
    <div class="grid2">
      <button class="btn ghost" onclick="doCheck()">检测有效性</button>
      <button class="btn primary" onclick="doPush()">一键提交到青龙</button>
    </div>
    <div class="msg" id="msg"></div>
  </div>

  <div class="card">
    <h2>④ 开发者工具 / 抓包取 CK（备用）</h2>
    <div class="tabs">
      <div class="tab on" id="tabIos" onclick="switchTab('ios')">iPhone</div>
      <div class="tab" id="tabAnd" onclick="switchTab('android')">安卓</div>
    </div>
    <div id="guide-ios">
      <ol>
        <li><b>推荐（最简单）：</b>App Store 安装 <b>Alook 浏览器</b></li>
        <li>打开 <b>m.jd.com</b> → 用<b>手机短信验证码</b>登录（比密码登录 CK 更耐用）</li>
        <li>底部 <b>工具箱 → 开发者工具 → Cookies</b>，复制完整 Cookie</li>
        <li>回到本页粘贴，点「检测」再「提交到青龙」即可</li>
      </ol>
      <div class="tip">进阶：想要<b>更耐用的 App CK</b>，可用 Stream / HTTP Catcher 等抓包工具，抓京东 App 任意 <code class="k">api.m.jd.com</code> 请求头里的 Cookie（含 pt_key / pt_pin）。</div>
    </div>
    <div id="guide-android" style="display:none">
      <ol>
        <li>安卓 <b>Alook 浏览器</b>（免费）：m.jd.com 短信登录 → 工具箱 → 开发者工具 → Cookies 复制</li>
        <li>或电脑浏览器访问 <b>m.jd.com</b> → F12 → Application → Cookies → 复制 <code class="k">pt_key</code> / <code class="k">pt_pin</code></li>
        <li>回到本页粘贴 → 检测 → 提交</li>
      </ol>
      <div class="tip">App CK：HttpCanary / Reqable 抓包京东 App 的 <code class="k">api.m.jd.com</code> 请求，复制 Cookie 头。</div>
    </div>
  </div>

  <div class="card">
    <h2>为什么 CK 老是掉？</h2>
    <div class="tip" style="font-size:13px;margin-top:0">
      · 网页密码登录的 CK 有效期短；<b>短信验证码登录</b>明显更久<br>
      · 同一账号在别处频繁登录会互踢，建议专门用一台设备登录取 CK<br>
      · App 抓包的 CK（App CK）比网页 CK 耐用得多<br>
      · 本工具提交前会先向京东官方接口校验，无效 CK 不会写进青龙
    </div>
  </div>

</main>
<script>
const $=id=>document.getElementById(id);
const envName='__ENV_NAME__';

function switchTab(t){
  $('tabIos').classList.toggle('on',t==='ios');
  $('tabAnd').classList.toggle('on',t==='android');
  $('guide-ios').style.display=t==='ios'?'':'none';
  $('guide-android').style.display=t==='android'?'':'none';
}

function parseCk(){
  const t=$('ckInput').value||'';
  const k=t.match(/pt_key=([^;\s"']+)/), p=t.match(/pt_pin=([^;\s"']+)/);
  const ok=k&&p;
  let pin='-';
  if(p){try{pin=decodeURIComponent(p[1])}catch(e){pin=p[1]}}
  $('preview').innerHTML='识别到账号：<b>'+esc(pin)+'</b>'+(ok?'':'（缺少 pt_key/pt_pin，请粘贴完整 Cookie）');
  return ok?{ck:'pt_key='+k[1]+';pt_pin='+p[1]+';',pin:pin}:null;
}
function esc(s){return String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}

function showMsg(ok,text){const m=$('msg');m.className='msg '+(ok?'ok':'err');m.innerHTML=text}

async function api(path,body){
  const r=await fetch(path,body?{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}:{});
  if(r.status===401){location.href='/login'+location.hash;throw new Error('登录已过期，正在跳转登录页')}
  return r.json();
}

async function doCheck(){
  const c=parseCk(); if(!c){showMsg(false,'❌ 未识别到有效的 pt_key / pt_pin');return}
  showMsg('','');$('msg').className='msg';$('msg').style.display='block';
  $('msg').innerHTML='⏳ 正在向京东官方接口校验…';
  try{
    const r=await api('/api/check',{ck:$('ckInput').value});
    if(r.ok){showMsg(true,'✅ CK 有效！昵称：<b>'+esc(r.nickname)+'</b>（账号 '+esc(r.pin)+'）')}
    else{showMsg(false,'❌ CK 无效：'+esc(r.msg||'未知错误'))}
  }catch(e){showMsg(false,'❌ 请求失败：'+esc(e.message))}
}

async function doPush(auto){
  const c=parseCk(); if(!c){showMsg(false,'❌ 未识别到有效的 pt_key / pt_pin');return}
  if(!auto && !confirm('确认把该 CK 提交到青龙（变量 '+envName+'）？'))return;
  $('msg').className='msg';$('msg').style.display='block';
  $('msg').innerHTML='⏳ 校验中并提交中…';
  try{
    const r=await api('/api/push',{ck:$('ckInput').value});
    if(r.ok){showMsg(true,'🎉 提交成功！（'+esc(r.nickname||'')+'）<br>'+esc(r.detail));
      $('ckInput').value='';parseCk();loadAccounts()}
    else{showMsg(false,'❌ 提交失败：'+esc(r.msg))}
  }catch(e){showMsg(false,'❌ 请求失败：'+esc(e.message))}
}

/* ---------- 扫码登录 ---------- */
let qrTimer=null,qrSid='',qrBusy=false;
function qrShow(ok,text){
  const m=$('qrMsg');
  m.className=ok===null?'':(ok?'ok':'err');
  m.innerHTML=text;
}
async function startQr(){
  if(qrBusy)return; qrBusy=true;
  stopQr(); qrShow(null,'⏳ 正在向京东申请二维码…');
  try{
    const r=await api('/api/qrcode');
    if(!r.ok){qrShow(false,'❌ '+esc(r.msg));qrBusy=false;return}
    qrSid=r.sid; $('qrImg').src=r.img; $('qrBox').style.display='';
    qrShow(null,'请用手机京东 App 扫一扫（二维码约 3 分钟内有效）');
    qrTimer=setInterval(pollQr,2000);
  }catch(e){qrShow(false,'❌ '+esc(e.message))}
  qrBusy=false;
}
function stopQr(){if(qrTimer){clearInterval(qrTimer);qrTimer=null}}
async function pollQr(){
  if(!qrSid)return;
  let r;
  try{r=await api('/api/qrcheck?sid='+encodeURIComponent(qrSid))}
  catch(e){return}
  if(r.status==='waiting'){qrShow(null,'📷 等待扫码…')}
  else if(r.status==='scanned'){qrShow(null,'✅ 已扫描，请在手机上点「确认登录」')}
  else if(r.status==='confirmed'){
    stopQr();qrSid='';
    qrShow(true,'🎉 登录成功！账号 <b>'+esc(r.nickname||r.pin)+'</b> 已自动提交青龙<br>'+esc(r.detail));
    loadAccounts();
  }else{
    stopQr();qrSid='';
    qrShow(false,'⚠️ '+esc(r.msg||'二维码已失效')+'，请点「生成二维码」重试');
  }
}

async function loadAccounts(){
  try{
    const s=await api('/api/status');
    if(!s.ql_configured){$('qlStatus').textContent='未配置';$('qlStatus').className='pill';
      $('accList').innerHTML='⚠️ 请先在服务端配置 QL_CLIENT_ID / QL_CLIENT_SECRET';return}
    const r=await api('/api/accounts');
    if(!r.ok){$('qlStatus').textContent='连接失败';$('qlStatus').className='pill';
      $('accList').innerHTML='❌ '+esc(r.msg);return}
    $('qlStatus').textContent='已连接';$('qlStatus').className='pill ok';
    const a=r.accounts||[];
    $('accList').innerHTML=a.length?a.map(x=>
      '<div class="acc"><span><span class="dot '+(x.enabled?'on':'off')+'"></span><span class="name">'+esc(x.pin)+'</span></span>'+
      '<span class="meta">'+esc(x.remarks||'')+' · '+(x.enabled?'已启用':'已禁用')+'</span></div>'
    ).join(''):'还没有任何账号，提交第一个吧 👉';
  }catch(e){$('qlStatus').textContent='连接失败';$('accList').innerHTML='❌ '+esc(e.message)}
}

/* ---------- 一键提取书签 ---------- */
function initBm(){
  const base=location.protocol+'//'+location.host;
  $('bmCode').value="javascript:(function(){var c=document.cookie,k=c.match(/pt_key=([^;]+)/),p=c.match(/pt_pin=([^;]+)/);if(!k||!p){alert('还没读到京东CK：请先在 m.jd.com 完成登录后再点我。若登录后仍不行，请改用 Alook 浏览器（开发者工具-Cookies 复制）。');return;}location.href='"+base+"/#ck='+encodeURIComponent('pt_key='+k[1]+';pt_pin='+p[1]+';');})();";
}
function showBm(t){$('bmMsg').textContent=t}
async function copyBm(){
  const t=$('bmCode');t.focus();t.select();
  let done=false;
  try{await navigator.clipboard.writeText(t.value);done=true}catch(e){
    try{done=document.execCommand('copy')}catch(e2){}
  }
  showBm(done?'✅ 已复制！去「添加/编辑书签」，把这段代码粘贴到网址栏，名称改为「提取CK」':'复制失败，请长按代码框全选后手动复制');
  if(done)setTimeout(()=>showBm(''),6000);
}
function gotoJd(){location.href='https://m.jd.com/'}
function checkHash(){
  const m=location.hash.match(/[#&]ck=([^&]+)/);
  if(!m)return false;
  let ck;try{ck=decodeURIComponent(m[1])}catch(e){ck=m[1]}
  history.replaceState(null,'',location.pathname);
  $('ckInput').value=ck;parseCk();
  return true;
}

$('ckInput').addEventListener('input',parseCk);
initBm();
if('__AUTH__'==='1'){$('logoutLink').style.display=''}
loadAccounts();
if(checkHash()){
  showMsg('','');$('msg').className='msg';$('msg').style.display='block';
  if(/jd\.com/.test(document.referrer||'')){
    $('msg').innerHTML='✅ 书签已提取到 CK（来自京东页面），正在自动提交青龙…';
    doPush(true);
  }else{
    $('msg').innerHTML='已自动填入 CK，请点下方「一键提交到青龙」';
  }
}
</script>
</body>
</html>
"""


def main():
    server = ThreadingHTTPServer((BIND_HOST, PORT), Handler)
    print("[jd-ck] 服务已启动: http://%s:%d  (青龙: %s, 变量: %s, 模式: %s)" % (
        BIND_HOST, PORT, QL_URL, ENV_NAME, MODE), flush=True)
    if not (QL_CLIENT_ID and QL_CLIENT_SECRET):
        print("[jd-ck] ⚠️ 尚未配置 QL_CLIENT_ID / QL_CLIENT_SECRET")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
