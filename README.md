# JD CK 助手

配合**青龙面板**使用的京东 Cookie 一键提交工具：手机浏览器打开 → 粘贴 Cookie → 自动清洗校验 → 一键写入青龙环境变量。

为 **NanoPi R2S / iStoreOS / OpenWrt** 这类软路由 Docker 环境设计：单文件 Python 标准库实现，零第三方依赖，ARM64 镜像秒级构建。iPhone / 安卓通用。

```
手机取 CK  ──▶  粘贴到本工具  ──▶  京东官方接口校验  ──▶  青龙 Open API 写入 JD_COOKIE
```

## 功能

- **① 扫码登录**：页面直接显示京东官方登录二维码，京东 App 扫一扫 + 确认，自动提取校验并提交（适合身边有两台设备时）
- **② 短信验证码一键提取（单手机推荐）**：手机浏览器在 `m.jd.com` 用短信验证码登录（真实页面，滑块验证正常过），登录后点一下预置的「提取CK」书签，自动跳回本工具并提交，**全程零复制粘贴、零截图**
- **③ 手动粘贴**：整段 Cookie 直接贴进来，自动只保留 `pt_key` / `pt_pin`（备用）
- **多账号安全**：检测到多个账号的 Cookie 混在一起会拒绝提交（防止误覆盖）；每次提交后自动核对并报告面板账号总数
- **提交前校验**：调用京东 `me-api` 接口验证，失效 CK 不会被写进青龙
- **自动增改**：按 `pt_pin` 判断账号，已存在则覆盖更新并确保启用，不存在则新增
- **账号总览**：首页直接看已提交账号的昵称、备注、启用状态
- **零依赖**：仅 Python 标准库，镜像约 50MB

## 「提取CK」书签怎么配（一次性，30 秒）

1. 打开本工具页面，点「复制书签代码」（代码里已自动带上你 R2S 的地址）
2. iOS Safari：随便收藏任意页面 → 编辑该书签 → 清空网址栏 → 粘贴刚复制的代码，名称改成「提取CK」
   Alook / 安卓浏览器：添加书签时直接把这段代码当地址填入
3. 以后每次：`m.jd.com` 短信登录 → 点「提取CK」书签 → 自动回本页提交，完成

## 多账号怎么加

- **短信/书签方式**：在 `m.jd.com` 退出当前账号、登录另一个账号，再点一次「提取CK」书签即可（可无限重复）
- **扫码方式**：京东 App 切换账号（或换家人手机）后重新扫码
- **粘贴方式**：每次只粘贴一个账号；多个账号混贴会被拒绝
- 每次提交成功后会显示「核对：面板现有 N 个账号（xxx、yyy）」，数字不对立即可见

## 快速开始（R2S 上三条命令）

```bash
ssh root@192.168.1.1          # 登录 R2S
cd /root
git clone https://github.com/Wangzhe997/jd-ck-helper.git
cd jd-ck-helper
sh deploy.sh                  # 交互式填写青龙凭据，自动构建启动
```

部署完成后手机浏览器访问 `http://R2S的IP:8787` 即可。

> `deploy.sh` 会检查 docker、生成 `.env`、提示输入青龙 Client ID / Secret，然后 `docker compose up -d --build` 并打印访问地址。更新代码只要 `git pull && docker compose up -d --build`。
>
> 国内网络 `git clone` 慢或失败时，可改用压缩包（无需 git 环境）：
> ```bash
> curl -L https://codeload.github.com/Wangzhe997/jd-ck-helper/tar.gz/refs/heads/main -o jd-ck-helper.tgz
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
| `TZ` | `Asia/Shanghai` | 时区 |

若脚本库明确要求「单变量多账号换行分隔」，把 `MODE` 改成 `merge` 后 `docker compose up -d` 重建即可。

## 安全提醒

- 本服务持有青龙 Open API 凭据，**只在内网使用**，务必不要做端口映射 / DDNS 暴露到公网
- CK 等同于账号登录态，**不要粘贴到任何第三方在线工具**
- 青龙面板建议升级到新版并保持内网访问（2.20.2 以下版本存在已知外网漏洞）

## 目录结构

```
app.py              # 全部逻辑：HTTP 服务 + CK 清洗/校验 + 青龙 Open API 调用
docker-compose.yml  # host 网络部署
Dockerfile          # python:3.12-alpine
deploy.sh           # R2S 一键部署脚本
.env.example        # 配置模板
```

## License

MIT
