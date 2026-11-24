# JD CK 助手

配合**青龙面板**使用的京东 Cookie 一键提交工具：手机浏览器打开 → 粘贴 Cookie → 自动清洗校验 → 一键写入青龙环境变量。

为 **NanoPi R2S / iStoreOS / OpenWrt** 这类软路由 Docker 环境设计：单文件 Python 标准库实现，零第三方依赖，ARM64 镜像秒级构建。iPhone / 安卓通用。

```
手机取 CK  ──▶  粘贴到本工具  ──▶  京东官方接口校验  ──▶  青龙 Open API 写入 JD_COOKIE
```

## 功能

- **① 扫码登录**：页面直接显示京东官方登录二维码，京东 App 扫一扫 + 确认，自动提取校验并提交（适合身边有两台设备时）
- **② 短信验证码 + 油猴脚本一键提取（单手机推荐）**：手机浏览器在 `m.jd.com` 用短信验证码登录（真实页面，滑块验证正常过），登录后点页面上的红色悬浮按钮「提取CK→青龙」，自动跳回本工具并提交，**全程零复制粘贴、零截图**
- **③ 手动粘贴**：整段 Cookie 直接贴进来，自动只保留 `pt_key` / `pt_pin`（备用）

> **注意**：原「一键提取书签」方案已废弃——京东把 `pt_key` 设为 HttpOnly，普通 `javascript:` 书签读不到，所以书签必然失效。改用油猴脚本（`GM_cookie` 有权限读 HttpOnly）才能真正实现一键提取。
- **多账号安全**：检测到多个账号的 Cookie 混在一起会拒绝提交（防止误覆盖）；每次提交后自动核对并报告面板账号总数
- **提交前校验**：调用京东 `me-api` 接口验证，失效 CK 不会被写进青龙
- **自动增改**：按 `pt_pin` 判断账号，已存在则覆盖更新并确保启用，不存在则新增
- **账号总览**：首页直接看已提交账号的昵称、备注、启用状态
- **登录保护（页面可设置/修改）**：用户名 + 密码登录，可在页面「安全设置」里首次设置、随时改用户名/改密码、或关闭；凭据以 PBKDF2 加盐哈希存于 `data/auth.json`，不落明文
- **零依赖**：仅 Python 标准库，镜像约 50MB

## 「油猴脚本」怎么装（一次性，1 分钟）

1. 手机浏览器安装 **Tampermonkey（油猴）** 插件：
   - 安卓：Kiwi 浏览器 / Firefox 装 Tampermonkey 扩展
   - iPhone：Safari 装「Userscripts」或直接装 Tampermonkey App（App Store 可能收费，可选）
2. 打开本工具页面，点「**安装油猴脚本**」，在弹出的页面点确认安装
   （脚本会自动带上你当前访问的地址，外网穿透也适用）
3. 以后每次：`m.jd.com` 短信登录 → 点页面右下角红色悬浮按钮「**提取CK→青龙**」→ 自动回本页提交，完成

> iPhone 若无法装油猴，请用「③ 手动粘贴」或「④ Alook / 抓包」方式，效果一样。

## 多账号怎么加

- **短信/油猴方式**：在 `m.jd.com` 退出当前账号、登录另一个账号，再点一次悬浮按钮即可（可无限重复）
- **扫码方式**：京东 App 切换账号（或换家人手机）后重新扫码
- **粘贴方式**：每次只粘贴一个账号；多个账号混贴会被拒绝
- 每次提交成功后会显示「核对：面板现有 N 个账号（xxx、yyy）」，数字不对立即可见

## 快速开始（R2S 上三条命令）

```bash
ssh root@192.168.1.1          # 登录 R2S
cd /root
git clone https://github.com/Wangzhe997/jd-ck-helper.git   # 国内慢/超时？看下方「GitHub 加速」
cd jd-ck-helper
sh deploy.sh                  # 交互式填写青龙凭据，自动构建启动
```

