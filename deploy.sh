#!/bin/sh
# JD CK 助手 —— 一键部署脚本（适配 iStoreOS / OpenWrt / Linux）
# 用法：sh deploy.sh
set -e
cd "$(dirname "$0")"

echo "==> JD CK 助手 部署"

# 1. 检查 docker
if ! command -v docker >/dev/null 2>&1; then
    echo "✗ 未检测到 docker，请先在 iStoreOS 的『Docker』应用里安装/启动 docker"
    exit 1
fi

# 2. 准备 .env
if [ ! -f .env ]; then
    cp .env.example .env
    echo "==> 已生成 .env（从 .env.example）"
fi

# 3. 交互填写青龙凭据（若为空）
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

# 4. 构建并启动
echo "==> 构建镜像并启动容器（首次约 1 分钟）"
if docker compose version >/dev/null 2>&1; then
    docker compose up -d --build
else
    docker-compose up -d --build
fi

# 5. 输出访问地址
PORT=$(grep '^PORT=' .env | cut -d= -f2); PORT=${PORT:-8787}
IP=$(ip route get 1.1.1.1 2>/dev/null | sed -n 's/.*src \([0-9.]*\).*/\1/p' | head -n1)
echo ""
echo "✅ 部署完成"
echo "   手机浏览器打开: http://${IP:-路由器IP}:${PORT}"
echo "   配置修改: 编辑 .env 后执行 docker compose up -d"
echo "   查看日志: docker logs -f jd-ck-helper"
