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
- `transport_batch`：转运批次，每批关联一条已提交观察，登记起点、目的地、承运人和温度上限。

### 转运批次状态机

`registered`（已登记）→ `release` → `in_transit`（在途）→ `receive` → `received`（已接收）

- `release` 需上报离场实测温度，超过登记的温度上限将拒绝放行。
- `receive` 上报接收实测温度：合规则结束（`received`）；超限转 `pending_review`（待复核），并暂停关联样本的 `send_lab`。
- 待复核时先由**登记该批次的原承运人**（`carrier` 角色或 `admin`）执行 `add_handling_note` 补处置说明，再由复核员（`reviewer`）执行 `review` 确认，恢复为 `in_transit` 后可重新接收。
- 同一观察不能出现在两个未结束批次（`registered`/`in_transit`/`pending_review`）中；批次结束后观察可再次转运。
- 重复提交相同 `box_code`（箱号）直接返回首次创建的批次，便于网络重试。
- 批次数据中维护 `temperature_records`（各环节温度与合规判定）和 `handovers`（交接时间线），动作同时写入审计日志。

## 主要接口

- `GET /health`：健康检查。
- `GET /api/<kind>`：按对象类型查询，可用`?status=`过滤。
- `POST /api/<kind>`：创建对象；请求体为JSON。
- `GET /api/entities/<id>`：读取对象当前版本。
- `POST /api/entities/<id>/actions`：提交`{"action":"动作名","data":{...},"expected_version":数字}`。
- `GET /api/audit`：读取审计记录。

请求身份通过`X-User-Id`和`X-Role`请求头传入。创建和动作的可执行角色由规则引擎控制。转运批次使用 `field`（登记/放行/接收）、`carrier`（补处置说明，需为批次登记的原承运人）和 `reviewer`（复核确认）角色。

### 转运批次示例

```bash
# 登记批次（observation_id 必须是已 submitted 的观察）
curl -X POST localhost:8305/api/transport_batches \
  -H 'Content-Type: application/json' -H 'X-Role: field' \
  -d '{"observation_id":"<obs-id>","box_code":"BOX-1","origin":"北保护站","destination":"省实验室","carrier":"lenglian-wang","max_temperature":8}'
# 放行（离场温度合规才放行）
curl -X POST localhost:8305/api/entities/<batch-id>/actions -H 'X-Role: field' \
  -d '{"action":"release","data":{"departure_temp":6}}'
# 接收（超限自动转 pending_review 并暂停送检）
curl -X POST localhost:8305/api/entities/<batch-id>/actions -H 'X-Role: field' \
  -d '{"action":"receive","data":{"arrival_temp":10.5}}'
# 原承运人补处置说明 → 复核员确认恢复
curl -X POST localhost:8305/api/entities/<batch-id>/actions -H 'X-Role: carrier' -H 'X-User-Id: lenglian-wang' \
  -d '{"action":"add_handling_note","data":{"handling_note":"已补冰排，温度回落"}}'
curl -X POST localhost:8305/api/entities/<batch-id>/actions -H 'X-Role: reviewer' \
  -d '{"action":"review","data":{"review_note":"同意恢复送检"}}'
```

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

离线同步使用批次和幂等键演示，不包含真实野外通信协议、地图底图或完整空间索引。