部署完成后手机浏览器访问 `http://R2S的IP:8787` 即可。

> `deploy.sh` 会检查 docker、生成 `.env`、提示输入青龙 Client ID / Secret，然后 `docker compose up -d --build` 并打印访问地址。更新代码只要 `git pull && docker compose up -d --build`。
>
> **GitHub 加速（国内 clone 慢或超时时用）**：把加速前缀直接拼在原链接前面即可：
> ```bash
> git clone https://ghfast.top/https://github.com/Wangzhe997/jd-ck-helper.git
> ```
> 备选前缀（某个失效就换下一个）：`https://gh-proxy.com/` · `https://ghproxy.net/`
>
> 完全不想用 git 时，也可下载压缩包（无需 git 环境），同样走加速：
> ```bash
> curl -L https://ghfast.top/https://codeload.github.com/Wangzhe997/jd-ck-helper/tar.gz/refs/heads/main -o jd-ck-helper.tgz
> tar -xzf jd-ck-helper.tgz && cd jd-ck-helper-main && sh deploy.sh
> ```

### 前置条件：青龙应用凭据

1. 青龙面板 → **系统设置 → 应用设置 → 新建应用**
2. 名称随意（如 `ck-helper`），权限至少勾选 **环境变量** 的查询 / 创建 / 修改
3. 保存后弹窗显示 **Client ID** 与 **Client Secret**（Secret 只显示一次，务必保存）

### 手动部署（不用 deploy.sh）

```bash
cp .env.example .env
vi .env                       # 填 QL_CLIENT_ID / QL_CLIENT_SECRET
docker compose up -d --build
```

不用 compose 也可以：

```bash
docker build -t jd-ck-helper .
docker run -d --name jd-ck-helper --restart unless-stopped \
  --network host \
  -e QL_URL=http://127.0.0.1:5700 \
  -e QL_CLIENT_ID=xxx -e QL_CLIENT_SECRET=xxx \
  jd-ck-helper
```

## 手机上怎么取 CK

### iPhone（推荐，全程手机完成）

**方案 A —— Alook 浏览器（最简单，App Store 约几元）**

1. App Store 安装 **Alook 浏览器**
2. 打开 `m.jd.com`，选择 **手机验证码登录**（比密码登录的 CK 耐用）
3. 底部 **工具箱 → 开发者工具 → Cookies**，复制完整 Cookie
4. 手机访问 `http://R2S的IP:8787` → 粘贴 → **检测** → **一键提交到青龙**

**方案 B —— 抓包 App（拿到 App CK，最耐用）**

用 **Stream**（免费）或 **HTTP Catcher**：启动抓包 → 安装并信任证书 → 打开京东 App 随便逛一下 → 停止 → 搜 `api.m.jd.com` → 打开任一请求 → 复制请求头 `Cookie`（含 `pt_key=...;pt_pin=...;`）→ 粘贴提交。

### 安卓

- **Alook 浏览器**（安卓免费），步骤同上方案 A
- 或 **HttpCanary / Reqable** 抓京东 App，同方案 B
- 或电脑浏览器打开 `m.jd.com` → F12 → Application → Cookies

## 为什么 CK 老是掉

| 原因 | 对策 |
| --- | --- |
| 密码登录的网页 CK 有效期短 | 改用**短信验证码登录** |
| 多设备反复登录互相踢下线 | 固定一台设备登录取 CK，其它别登 |
| 网页 CK 本身寿命有限 | 用**抓包拿到的 App CK**，常见可用数月 |
| 失效 CK 不清理，脚本一直报错 | 本工具提交前即校验，只写入有效 CK |

