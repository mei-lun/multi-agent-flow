# Docker 镜像

- `server.Dockerfile`：构建 Web 静态资源和 Python Server 的多阶段镜像。
- `runner.Dockerfile`：安装 Git、Docker 客户端及 Runner Python 环境。
- `profiles/`：Agent 作业允许使用的最小运行镜像定义，不接受任意镜像字符串。

