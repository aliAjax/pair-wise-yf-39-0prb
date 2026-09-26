# 野生动物疫病监测与离线同步

这是一个只使用Python标准库和SQLite的模块化项目，默认端口为`8305`。所有业务规则集中在`src/rules.py`，`app.py`只负责组装依赖和启动服务。

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
python3 app.py --db ./data.db --port 8305
```

服务启动时会自动建表。`--host`可修改监听地址，`--db`可指定其他SQLite文件。

## 核心对象

- `observation`：现场观察；`sample`：样本与实验室结果；`cluster`：异常聚集事件。
- `transport_batch`：野外样本转运批次，每批关联一条已提交（`submitted`）观察，记录箱号、起点、目的地、承运人和温度上限。

## 转运批次状态机

`registered`（已登记）→ `in_transit`（运输中）→ `completed`（已完成）；接收实测超限时转入 `under_review`（待复核）。

- 创建：角色 `field`/`admin`；要求观察已提交；同一观察不能同时存在两个未结束（非 `completed`）批次；重复箱号的重试直接返回首次创建的批次。
- `depart`（放行，`field`/`carrier`）：提交离场测温，超过本批温度上限则拒绝放行；同时写入温度与交接记录。实际接箱的承运人被记为原承运人。
- `handover`（途中交接，`field`/`carrier`）：登记交出方/接收方，可附带测温；追加交接记录。
- `receive`（接收，`field`/`lab`）：登记接收实测温度；超限则批次转 `under_review`、标记 `submission_suspended`，关联样本的 `send_lab`（送检）被暂停。
- `add_handling_note`（仅原承运人）：补充超限处置说明。
- `confirm_review`（`reviewer`/`admin`）：复核确认后批次完成、解除送检暂停；未补处置说明不能复核。

批次的 `data` 内含 `temperature_logs`、`handover_logs`、`handling_notes` 三条追加式记录，所有动作同时写入 `/api/audit`。

## 主要接口

- `GET /health`：健康检查。
- `GET /api/<kind>`：按对象类型查询，可用`?status=`过滤（`kind` 可用 `transport_batches`）。
- `POST /api/<kind>`：创建对象；请求体为JSON。
- `GET /api/entities/<id>`：读取对象当前版本。
- `POST /api/entities/<id>/actions`：提交`{"action":"动作名","data":{...},"expected_version":数字}`。
- `GET /api/audit?entity_id=<id>`：读取审计记录，可按实体过滤。

请求身份通过`X-User-Id`和`X-Role`请求头传入。创建和动作的可执行角色由规则引擎控制。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

离线同步使用批次和幂等键演示，不包含真实野外通信协议、地图底图或完整空间索引。