## 配置项（.env）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `QL_URL` | `http://127.0.0.1:5700` | 青龙地址，host 网络下本机直接填 127.0.0.1 |
| `QL_CLIENT_ID` | 空 | 青龙应用 Client ID |
| `QL_CLIENT_SECRET` | 空 | 青龙应用 Client Secret |
| `ENV_NAME` | `JD_COOKIE` | 京东 CK 变量名 |
| `MODE` | `multi` | `multi` 一账号一变量（推荐）；`merge` 合并到单变量换行分隔 |
| `PORT` | `8787` | 监听端口 |
| `BIND_HOST` | `0.0.0.0` | 监听地址 |
| `ACCESS_USERNAME` | `admin` | 仅「首次启动自动导入」用的初始用户名（之后以页面为准） |
| `ACCESS_PASSWORD` | 空 | 仅「首次启动自动导入」用的初始密码；留空=不启用。**日常请用页面的「安全设置」** |
| `DATA_DIR` | 脚本同目录 `data/` | 登录凭据目录（Docker 中固定为 `/data`，由 compose 挂卷） |
| `TZ` | `Asia/Shanghai` | 时区 |

若脚本库明确要求「单变量多账号换行分隔」，把 `MODE` 改成 `merge` 后 `docker compose up -d` 重建即可。

## 登录保护（用户名 + 密码，页面可设置/修改）

上班时 CK 掉了想在外面补救、或给家人扫码，会通过 ddnsto 之类的穿透把本工具暴露到公网。**这时必须开启登录保护**——本工具持有青龙密钥且能写入 CK，裸奔等于把账号送人。

**推荐用法：直接在页面上设置，不用改任何配置文件。**

1. 打开工具页面（内网地址即可）
2. 右上角点「🔒 设置登录保护」→ 填用户名 + 密码（至少 8 位）→ 保存
3. 立即生效：当前浏览器保持登录，其它设备需用同样的用户名和密码登录（会话 30 天）

之后随时可以在右上角「⚙️ 安全设置」里：

- **改用户名 / 改密码**：输入当前密码验证；保存后**所有已登录设备立即失效**，需重新登录
- **关闭登录保护**：输入当前密码确认（只建议纯内网用）

凭据保存在 `data/auth.json`（PBKDF2-HMAC-SHA256 加盐哈希，**不存明文**）。compose 已把 `./data` 挂成卷，重建容器不会丢。

> 若更习惯环境变量：在 `.env` 里填 `ACCESS_USERNAME` / `ACCESS_PASSWORD`，首次启动会自动导入为初始凭据；**之后在页面改的密码优先级更高**，不会被 `.env` 覆盖回去（想彻底重置就停容器、删掉 `data/auth.json`、再启动）。

实现细节：HMAC 签名的 HttpOnly Cookie（SameSite=Lax），签名密钥随密码哈希变化，所以改密码/改用户名后旧会话自动失效；连续 5 次密码错误锁定该 IP 2 分钟；未登录时所有 `/api/*` 一律 401。

> 另外两点建议：① ddnsto 本身若支持访问密码/微信验证，也一并开启（双保险）；② 穿透只映射本工具的 8787 端口，**不要**把青龙面板的 5700 也映射出去。

## 安全提醒

- 本服务持有青龙 Open API 凭据，**只在内网使用**；若要外网访问务必先开启登录保护
- **首次设置登录保护请在内网完成**：未启用时任何能访问的人都能设置，别给外人抢先「认领」的机会
- `data/auth.json` 存的是密码哈希，也别外传；忘了密码就删掉它重启（见上节）
- CK 等同于账号登录态，**不要粘贴到任何第三方在线工具**
- 青龙面板建议升级到新版并保持内网访问（2.20.2 以下版本存在已知外网漏洞）

## 目录结构

```
app.py              # 全部逻辑：HTTP 服务 + 登录认证 + CK 清洗/校验 + 青龙 Open API 调用
docker-compose.yml  # host 网络部署 + ./data 卷（存登录凭据）
Dockerfile          # python:3.12-alpine
deploy.sh           # R2S 一键部署脚本
.env.example        # 配置模板
data/auth.json      # 首次设置登录保护后自动生成（加盐哈希，勿提交/外传）
```

## License

MIT
