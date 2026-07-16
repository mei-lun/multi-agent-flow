"""Runner process entry point and long-poll loop."""


def main() -> None:
    """注册节点事件、轮询 Git control、认领、执行和提交直到关闭。

    实现顺序：加载可信本地配置和持久 node_id；自检 Docker/Git/目录；向节点分支提交注册/
    更新事件；fetch control；按容量为 READY 任务提交 claim；等待 control 确认后执行；用进度
    事件刷新状态；push 任务分支和 submission 事件。关闭时停止新 claim、报告/保存本地状态并
    清理资源。GitHub 不可达时可以继续当前本地工作，但不能获得新任务或宣称提交已接受。
    """
    ...
