#!/bin/sh
# JD CK 助手 —— 一键部署脚本（适配 iStoreOS / OpenWrt / Linux）
# 用法：sh deploy.sh
# 针对 iStoreOS 软路由环境做了以下加固：
#   - DNS 自检：域名解析失败时自动临时改用公共 DNS
#   - 镜像加速：自动写入 Docker 国内加速器（iStoreOS 用 UCI 而非 daemon.json）
#   - 服务名识别：自动识别 dockerd / docker 服务名
#   - 青龙连通性预检：构建前先探测青龙地址，避免装完才发现连不上
set -e
cd "$(dirname "$0")"

echo "==> JD CK 助手 部署（iStoreOS 优化版）"

# ---------- 0. DNS 自检与临时修复 ----------
# iStoreOS 上 dnsmasq 有时拿不到上游，导致 git/wget 报 Could not resolve host。
# 这里只做「临时」修复（不写 UCI，避免覆盖用户的 DNS 设置），重启网络后会还原。
if command -v nslookup >/dev/null 2>&1; then
    if ! nslookup github.com >/dev/null 2>&1; then
        echo "==> 检测到 DNS 解析异常，临时改用公共 DNS（223.5.5.5 / 119.29.29.29）"
        if [ -w /etc/resolv.conf ]; then
            printf 'nameserver 223.5.5.5\nnameserver 119.29.29.29\n' > /etc/resolv.conf
            echo "    （临时生效，重启网络后还原；如需永久修改请在 iStoreOS 网络设置里配置）"
        else
            echo "    ! 无权限写 /etc/resolv.conf，请手动设置 DNS 后重试"
        fi
    fi
fi

# ---------- 1. 检查 docker ----------
if ! command -v docker >/dev/null 2>&1; then
    echo "✗ 未检测到 docker，请先在 iStoreOS 的『Docker』应用里安装/启动 docker"
    exit 1
fi

# ---------- 2. Docker 镜像加速（iStoreOS 用 UCI） ----------
# 注意：iStoreOS/OpenWrt 的加速器配置在 /etc/config/dockerd，
# 写 /etc/docker/daemon.json 不生效——这是之前构建卡在拉镜像的根因。
if command -v uci >/dev/null 2>&1; then
    if uci show dockerd >/dev/null 2>&1; then
        if ! uci get dockerd.globals.registry_mirrors >/dev/null 2>&1; then
            echo "==> 配置 Docker 国内镜像加速（写入 UCI: dockerd.globals.registry_mirrors）"
            uci add_list dockerd.globals.registry_mirrors='https://docker.1ms.run'
            uci add_list dockerd.globals.registry_mirrors='https://docker.xuanyuan.me'
            uci add_list dockerd.globals.registry_mirrors='https://docker.m.daocloud.io'
            uci commit dockerd
            # 重启 dockerd 让加速生效（iStoreOS 服务名是 dockerd，不是 docker）
            if [ -x /etc/init.d/dockerd ]; then
                /etc/init.d/dockerd restart >/dev/null 2>&1 || true
                sleep 2
                echo "    dockerd 已重启，镜像加速生效"
            fi
        else
            echo "==> 检测到已配置镜像加速，跳过"
        fi
    fi
fi

# ---------- 3. 准备 .env ----------
if [ ! -f .env ]; then
    cp .env.example .env
    echo "==> 已生成 .env（从 .env.example）"
fi

# ---------- 4. 交互填写青龙凭据（若为空） ----------
need_prompt=0
grep -q '^QL_CLIENT_ID=.\+' .env || need_prompt=1
grep -q '^QL_CLIENT_SECRET=.\+' .env || need_prompt=1
if [ "$need_prompt" = "1" ]; then
    echo ""
    echo "请粘贴青龙面板『系统设置 → 应用设置』里新建应用得到的两个值："
    printf "Client ID: "
    read -r CID
    printf "Client Secret: "
    read -r CSEC
    # 用 awk 重写，避免 sed 在部分 busybox 上的 -i 差异
    awk -v cid="$CID" -v csec="$CSEC" '
        /^QL_CLIENT_ID=/     { print "QL_CLIENT_ID=" cid; next }
        /^QL_CLIENT_SECRET=/ { print "QL_CLIENT_SECRET=" csec; next }
        { print }
    ' .env > .env.tmp && mv .env.tmp .env
    echo "==> 已写入 .env"
fi

# ---------- 5. 青龙连通性预检 ----------
QL_URL=$(grep '^QL_URL=' .env | cut -d= -f2 | tr -d '\r'); QL_URL=${QL_URL:-http://127.0.0.1:5700}
if command -v wget >/dev/null 2>&1; then
    if ! wget -q -O /dev/null -T 5 "$QL_URL" 2>/dev/null; then
        echo ""
        echo "⚠ 警告：连不上青龙面板 $QL_URL"
        echo "   请确认青龙容器已启动（docker ps 里 qinglong 状态为 Up）。"
        echo "   若青龙在别的设备，请把 .env 里的 QL_URL 改成实际地址。"
        echo "   （本工具现在仍会部署，但页面会显示『连接失败』）"
    else
        echo "==> 青龙连通性检查通过（$QL_URL）"
    fi
fi

# ---------- 6. 构建并启动 ----------
echo "==> 构建镜像并启动容器（首次约 1 分钟）"
if docker compose version >/dev/null 2>&1; then
    docker compose up -d --build
else
    docker-compose up -d --build
fi

# ---------- 7. 输出访问地址 ----------
PORT=$(grep '^PORT=' .env | cut -d= -f2 | tr -d '\r'); PORT=${PORT:-8787}
IP=$(ip route get 1.1.1.1 2>/dev/null | sed -n 's/.*src \([0-9.]*\).*/\1/p' | head -n1)
[ -z "$IP" ] && IP=$(hostname -i 2>/dev/null | awk '{print $1}')
echo ""
echo "✅ 部署完成"
echo "   手机浏览器打开: http://${IP:-路由器IP}:${PORT}"
echo "   首次使用: 打开页面 → 右上角「🔒 设置登录保护」设用户名和密码（外网访问前务必设置）"
echo "   取 CK 推荐: 装油猴 → 打开页面点「安装油猴脚本」→ m.jd.com 短信登录 → 点悬浮按钮"
echo "   配置修改: 编辑 .env 后执行 docker compose up -d"
echo "   查看日志: docker logs -f jd-ck-helper"
