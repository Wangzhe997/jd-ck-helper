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
  ACCESS_USERNAME    可选，首次启动自动导入的初始用户名，默认 admin
  ACCESS_PASSWORD    可选，首次启动自动导入的初始密码。留空=不启用认证（纯内网）。
                     之后一律以页面「安全设置」保存的为准，改密码不会被环境变量覆盖
  DATA_DIR           登录凭据持久化目录，默认脚本同目录的 data/；
                     Docker 中由 compose 挂载为 /data 卷，重建容器不丢失
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
from html import escape
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

ACCESS_USERNAME = os.environ.get("ACCESS_USERNAME", "").strip() or "admin"
ACCESS_PASSWORD = os.environ.get("ACCESS_PASSWORD", "").strip()
DATA_DIR = (os.environ.get("DATA_DIR", "").strip()
            or os.path.join(os.path.dirname(os.path.abspath(__file__)), "data"))
AUTH_FILE = os.path.join(DATA_DIR, "auth.json")

# 登录会话：HMAC 签名 cookie，有效期 30 天
AUTH_TTL = 30 * 86400
AUTH_COOKIE = "jdck_auth"
PBKDF2_ITER = 150000

# 认证状态（内存镜像，持久化在 data/auth.json）
#   {"enabled": bool, "username": str, "salt": hex, "hash": hex, "iter": int}
AUTH_STATE = {"enabled": False, "username": "", "salt": "", "hash": "",
              "iter": PBKDF2_ITER}
AUTH_LOCK = threading.Lock()

# 登录失败限速：ip -> [失败次数, 锁定截止时间戳]
LOGIN_FAILS = {}
LOGIN_LOCK = threading.Lock()
LOGIN_MAX_FAILS = 5
LOGIN_LOCK_SECS = 120

UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) "
      "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1")

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

def _derive(password, salt_hex, iterations):
    """PBKDF2-HMAC-SHA256 -> hex"""
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                             bytes.fromhex(salt_hex), iterations)
    return dk.hex()


def _validate_username(username):
    if not isinstance(username, str):
        return "用户名格式错误"
    u = username.strip()
    if not (1 <= len(u) <= 32):
        return "用户名需为 1-32 个字符"
    if re.search(r"\s", u) or re.search(r"[\x00-\x1f\x7f]", u):
        return "用户名不能包含空格或控制字符"
    return ""


def _validate_password(password):
    if not isinstance(password, str) or not (8 <= len(password) <= 128):
        return "密码长度需为 8-128 位"
    return ""


def _validate_cred(username, password):
    return _validate_username(username) or _validate_password(password)


def _auth_load():
    """载入凭据：优先 data/auth.json；不存在则用 .env 的初始值自动迁移一次"""
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
    except OSError as e:
        print("[jd-ck] ⚠️ 无法创建数据目录 %s: %s" % (DATA_DIR, e), flush=True)
        return
    if os.path.exists(AUTH_FILE):
        try:
            with open(AUTH_FILE, "r", encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d, dict) and "enabled" in d:
                AUTH_STATE.update({
                    "enabled": bool(d.get("enabled")),
                    "username": str(d.get("username") or ""),
                    "salt": str(d.get("salt") or ""),
                    "hash": str(d.get("hash") or ""),
                    "iter": int(d.get("iter") or PBKDF2_ITER),
                })
                return
        except (ValueError, OSError, TypeError):
            print("[jd-ck] ⚠️ auth.json 解析失败，回退到环境变量配置", flush=True)
    # 兼容旧版：把 .env 里的 ACCESS_PASSWORD 作为初始凭据导入（仅一次）
    if ACCESS_PASSWORD:
        salt = os.urandom(16).hex()
        AUTH_STATE.update({
            "enabled": True,
            "username": ACCESS_USERNAME,
            "salt": salt,
            "hash": _derive(ACCESS_PASSWORD, salt, PBKDF2_ITER),
            "iter": PBKDF2_ITER,
        })
        try:
            _auth_save()
            print("[jd-ck] 已把 .env 中的 ACCESS_PASSWORD 导入 %s" % AUTH_FILE,
                  flush=True)
        except OSError as e:
            print("[jd-ck] ⚠️ 写入 %s 失败（本次仍生效，重启需重新导入）: %s"
                  % (AUTH_FILE, e), flush=True)


