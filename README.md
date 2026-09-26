# 动物园谱系与繁育协调

这是一个只使用Python标准库和SQLite的模块化项目，默认端口为`8308`。所有业务规则集中在`src/rules.py`，`app.py`只负责组装依赖和启动服务。

## 模块结构

- `app.py`：命令行参数、依赖组装、启动和信号处理。
- `src/domain.py`：角色、数据结构、领域异常和基础校验。
- `src/rules.py`：状态机、权限、领域计算、冲突和跨对象校验。
- `src/repository.py`：SQLite建表、查询、事务和乐观锁。
- `src/service.py`：用例编排、幂等处理、版本控制和审计写入。
- `src/http_api.py`：HTTP路由、请求解析和统一错误响应。
- `src/audit.py`：实体操作审计时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则和失败场景测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8308
```

服务启动时会自动建表。`--host`可修改监听地址，`--db`可指定其他SQLite文件。

## 核心对象

- `animal`：个体谱系，状态为 `active / quarantined / departed / deceased`。
- `health_check`：单只动物的一次健康检查，状态为 `pending / passed / failed`，结果由兽医账号录入。
- `pairing`：配对建议，状态为 `proposed / approved / rejected / completed`。
- `transfer`：机构和运输记录。

## 繁育审批规则

1. 协调员创建配对建议时必须填写 `sire_id`、`dam_id` 及双方最近健康检查编号 `sire_check_id`、`dam_check_id`；检查必须真实存在且属于对应动物。
2. 兽医在自己的账号下对各自负责的检查执行 `record_result`（`passed` 或 `failed`），结果只能录入一次，录入人记录为 `checked_by`。
3. 繁育委员会（`committee` 角色）批准时实时校验：双方检查均为 `passed`、双方动物当前均为 `active`、亲缘系数不高于 `0.125`，任一不满足即拒绝，错误中带结构化原因。
4. 批准后任一只动物进入隔离（`quarantined`）、离馆（`departed`）或死亡（`deceased`），`complete` 登记产仔会被拒绝，响应指出具体动物编号、名称和当前状态。
5. 动物恢复健康后，协调员可对同一条建议执行 `resubmit` 回到 `proposed`，由委员会重新批准；被驳回的建议也可重新送审。
6. `GET /api/pairings/<id>/review` 返回双方动物与检查的实时状态快照、批准阻断项（`approval_issues`）和登记阻断项（`blockers`），供页面展示。

## 主要接口

- `GET /health`：健康检查。
- `GET /api/<kind>`：按对象类型查询，可用`?status=`过滤；`kind` 支持 `animals`、`health_checks`、`pairings`、`transfers`。
- `POST /api/<kind>`：创建对象；请求体为JSON。
- `GET /api/entities/<id>`：读取对象当前版本。
- `GET /api/pairings/<id>/review`：配对建议的实时审查视图。
- `POST /api/entities/<id>/actions`：提交`{"action":"动作名","data":{...},"expected_version":数字}`。
- `GET /api/audit`：读取审计记录。

请求身份通过`X-User-Id`和`X-Role`请求头传入。创建和动作的可执行角色由规则引擎控制。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

谱系系数是简化亲缘规则，不替代专业谱系软件、遗传咨询或法定动物运输许可。
