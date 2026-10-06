# 自来水厂取样合规复核服务

基于 **FastAPI + SQLite** 的取样合规复核服务:维护分区必测指标与限值生效区间,登记样本并录入/更正读数,满足发布条件后生成不可篡改的合规结论快照。

## 快速开始

### Docker Compose(一键启动,自动初始化数据库)

```bash
docker compose up --build
```

服务地址:**http://localhost:8000**,交互式文档:**http://localhost:8000/docs**。

数据库文件在命名卷 `water-data` 中(`/data/water.db`),首次启动自动建库建表,无需额外初始化。

### 本地运行

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

### 运行测试

```bash
pip install -r requirements-dev.txt
pytest tests/ -q
```

## 配置

| 配置项 | 环境变量 | 默认值 | 说明 |
|---|---|---|---|
| 数据库路径 | `DB_PATH` | 容器内 `/data/water.db`,本地 `./data/water.db` | SQLite 文件位置,目录自动创建 |
| 监听地址 | 启动参数 `--host` | `0.0.0.0` | |
| 监听端口 | 启动参数 `--port` | `8000` | Compose 映射到宿主机 `8000` |

时间戳一律使用 ISO 8601(建议带时区,如 `2026-10-06T08:30:00Z`);不带时区按 UTC 处理,服务内部统一规范化为 UTC 存储。

## 业务规则

- **限值区间**:每分区每指标维护若干左闭右开区间 `[effective_from, effective_to)`。`from >= to` 为无效区间(422);与已有区间重叠拒绝(409);端点相接不算重叠。
- **样本**:登记分区与采样时刻,携带乐观锁版本号 `version`(初始 0)。
- **读数录入/更正**:读数必须非负(否则 422),请求必须带 `expected_version`,与样本当前版本不符报 409 `version_conflict`;每次成功写入版本 +1。
- **更正幂等**:更正请求必须带全局唯一 `request_id`。同一请求号重放返回首次的结果(响应头 `X-Idempotent-Replay: true`,不产生新写入);同号异内容报 409 `request_id_conflict`。
- **发布条件**:该分区全部必测指标读数齐全,且采样时刻对每项必测指标**恰有一条**生效限值,否则拒绝(422 `missing_readings` / `limit_not_exactly_one`),失败不留部分报告。
- **结论判定**:任一必测指标读数 > 限值即 `non_compliant`,否则 `compliant`。
- **报告快照**:发布即保存读数、限值依据(规则 id、限值、区间)与结论快照。同一读数版本不得重复发布(409 `already_published`);更正后再发布生成递增的 `report_version`,旧版本永久可查、不被覆盖。
- **并发发布**:写路径使用 `BEGIN IMMEDIATE` 事务 + `(sample_id, sample_version)` 唯一约束,并发发布同一读数版本只会有一个成功,其余收到 409。

## API 一览

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 健康检查 |
| POST | `/zones` | 创建分区 |
| PUT / DELETE | `/zones/{zone}/indicators/{indicator}` | 设置 / 取消必测指标 |
| GET | `/zones/{zone}/indicators` | 必测指标列表 |
| POST | `/zones/{zone}/indicators/{indicator}/limits` | 新增限值生效区间(重叠/无效拒绝) |
| GET | `/zones/{zone}/indicators/{indicator}/limits` | 限值区间列表 |
| POST | `/samples` | 登记样本(分区 + 采样时刻) |
| GET | `/samples/{id}` | 样本详情(当前版本与读数) |
| POST | `/samples/{id}/readings` | 录入读数(带 `expected_version`) |
| PUT | `/samples/{id}/readings/{indicator}` | 更正读数(带 `expected_version` + `request_id`,幂等) |
| POST | `/samples/{id}/publish` | 发布合规报告(快照) |
| GET | `/samples/{id}/reports` | 报告版本列表 |
| GET | `/samples/{id}/reports/{report_version}` | 指定版本报告快照 |

## 调用示例

完整可执行脚本(服务启动后运行):

```bash
bash examples/demo.sh
```

核心流程:

```bash
BASE=http://localhost:8000

# 1. 分区 + 必测指标 + 限值区间(左闭右开)
curl -X POST $BASE/zones -H 'Content-Type: application/json' -d '{"name":"东区"}'
curl -X PUT $BASE/zones/东区/indicators/turbidity
curl -X POST $BASE/zones/东区/indicators/turbidity/limits -H 'Content-Type: application/json' -d '{
  "effective_from":"2026-01-01T00:00:00Z","effective_to":"2027-01-01T00:00:00Z","max_value":1.0}'

# 2. 登记样本
curl -X POST $BASE/samples -H 'Content-Type: application/json' -d '{
  "zone":"东区","sampled_at":"2026-10-06T08:30:00Z"}'
# => {"sample_id":1, ..., "version":0}

# 3. 录入读数(预期版本 0)
curl -X POST $BASE/samples/1/readings -H 'Content-Type: application/json' -d '{
  "indicator":"turbidity","value":0.42,"expected_version":0}'

# 4. 发布 -> 返回并保存快照
curl -X POST $BASE/samples/1/publish

# 5. 更正读数(幂等请求号),再发布产生新报告版本
curl -X PUT $BASE/samples/1/readings/turbidity -H 'Content-Type: application/json' -d '{
  "value":3.2,"expected_version":1,"request_id":"corr-20261006-001"}'
curl -X POST $BASE/samples/1/publish

# 6. 查询报告历史 / 指定版本
curl $BASE/samples/1/reports
curl $BASE/samples/1/reports/1
```

发布响应示例:

```json
{
  "report_id": 2,
  "sample_id": 1,
  "sample_version": 2,
  "report_version": 2,
  "zone": "东区",
  "sampled_at": "2026-10-06T08:30:00.000000+00:00",
  "conclusion": "non_compliant",
  "created_at": "2026-10-06T01:51:50.579287+00:00",
  "items": [
    {
      "indicator": "turbidity",
      "value": 3.2,
      "exceeded": true,
      "limit": {
        "rule_id": 1,
        "max_value": 1.0,
        "effective_from": "2026-01-01T00:00:00.000000+00:00",
        "effective_to": "2027-01-01T00:00:00.000000+00:00"
      }
    }
  ]
}
```

## 项目结构

```
app/
  main.py        # FastAPI 路由与业务逻辑(事务、幂等、发布判定)
  db.py          # SQLite 模式、连接、时间规范化
tests/
  test_api.py    # 区间校验、版本冲突、幂等、发布、并发等端到端测试
examples/
  demo.sh        # 端到端调用示例
Dockerfile
docker-compose.yml
requirements.txt / requirements-dev.txt
```