def _auth_save():
    """原子写入，避免意外断电写坏；Linux 下权限 0600"""
    os.makedirs(DATA_DIR, exist_ok=True)
    payload = {
        "enabled": AUTH_STATE["enabled"],
        "username": AUTH_STATE["username"],
        "salt": AUTH_STATE["salt"],
        "hash": AUTH_STATE["hash"],
        "iter": AUTH_STATE["iter"],
        "updated": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    tmp = AUTH_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, AUTH_FILE)


def _auth_secret():
    """签名密钥随用户名/密码哈希变化 -> 改密码或用户名即让所有旧会话失效"""
    raw = "jd-ck-helper:%s:%s" % (AUTH_STATE["username"], AUTH_STATE["hash"])
    return hashlib.sha256(raw.encode("utf-8")).digest()


def _auth_sign(exp):
    return hmac.new(_auth_secret(), str(exp).encode("utf-8"),
                    hashlib.sha256).hexdigest()[:32]


def _auth_token():
    exp = int(time.time()) + AUTH_TTL
    return "%d.%s" % (exp, _auth_sign(exp))


def _session_cookie():
    return ("%s=%s; Path=/; Max-Age=%d; HttpOnly; SameSite=Lax"
            % (AUTH_COOKIE, _auth_token(), AUTH_TTL))


def _auth_ok(cookie_header):
    if not AUTH_STATE["enabled"]:
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


def _check_pw(password):
    """校验密码（用户名需另行比较）"""
    if not AUTH_STATE["enabled"] or not isinstance(password, str):
        return False
    try:
        calc = _derive(password, AUTH_STATE["salt"], AUTH_STATE["iter"])
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(calc, AUTH_STATE["hash"])


