# websocket 服务 Dockerfile —— 拼多多自动回复系统长连接服务（默认端口 8090）
# ==============================================================================
# 本文件用途：构建 websocket（长连接）服务镜像。
# 设计要点：
#   1. 基于 python:3.11-slim，与 pyproject.toml 声明的 requires-python>=3.11 一致；
#   2. common 公共库经 sys.path 共享引用（main.py 将项目根目录加入 sys.path），
#      镜像内同时复制 common 与 websocket 目录，并设置 PYTHONPATH=/app；
#   3. 本服务承载账号密码登录（Playwright），需安装 Chromium 及其系统依赖；
#   4. 依赖从各自 pyproject.toml 的 [project].dependencies 提取后安装；
#   5. 入口统一为本服务 main.py（最小入口桩，规范 32），监听 0.0.0.0、
#      端口经环境变量 WEBSOCKET_PORT 配置，禁止写死 localhost（规范 21）；
#   6. 全链路北京时间（规范 17），容器时区设为 Asia/Shanghai。

FROM python:3.11-slim

# Playwright 浏览器安装路径（固定目录便于缓存与定位）
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

# 配置国内镜像源，加速 apt 与 pip 安装
RUN sed -i 's|deb.debian.org|mirrors.aliyun.com|g' /etc/apt/sources.list.d/debian.sources 2>/dev/null || \
    sed -i 's|deb.debian.org|mirrors.aliyun.com|g' /etc/apt/sources.list 2>/dev/null || true
RUN pip config set global.index-url https://pypi.tuna.tsinghua.edu.cn/simple

WORKDIR /app

# 安装基础系统依赖：curl（健康检查）、ca 证书、gcc（部分依赖需编译）。
# 额外安装中文字体 fonts-noto-cjk：TikTok 泰国站卖家后台为中文界面，headless
# 渲染 / 截图 / 选择器匹配需字体支持（TIK-017，s5 结论 headless 可用、无需 Xvfb）。
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates gcc fonts-noto-cjk \
    && update-ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# 复制 common 公共库与 websocket 服务源码（common 经 sys.path 共享）
COPY common /app/common
COPY websocket /app/websocket

# 从 websocket/pyproject.toml 与 common/pyproject.toml 提取依赖并安装。
RUN python -c "import tomllib; \
deps = tomllib.load(open('/app/common/pyproject.toml','rb')).get('project',{}).get('dependencies',[]) \
+ tomllib.load(open('/app/websocket/pyproject.toml','rb')).get('project',{}).get('dependencies',[]); \
open('/tmp/requirements.txt','w').write('\n'.join(deps))" \
    && pip install --no-cache-dir -r /tmp/requirements.txt

# 安装 Playwright Chromium 及其系统依赖（账号密码登录需启动浏览器）
RUN python -m playwright install --with-deps chromium

# 创建浏览器持久化数据目录（Playwright launch_persistent_context 使用）
RUN mkdir -p /app/websocket/browser_data

# 暴露长连接服务端口（与默认 WEBSOCKET_PORT 一致；实际端口经环境变量配置）
EXPOSE 8090

# 运行环境变量：日志实时输出 / 共享库可导入 / 北京时间
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app
ENV TZ=Asia/Shanghai

# 容器健康检查：探测服务 /health（端口经环境变量，默认 8090）
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${WEBSOCKET_PORT:-8090}/health" || exit 1

# 启动服务：入口统一为 websocket/main.py（最小入口桩，规范 32）
CMD ["python", "websocket/main.py"]
