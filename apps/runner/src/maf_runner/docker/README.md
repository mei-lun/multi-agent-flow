# Docker 接口逻辑

DockerManager 只使用本地白名单 Profile 和镜像 digest。一个 Job 对应独立容器、网络和工作区挂载。取消、超时、lease 丢失或异常都必须进入同一个 finally 清理链。清理按 Runner/job label 定位，绝不能用模糊名称删除其他容器。

