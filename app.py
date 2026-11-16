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
"""
import json
import os
import re
import sys
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

UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) "
      "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1")


# ---------------------------------------------------------------- 基础工具

def parse_ck(text):
    """从任意粘贴文本中提取 pt_key / pt_pin，返回 (清洗后的ck, pin原值)"""
    if not text:
        return None, None
    m_key = re.search(r"pt_key=([^;\s\"']+)", text)
    m_pin = re.search(r"pt_pin=([^;\s\"']+)", text)
    if not m_key or not m_pin:
        return None, None
    ck = "pt_key=%s;pt_pin=%s;" % (m_key.group(1), m_pin.group(1))
    return ck, m_pin.group(1)


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


# ---------------------------------------------------------------- 京东 CK 校验

def check_jd(ck):
    """调用京东 me-api 校验 CK，返回 dict"""
    try:
        res = http_json(
            "GET",
            "https://me-api.jd.com/user_new/info/GetJDUserInfoUnion",
            headers={"Cookie": ck, "Referer": "https://home.m.jd.com/"},
        )
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
        raise RuntimeError("青龙接口 %s %s 返回异常: %s" % (
            method, path, json.dumps(res, ensure_ascii=False)[:300]))
    return res


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
        return "; ".join(msgs)

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
            ql_api("POST", "/open/envs/enable", token, [target["id"]])
            msgs.append("该账号原来是禁用状态，已重新启用")
    else:
        res = ql_api("POST", "/open/envs", token,
                     [{"name": ENV_NAME, "value": ck, "remarks": remarks}])
        created = res.get("data") or [{}]
        new_id = created[0].get("id")
        msgs.append("新增账号变量成功%s" % ("（#id=%s）" % new_id if new_id else ""))
        if new_id:
            ql_api("POST", "/open/envs/enable", token, [new_id])
            msgs.append("已确保启用")
    return "; ".join(msgs)


# ---------------------------------------------------------------- HTTP 服务

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        sys.stderr.write("[jd-ck] %s - %s\n" % (self.address_string(), fmt % args))

    # ---------- helpers ----------
    def _send(self, status, content, ctype="application/json; charset=utf-8"):
        data = content.encode("utf-8") if isinstance(content, str) else content
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

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

    # ---------- routes ----------
    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            html = PAGE.replace("__ENV_NAME__", ENV_NAME).replace("__MODE__", MODE)
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
        if path == "/api/check":
            ck, pin = parse_ck(body.get("ck", ""))
            if not ck:
                self._json({"ok": False, "msg": "未找到 pt_key / pt_pin，请粘贴完整的 Cookie"})
                return
            result = check_jd(ck)
            result["pin"] = unquote(pin)
            result["ck"] = ck
            self._json(result)
        elif path == "/api/push":
            ck, pin = parse_ck(body.get("ck", ""))
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
</style>
</head>
<body>
<header>
  <h1>🍪 JD CK 助手</h1>
  <p>变量 <span id="envName">__ENV_NAME__</span> · 模式 <span id="modeName">__MODE__</span></p>
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
    <h2>① 怎么获取 CK（手机上即可完成）</h2>
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
    <h2>② 粘贴 Cookie</h2>
    <textarea id="ckInput" placeholder="把整段 Cookie 粘贴到这里（乱七八糟没关系，会自动只保留 pt_key / pt_pin）"></textarea>
    <div class="preview" id="preview">识别到账号：<b>—</b></div>
    <div class="grid2">
      <button class="btn ghost" onclick="doCheck()">检测有效性</button>
      <button class="btn primary" onclick="doPush()">一键提交到青龙</button>
    </div>
    <div class="msg" id="msg"></div>
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

async function doPush(){
  const c=parseCk(); if(!c){showMsg(false,'❌ 未识别到有效的 pt_key / pt_pin');return}
  if(!confirm('确认把该 CK 提交到青龙（变量 '+envName+'）？'))return;
  $('msg').className='msg';$('msg').style.display='block';
  $('msg').innerHTML='⏳ 校验中并提交中…';
  try{
    const r=await api('/api/push',{ck:$('ckInput').value});
    if(r.ok){showMsg(true,'🎉 提交成功！（'+esc(r.nickname||'')+'）<br>'+esc(r.detail));loadAccounts()}
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

$('ckInput').addEventListener('input',parseCk);
loadAccounts();
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
