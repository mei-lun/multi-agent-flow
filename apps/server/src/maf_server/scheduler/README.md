# Scheduler 接口、调用关系与实现逻辑

## 职责

Scheduler 解释已发布 Workflow、维护 LangGraph checkpoint、创建 Git 协调任务、处理提交和人工信号。它不运行远程 Agent，不调用模型，不执行 Tool；Git 写入统一经 GitCoordinationService。

## 主调用链

```text
RunService.start_run
  → SchedulerService.start_run
  → load RunSnapshot
  → graph_builder.compile_workflow
  → LangGraph 推进
  → graph_nodes.dispatch
  → JobDispatcher
  → maf/control 中 READY task
  → checkpoint 等待 Git submission

SUBMISSION_CREATED / InboxDecision / Timer
  → WakeupService(event_id 去重)
  → SchedulerService.resume_run
  → graph_nodes.evaluate
  → QualityGateService
  → graph_nodes.route 或 graph_nodes.rework
  → 下一 dispatch / human wait / end
```

## 一致性规则

Git control、业务表和 checkpoint 无法做一个原子事务，因此采用可重放步骤：先确认权威 control commit，再按 commit/event ID 更新 SQLite 投影和 checkpoint；重启后从投影水位重放。所有 graph node 必须幂等，不能依赖“只执行一次”。Graph State 只保存小字段、task ID、control commit 和 Artifact ID。

## 初级实现步骤

先实现固定假 Runner 结果的回放测试，再接真实 Runner。每个 node 函数按“读取输入引用→检查当前状态→执行一个确定动作→保存唯一事件→返回新小状态”编写。禁止在 node 中写无限循环、sleep、网络调用或大文档正文。
