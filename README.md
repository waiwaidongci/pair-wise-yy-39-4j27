# 大坝巡检、缺陷与应急管理

安排巡检，记录渗流、位移、裂缝等缺陷并跟踪修复、复检和应急预案。

## 模块结构

- `app.py`：参数解析、依赖组装和HTTP服务启动。
- `src/domain.py`：数据结构、错误、状态和基础校验。
- `src/rules.py`：状态机、角色矩阵、优先级、期限和关闭不变量。
- `src/repository.py`：SQLite建表、事务、版本控制和审计链。
- `src/service.py`：权限检查、用例编排、并发控制和审计。
- `src/http_api.py`：JSON路由和统一错误响应。
- `src/audit.py`：UTC时间和SHA-256审计事件。
- `static/index.html`：最小演示页。
- `tests/`：完整流程、规则和失败测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8316
```

默认端口为`8316`，首次启动自动建库。使用`X-Actor`和`X-Role`请求头传递身份。

## 主要接口

- `GET /health`
- `GET /api/items`
- `POST /api/items`
- `GET /api/items/{id}`
- `POST /api/items/{id}/records`
- `POST /api/items/{id}/transition`，必须提交`expected_version`
- `POST /api/items/{id}/spot-checks`：应急经理对已关闭缺陷发起处置抽检
- `POST /api/items/{id}/spot-checks/judge`：指定复检人判定`passed`/`failed`
- `GET /api/items/{id}/spot-checks`、`GET /api/spot-checks`
- `GET /api/audit`

允许角色：inspector, dam_engineer, emergency_manager, viewer。异常值比控制阈值越高，缺陷优先级越高；应急处置缺陷必须完成复检并记录证据后才能关闭。

## 处置抽检规则

- 只有`emergency_manager`能发起，需填写`check_no`（全局唯一，重复返回409）、`description`和`checker`。
- 抽检对象必须处于`closed`；同一缺陷同时只能有一张`pending`抽检。
- `checker`不能是该缺陷最近一次复检（`verified`）的动作人，冲突返回422。
- 只有指定的`checker`（角色限inspector/dam_engineer）能判定，须提交与缺陷当前版本一致的`expected_version`。
- `failed`必须填`result_note`；缺陷退回`repair`，版本+1并以退回时刻重新计算维修期限（`repair_restarted_at`/`repair_deadline_at`），原关闭记录与原因保留在审计链中，可重新复检关闭后再次抽检。
- `passed`维持关闭；发起人和判定人、抽检版本与缺陷版本均写入哈希链审计。

## 测试

```bash
python3 -m unittest discover -s tests -v
```
