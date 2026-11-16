FROM python:3.12-alpine

LABEL description="JD CK Helper - 配合青龙面板的京东 Cookie 一键提交工具（零依赖）"

WORKDIR /app
COPY app.py .

EXPOSE 8787

CMD ["python", "app.py"]
