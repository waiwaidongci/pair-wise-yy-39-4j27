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
- `POST /api/items/{id}/spot-checks`，应急经理对已关闭缺陷发起处置抽检
- `GET /api/items/{id}/spot-checks`、`GET /api/spot-checks`、`GET /api/spot-checks/{id}`
- `POST /api/spot-checks/{id}/decide`，指定复检人提交判定，必须提交`expected_version`
- `GET /api/audit`

允许角色：inspector, dam_engineer, emergency_manager, viewer。异常值比控制阈值越高，缺陷优先级越高；应急处置缺陷必须完成复检并记录证据后才能关闭。

### 处置抽检

汛期抽查复核关闭时的复检结论：

- 仅`emergency_manager`可发起，提交`check_no`（抽检编号，全局唯一，重复返回409）、`description`（检查说明）、`reviewer`（复检人）。
- 只能对`closed`状态缺陷发起；同一缺陷只允许一张`pending`抽检（部分唯一索引保证）。
- 被抽检缺陷当时的复检人（`repair→verified`的操作人）不能担任本次抽检的`reviewer`，否则409。
- 发起时快照保留原关闭时间、关闭操作人和关闭原因，以及发起时的缺陷版本。
- 判定`result`为`passed`/`failed`；仅指定`reviewer`可判定，`failed`必须填写`note`。
- 不通过：原子地把缺陷退回`repair`、版本+1并刷新`updated_at`重新计时，同时写入`spot_check_return`记录；原关闭记录与原因继续保留在抽检单上。
- 通过：维持`closed`，抽检单标记`passed`。判定后可重新复检、重新关闭，再发起新的抽检。
- 每次发起与判定都写入SHA-256审计链，记录责任人、结果与版本。

## 测试

```bash
python3 -m unittest discover -s tests -v
```
