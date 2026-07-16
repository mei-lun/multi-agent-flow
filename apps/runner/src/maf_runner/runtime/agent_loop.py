"""带取消、预算和输出检查的有界 observe-think-act 接口。"""


async def run_agent(context: object) -> object:
    """运行 Agent，直到提交、等待人工、失败、取消或达到硬上限。

    每轮先检查取消/时间/步数/Tool 次数/预算；构造裁剪后的消息调用 ModelClient；模型返回
    ToolCall 时只经 ToolClient 执行并记录；返回 final 时按 output contract 本地预校验；所有
    中间大输出写 Artifact。不得用无限自动重试掩盖错误。
    """
    ...
