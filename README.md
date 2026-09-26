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
- `static/index.html`：演示页面（身份切换、动物状态、健康检查、配对审批）。
- `tests/`：完整流程、规则和失败场景测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8308
```

服务启动时会自动建表。`--host`可修改监听地址，`--db`可指定其他SQLite文件。

## 核心对象

- `animal`：个体谱系；状态：`active`（在馆健康）→ `quarantined`（隔离）/ `departed`（离馆）/ `deceased`（死亡），隔离和离馆可恢复。
- `health_exam`：健康检查记录；兽医（`veterinarian`）用各自账号录入，结论为`passed`或`failed`。
- `pairing`：配对建议；协调员（`coordinator`）提交时必须填写`sire_id`、`dam_id`及双方**最近一次**检查编号`sire_exam_id`、`dam_exam_id`。
- `transfer`：机构和运输记录。

## 配对审批流程

1. 协调员提交建议（状态`proposed`），系统自动记录提交人账号。
2. 繁育委员会（`committee`）批准或驳回。批准条件（三者同时满足）：
   - 双方动物的**最近一次**健康检查均为`passed`；
   - 双方动物当前均为`active`（不处于隔离、离馆或死亡）；
   - 亲缘风险系数 ≤ 0.125。
3. 批准后（`approved`）如任一方进入隔离、离馆或死亡，`complete`（登记产仔）会被拒绝，错误信息和配对视图的`blockers`字段都会指明是哪只动物及其当前状态。
4. 被驳回（`rejected`）或批准后动物状态变化的同一条建议，可由协调员`resubmit`重新送审（可同时更新检查编号），回到`proposed`再次审批。
5. 查询配对（`GET /api/entities/<id>`或`GET /api/pairing`）返回委员会视图：双方动物当前状态、引用检查与最近检查结论、亲缘系数、`approvable`/`approval_issues`、`can_register_offspring`/`blockers`。

## 角色

`admin`、`registrar`（动物/运输登记）、`veterinarian`（健康检查、隔离/解除隔离、死亡登记）、`coordinator`（提交建议、重新送审、登记产仔）、`committee`（批准/驳回）、`viewer`（只读）。

## 主要接口

- `GET /health`：健康检查。
- `GET /api/<kind>`：按对象类型查询，可用`?status=`过滤。
- `POST /api/<kind>`：创建对象；请求体为JSON。
- `GET /api/entities/<id>`：读取对象当前版本。
- `POST /api/entities/<id>/actions`：提交`{"action":"动作名","data":{...},"expected_version":数字}`。
- `GET /api/audit`：读取审计记录。

请求身份通过`X-User-Id`和`X-Role`请求头传入。创建和动作的可执行角色由规则引擎控制。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

谱系系数是简化亲缘规则，不替代专业谱系软件、遗传咨询或法定动物运输许可。