def _set_cred(username, password):
    """新建/重置凭据（调用方需持 AUTH_LOCK）"""
    salt = os.urandom(16).hex()
    AUTH_STATE.update({
        "enabled": True,
        "username": username.strip(),
        "salt": salt,
        "hash": _derive(password, salt, PBKDF2_ITER),
        "iter": PBKDF2_ITER,
    })
    _auth_save()


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
  input+input{margin-top:10px}
  input:focus{outline:2px solid #ffc9c5;border-color:#e1251b}
  button{margin-top:12px;width:100%;border:0;border-radius:10px;padding:12px;
         font-size:15px;font-weight:600;background:#e1251b;color:#fff;cursor:pointer}
  button:active{transform:scale(.98)}
  button[disabled]{opacity:.6}
  .err{margin-top:10px;font-size:13px;color:#b32017;display:none;line-height:1.6}
  .hint{margin-top:12px;font-size:12px;color:#98a2b3;line-height:1.6}
</style>
</head>
<body>
<div class="box">
  <h1>🍪 JD CK 助手</h1>
  <p>请输入用户名和访问密码</p>
  <form onsubmit="return go(event)">
    <input id="un" type="text" placeholder="用户名" autocomplete="username"
           autocapitalize="off" autocorrect="off" spellcheck="false">
    <input id="pw" type="password" placeholder="访问密码" autocomplete="current-password">
    <button type="submit" id="btn">登 录</button>
  </form>
  <div class="err" id="err"></div>
  <div class="hint">忘记了？在路由器上查看 <code>data/auth.json</code> 所在目录，
       或删除该文件后重启容器可重置登录保护。</div>
</div>
<script>
var un=document.getElementById('un');
try{un.value=localStorage.getItem('jdck_user')||''}catch(e){}
if(!un.value){un.focus()}else{document.getElementById('pw').focus()}
function go(e){
  e.preventDefault();
  var u=un.value.trim(), p=document.getElementById('pw').value;
  var el=document.getElementById('err'), btn=document.getElementById('btn');
  if(!u||!p){el.style.display='block';el.textContent='❌ 请填写用户名和密码';return false}
  btn.disabled=true;
  fetch('/api/login',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({username:u,pw:p})})
    .then(r=>r.json())
    .then(d=>{
      btn.disabled=false;
      if(d.ok){
        try{localStorage.setItem('jdck_user',u)}catch(e){}
        // 保留 #ck= 片段（油猴脚本回跳时带到首页继续自动提交）
        location.href='/'+location.hash;
      }else{
        el.style.display='block';el.textContent='❌ '+(d.msg||'用户名或密码错误');
        document.getElementById('pw').select();
      }
    })
    .catch(()=>{btn.disabled=false;el.style.display='block';
      el.textContent='❌ 网络错误，请重试';});
  return false;
}
</script>
</body>
</html>
"""


# ---------------------------------------------------------------- 油猴脚本
# 京东 pt_key 是 HttpOnly，普通 javascript: 书签读不到；油猴的 GM_cookie 可以。
# 脚本由服务端动态生成，把本工具地址烤进去，用户只需安装一次。
USERSCRIPT = """// ==UserScript==
// @name         JD CK 助手 - 一键提取 Cookie
// @namespace    https://github.com/Wangzhe997/jd-ck-helper
// @version      1.0
// @description  在京东页面一键读取 pt_key/pt_pin（含 HttpOnly）并回传给 JD CK 助手
// @match        https://*.jd.com/*
// @match        http://*.jd.com/*
// @grant        GM_cookie
// @grant        GM_registerMenuCommand
// @run-at       document-idle
// ==/UserScript==

(function () {
  'use strict';
  var TOOL = '__BASE__';

  function extract() {
    var sent = false;
    function back(ck) {
      if (sent) return;
      sent = true;
      location.href = TOOL + '/?from=jd#ck=' + encodeURIComponent(ck);
    }
    if (typeof GM_cookie === 'undefined' || !GM_cookie.list) {
      alert('当前脚本管理器不支持 GM_cookie。请使用 Tampermonkey，并在脚本设置中允许 Cookie 权限。');
      return;
    }
    GM_cookie.list({ url: 'https://m.jd.com/' }, function (cookies, err) {
      if (err || !cookies) {
        alert('读取 Cookie 失败：' + (err && err.message || '未知错误'));
        return;
      }
      var key = '', pin = '';
      for (var i = 0; i < cookies.length; i++) {
        if (cookies[i].name === 'pt_key') key = cookies[i].value;
        if (cookies[i].name === 'pt_pin') pin = cookies[i].value;
      }
      if (!key || !pin) {
        alert('还没读到京东登录态：请先在 m.jd.com 用短信验证码登录成功后再点。');
        return;
      }
      back('pt_key=' + key + ';pt_pin=' + pin + ';');
    });
  }

  if (typeof GM_registerMenuCommand === 'function') {
    GM_registerMenuCommand('提取 CK 并提交到青龙', extract);
  }

  var btn = document.createElement('div');
  btn.textContent = '提取CK\\u2192青龙';
  btn.style.cssText = 'position:fixed;right:12px;bottom:18px;z-index:999999;' +
    'padding:10px 15px;background:#e1251b;color:#fff;border-radius:22px;' +
    'font-size:14px;line-height:1;box-shadow:0 2px 10px rgba(0,0,0,.28);' +
    'cursor:pointer;user-select:none';
  btn.addEventListener('click', extract);
  if (document.body) document.body.appendChild(btn);
})();
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
        if path == "/ck.user.js":
            # 让脚本从当前访问的 Host 自动生成回跳地址，兼容 ddnsto 穿透域名
            host = self.headers.get("Host") or ("127.0.0.1:%d" % PORT)
            proto = (self.headers.get("X-Forwarded-Proto") or "http").split(",")[0].strip()
            base = "%s://%s" % (proto, host)
            self._send(200, USERSCRIPT.replace("__BASE__", base),
                       "application/javascript; charset=utf-8")
            return
        if AUTH_STATE["enabled"] and self._guard():
            return
        if path in ("/", "/index.html"):
            html = PAGE.replace("__ENV_NAME__", ENV_NAME).replace("__MODE__", MODE)
            html = html.replace("__AUTH__", "1" if AUTH_STATE["enabled"] else "0")
            html = html.replace("__AUTH_USER__", escape(AUTH_STATE["username"]))
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
        else:
            self._send(404, "not found", "text/plain; charset=utf-8")

    def do_POST(self):
        path = self.path.split("?")[0]
        body = self._body()
        ip = self.address_string()
        if path == "/api/login":
            allowed, why = _login_allowed(ip)
            if not allowed:
                self._json({"ok": False, "msg": why}, 429)
                return
            un = body.get("username", "")
            pw = body.get("pw", "")
            ok = (AUTH_STATE["enabled"] and isinstance(un, str)
                  and isinstance(pw, str)
                  and hmac.compare_digest(un.strip(), AUTH_STATE["username"])
                  and _check_pw(pw))
            if ok:
                _login_ok(ip)
                self._send(200, '{"ok": true}', "application/json; charset=utf-8",
                           extra=[("Set-Cookie", _session_cookie())])
            else:
                _login_fail(ip)
                self._json({"ok": False, "msg": "用户名或密码错误"}, 401)
            return
        if path == "/api/auth/setup":
            allowed, why = _login_allowed(ip)
            if not allowed:
                self._json({"ok": False, "msg": why}, 429)
                return
            un = body.get("username", "")
            pw = body.get("pw", "")
            err = _validate_cred(un, pw)
            if not err and pw != body.get("pw2", ""):
                err = "两次输入的密码不一致"
            if err:
                self._json({"ok": False, "msg": err})
                return
            with AUTH_LOCK:
                if AUTH_STATE["enabled"]:
                    err = "登录保护已启用，请用「修改」功能"
                else:
                    try:
                        _set_cred(un, pw)
                        err = ""
                    except OSError as e:
                        err = "写入凭据文件失败：%s" % e
            if err:
                self._json({"ok": False, "msg": err}, 403)
                return
            _login_ok(ip)
            self._send(200, '{"ok": true}', "application/json; charset=utf-8",
                       extra=[("Set-Cookie", _session_cookie())])
            return
        if AUTH_STATE["enabled"] and self._guard():
            return
        if path == "/api/auth/change":
            allowed, why = _login_allowed(ip)
            if not allowed:
                self._json({"ok": False, "msg": why}, 429)
                return
            old = body.get("old_pw", "")
            new_un = (body.get("username") or "").strip()
            new_pw = body.get("pw") or ""
            with AUTH_LOCK:
                if not _check_pw(old):
                    err = "当前密码不正确"
                else:
                    target = new_un or AUTH_STATE["username"]
                    err = _validate_username(target)
                    if not err and new_pw:
                        err = _validate_password(new_pw)
                        if not err and new_pw != (body.get("pw2") or ""):
                            err = "两次输入的新密码不一致"
                    if not err and not new_pw and target == AUTH_STATE["username"]:
                        err = "没有需要保存的修改"
                    if not err:
                        if new_pw:
                            salt = os.urandom(16).hex()
                            AUTH_STATE.update({
                                "salt": salt,
                                "hash": _derive(new_pw, salt, PBKDF2_ITER),
                                "iter": PBKDF2_ITER,
                            })
                        AUTH_STATE["username"] = target
                        AUTH_STATE["enabled"] = True
                        try:
                            _auth_save()
                        except OSError as e:
                            err = "写入凭据文件失败：%s" % e
                if err:
                    _login_fail(ip)
            if err:
                self._json({"ok": False, "msg": err})
                return
            _login_ok(ip)
            self._send(200, '{"ok": true}', "application/json; charset=utf-8",
                       extra=[("Set-Cookie", _session_cookie())])
            return
        if path == "/api/auth/disable":
            allowed, why = _login_allowed(ip)
            if not allowed:
                self._json({"ok": False, "msg": why}, 429)
                return
            with AUTH_LOCK:
                if not _check_pw(body.get("pw", "")):
                    err = "密码不正确"
                else:
                    err = ""
                    AUTH_STATE.update({"enabled": False, "username": "",
                                       "salt": "", "hash": "",
                                       "iter": PBKDF2_ITER})
                    try:
                        _auth_save()
                    except OSError as e:
                        err = "写入凭据文件失败：%s" % e
            if err:
                _login_fail(ip)
                self._json({"ok": False, "msg": err})
                return
            _login_ok(ip)
            self._send(200, '{"ok": true}', "application/json; charset=utf-8",
                       extra=[("Set-Cookie",
                               "%s=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax"
                               % AUTH_COOKIE)])
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
  .qrsteps{font-size:13px;color:#333c4e;line-height:1.8;margin:4px 0 2px}
  .notice{border-left:4px solid var(--brand)}
  /* 方法卡片网格：宽屏两列并排，窄屏自动单列 */
  .methods{display:grid;grid-template-columns:1fr;gap:14px}
  .warnbar{margin-top:14px;background:#fff8ed;border:1px solid #ffd8a8;color:#8a5312;
           border-radius:12px;padding:12px 14px;font-size:13px;line-height:1.7}
  .warnbar b{color:#b45309}
  .warnbar .go{margin-top:9px;background:#d9822b;color:#fff;border:0;border-radius:8px;
               padding:8px 14px;font-size:13px;font-weight:600;cursor:pointer}
  .fld{margin-top:11px}
  .fld label{display:block;font-size:12px;color:var(--sub);margin-bottom:5px}
  .fld input{width:100%;box-sizing:border-box;border:1px solid var(--line);border-radius:10px;
             padding:11px;font-size:16px;background:#fbfcfe;color:var(--ink)}
  .fld input:focus{outline:2px solid #ffc9c5;border-color:var(--brand)}
  .btn.danger{background:#fff;border:1px solid #f3b6b0;color:#b32017}
  .hintbox{font-size:12px;color:var(--sub);line-height:1.7;margin-top:2px}
  .sep{margin-top:12px;border-top:1px dashed var(--line);padding-top:12px}
  .who{font-weight:600;color:var(--brand)}
  header a{color:#fff;text-decoration:underline;opacity:.9}
  /* ---------- 响应式：手机单列，平板/电脑放宽并多列，避免宽屏上挤成窄条 ---------- */
  @media(min-width:720px){
    header{padding:26px 0 22px;text-align:center}
    header h1{font-size:24px}
    header p{font-size:13px}
    main{max-width:820px;padding:0 24px}
    .card{padding:20px 22px}
    .card h2{font-size:16px}
    .methods{grid-template-columns:repeat(2,minmax(0,1fr))}
  }
  @media(min-width:1080px){
    header{padding:32px 0 26px}
    header h1{font-size:28px}
    main{max-width:1040px;padding:0 32px}
    .methods{grid-template-columns:repeat(3,minmax(0,1fr))}
    .card{padding:22px 24px}
    /* 电脑端让说明类卡片跨满整行，更易读 */
    .notice{grid-column:1/-1}
  }
</style>
</head>
<body>
<header>
  <h1>🍪 JD CK 助手</h1>
  <p>变量 <span id="envName">__ENV_NAME__</span> · 模式 <span id="modeName">__MODE__</span></p>
  <p style="margin-top:9px">
    <a href="javascript:toggleSec()" id="secLink" style="font-size:12.5px">🔒 设置登录保护</a>
    <a id="logoutLink" href="/logout" style="display:none;margin-left:14px;font-size:12.5px;opacity:.85">退出登录</a>
  </p>
</header>
<main>

  <div class="warnbar" id="secWarn" style="display:none">
    ⚠️ <b>当前未开启登录保护</b>：任何能访问这个地址的人都能写入你的京东 CK。
    若已通过 ddnsto / 端口映射暴露到外网，请立即设置用户名和密码。
    <br><button class="go" onclick="toggleSec()">立即设置 →</button>
  </div>

  <div class="card" id="secCard" style="display:none">
    <h2>🔐 登录保护</h2>

    <div id="secSetup" style="display:none">
      <div class="hintbox">首次设置：定一个用户名和强密码（至少 8 位）。保存后立即生效，
        当前浏览器会自动保持登录；其它设备需用同样的用户名和密码登录。</div>
      <div class="fld"><label>用户名</label>
        <input id="suUser" value="admin" autocapitalize="off" autocorrect="off" spellcheck="false"></div>
      <div class="fld"><label>密码（≥ 8 位）</label>
        <input id="suPw" type="password" autocomplete="new-password"></div>
      <div class="fld"><label>确认密码</label>
        <input id="suPw2" type="password" autocomplete="new-password"></div>
      <div class="msg" id="suMsg"></div>
      <div class="grid2"><button class="btn primary" onclick="setupAuth()">启用登录保护</button></div>
    </div>

    <div id="secManage" style="display:none">
      <div class="hintbox">当前用户名：<span class="who" id="curUser">__AUTH_USER__</span>。
        修改用户名或密码后，<b>所有已登录设备立即失效</b>，需要重新登录。
        只改其中一项时，另一项留空即可。</div>
      <div class="fld"><label>当前密码（必填，用于验证身份）</label>
        <input id="mgOld" type="password" autocomplete="current-password"></div>
      <div class="fld"><label>新用户名（留空 = 不修改）</label>
        <input id="mgUser" autocapitalize="off" autocorrect="off" spellcheck="false" placeholder="__AUTH_USER__"></div>
      <div class="fld"><label>新密码（留空 = 不修改）</label>
        <input id="mgPw" type="password" autocomplete="new-password"></div>
      <div class="fld"><label>确认新密码</label>
        <input id="mgPw2" type="password" autocomplete="new-password"></div>
      <div class="msg" id="mgMsg"></div>
      <div class="grid2">
        <button class="btn primary" onclick="saveAuth()">保存修改</button>
        <button class="btn ghost" onclick="showDisable()">关闭登录保护</button>
      </div>
      <div id="disableBox" class="sep" style="display:none">
        <div class="hintbox" style="color:#b32017">⚠️ 关闭后任何能访问本页的人都能写入 CK，
          仅建议在纯内网环境关闭。请输入当前密码确认。</div>
        <div class="fld"><label>当前密码</label>
          <input id="dsPw" type="password" autocomplete="current-password"></div>
        <div class="grid2"><button class="btn danger" onclick="disableAuth()">确认关闭保护</button></div>
      </div>
    </div>
  </div>

  <div class="card">
    <div class="row" style="justify-content:space-between">
      <h2 style="margin:0">青龙面板状态</h2>
      <button class="pill" id="qlStatus" onclick="loadAccounts()">检查中…</button>
    </div>
    <div id="accList" class="tip">已提交账号：加载中…</div>
  </div>

  <div class="card notice">
    <h2>📌 关于扫码登录</h2>
    <div class="tip" style="font-size:13px;margin-top:0">
      ⚠️ <b>扫码登录已移除</b>：本工具运行在服务器（软路由）上，扫码请求由服务器 IP 发出，京东会判定登录环境异常并强制滑块安全验证（<code class="k">riskCode 1100</code>），程序无法自动通过，CK 始终下发不了。为避免每次白试，已下线该功能，请用下面两种稳定方式：<b>① 油猴脚本</b> 或 <b>② 手动粘贴 / ③ 抓包</b>。
    </div>
  </div>

  <div class="methods">

  <div class="card">
    <h2>① 短信验证码登录 · 油猴脚本一键提取（单手机推荐）</h2>
    <div class="qrsteps">1. 手机浏览器装好 <b>Tampermonkey（油猴）</b> 插件<br>2. 点下面「安装油猴脚本」→ 在弹窗里确认安装（脚本会自动带上本工具地址）<br>3. 打开 <b>m.jd.com</b> → 选 <b>「手机验证码登录」</b> 登录成功<br>4. 页面上会出现红色悬浮按钮 <b>「提取CK→青龙」</b>，点它即自动跳回本页提交，<b>全程零复制粘贴</b></div>
    <div class="grid2" style="margin-top:10px">
      <button class="btn primary" id="bmInstall" onclick="installUserscript()">安装油猴脚本</button>
      <button class="btn ghost" onclick="gotoJd()">去 m.jd.com 登录</button>
    </div>
    <div id="bmMsg" class="tip" style="color:var(--brand)"></div>
    <div class="tip"><b>为什么要油猴而不是书签？</b> 京东把 <code class="k">pt_key</code> 设成了 HttpOnly，普通网页 JS（包括书签）根本读不到；只有油猴的 <code class="k">GM_cookie</code> 有权限读取，所以书签方案天生读不到 CK。<b>换账号</b>：在 m.jd.com 退出并登录另一个账号后，再点一次悬浮按钮即可。</div>
  </div>

  <div class="card">
    <h2>② 手动粘贴 Cookie（备用）</h2>
    <textarea id="ckInput" placeholder="把整段 Cookie 粘贴到这里（乱七八糟没关系，会自动只保留 pt_key / pt_pin）"></textarea>
    <div class="preview" id="preview">识别到账号：<b>—</b></div>
    <div class="grid2">
      <button class="btn ghost" onclick="doCheck()">检测有效性</button>
      <button class="btn primary" onclick="doPush()">一键提交到青龙</button>
    </div>
    <div class="msg" id="msg"></div>
  </div>

  <div class="card">
    <h2>③ 开发者工具 / 抓包取 CK（备用）</h2>
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

  </div><!-- /methods -->

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

/* ---------- 油猴脚本一键提取 ---------- */
function showBm(t){$('bmMsg').textContent=t}
function installUserscript(){
  const url=location.protocol+'//'+location.host+'/ck.user.js';
  showBm('正在打开脚本安装地址…若没有自动安装，请复制此地址到装了油猴的浏览器打开：'+url);
  window.open(url,'_blank');
}
async function copyBm(){
  const url=location.protocol+'//'+location.host+'/ck.user.js';
  let done=false;
  try{await navigator.clipboard.writeText(url);done=true}catch(e){
    try{
      const ta=document.createElement('textarea');ta.value=url;document.body.appendChild(ta);
      ta.select();done=document.execCommand('copy');document.body.removeChild(ta);
    }catch(e2){}
  }
  showBm(done?'✅ 已复制脚本地址，去装了油猴的浏览器打开安装':'复制失败，请手动打开：'+url);
  if(done)setTimeout(()=>showBm(''),8000);
}
function gotoJd(){location.href='https://m.jd.com/'}
let cameFromJd=false;
function checkHash(){
  const m=location.hash.match(/[#&]ck=([^&]+)/);
  if(!m)return false;
  cameFromJd=/[?&]from=jd\b/.test(location.search)||/jd\.com/.test(document.referrer||'');
  let ck;try{ck=decodeURIComponent(m[1])}catch(e){ck=m[1]}
  history.replaceState(null,'',location.pathname);
  $('ckInput').value=ck;parseCk();
  return true;
}

/* ---------- 登录保护：设置 / 修改 / 关闭 ---------- */
const AUTH_ON=('__AUTH__'==='1');
function toggleSec(){
  const c=$('secCard');
  const open=(c.style.display==='none');
  c.style.display=open?'':'none';
  if(open)c.scrollIntoView({behavior:'smooth',block:'start'});
}
function secMsg(id,state,text){
  const el=$(id);
  el.className='msg'+(state?' '+state:'');
  el.style.display='block';
  el.textContent=text;
}
async function secPost(path,obj){
  const r=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify(obj)});
  if(r.status===401&&AUTH_ON){location.href='/login';throw new Error('登录已过期，正在跳转登录页')}
  if(r.status===429){let d={};try{d=await r.json()}catch(e){}throw new Error(d.msg||'操作过于频繁，请稍后再试')}
  return r.json();
}
async function setupAuth(){
  const un=$('suUser').value.trim(), pw=$('suPw').value;
  if(!un){secMsg('suMsg','err','❌ 请填写用户名');return}
  if(pw.length<8){secMsg('suMsg','err','❌ 密码至少 8 位');return}
  if(pw!==$('suPw2').value){secMsg('suMsg','err','❌ 两次输入的密码不一致');return}
  secMsg('suMsg','','⏳ 正在启用…');
  try{
    const d=await secPost('/api/auth/setup',{username:un,pw:pw,pw2:$('suPw2').value});
    if(d.ok){secMsg('suMsg','ok','✅ 已启用登录保护，正在刷新…');setTimeout(()=>location.reload(),700)}
    else secMsg('suMsg','err','❌ '+(d.msg||'设置失败'));
  }catch(e){secMsg('suMsg','err','❌ '+e.message)}
}
async function saveAuth(){
  const old=$('mgOld').value, np=$('mgPw').value;
  if(!old){secMsg('mgMsg','err','❌ 请填写当前密码');return}
  if(np&&np.length<8){secMsg('mgMsg','err','❌ 新密码至少 8 位');return}
  if(np!==$('mgPw2').value){secMsg('mgMsg','err','❌ 两次输入的新密码不一致');return}
  secMsg('mgMsg','','⏳ 正在保存…');
  try{
    const d=await secPost('/api/auth/change',{old_pw:old,username:$('mgUser').value.trim(),
      pw:np,pw2:$('mgPw2').value});
    if(d.ok){secMsg('mgMsg','ok','✅ 已保存，正在刷新…');setTimeout(()=>location.reload(),700)}
    else secMsg('mgMsg','err','❌ '+(d.msg||'保存失败'));
  }catch(e){secMsg('mgMsg','err','❌ '+e.message)}
}
function showDisable(){
  const b=$('disableBox');
  b.style.display=(b.style.display==='none')?'':'none';
  if(b.style.display!=='none')$('dsPw').focus();
}
async function disableAuth(){
  const pw=$('dsPw').value;
  if(!pw){secMsg('mgMsg','err','❌ 请输入当前密码');return}
  if(!confirm('确认关闭登录保护？关闭后任何能访问本页的人都能写入你的京东 CK。'))return;
  secMsg('mgMsg','','⏳ 正在关闭…');
  try{
    const d=await secPost('/api/auth/disable',{pw:pw});
    if(d.ok){secMsg('mgMsg','ok','✅ 已关闭，正在刷新…');setTimeout(()=>location.reload(),700)}
    else secMsg('mgMsg','err','❌ '+(d.msg||'关闭失败'));
  }catch(e){secMsg('mgMsg','err','❌ '+e.message)}
}

$('ckInput').addEventListener('input',parseCk);
if(AUTH_ON){
  $('logoutLink').style.display='';
  $('secLink').textContent='⚙️ 安全设置';
  $('secManage').style.display='';
}else{
  $('secWarn').style.display='';
  $('secSetup').style.display='';
}
loadAccounts();
if(checkHash()){
  showMsg('','');$('msg').className='msg';$('msg').style.display='block';
  if(cameFromJd){
    $('msg').innerHTML='✅ 油猴脚本已提取到 CK（来自京东页面），正在自动提交青龙…';
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
    _auth_load()
    server = ThreadingHTTPServer((BIND_HOST, PORT), Handler)
    print("[jd-ck] 服务已启动: http://%s:%d  (青龙: %s, 变量: %s, 模式: %s)" % (
        BIND_HOST, PORT, QL_URL, ENV_NAME, MODE), flush=True)
    if AUTH_STATE["enabled"]:
        print("[jd-ck] 🔒 登录保护已启用（用户名: %s）" % AUTH_STATE["username"],
              flush=True)
    else:
        print("[jd-ck] ⚠️ 未启用登录保护（可在页面「安全设置」中设置，仅建议纯内网使用）",
              flush=True)
    if not (QL_CLIENT_ID and QL_CLIENT_SECRET):
        print("[jd-ck] ⚠️ 尚未配置 QL_CLIENT_ID / QL_CLIENT_SECRET")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
