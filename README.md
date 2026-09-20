# 区域医护能力规划台账

汇集人员资质、注册地点、执业范围、劳动关系和可服务时段，按统一口径形成区域医护能力现状、历史基线与规划情景。项目以领域契约约定参与者、状态和不可破坏的业务原则，基础服务提供稳定的运行检查与契约读取接口，便于各模块围绕同一语义协作。

## 运行

```bash
python3 service.py --check                 # 核对契约与事实库
python3 service.py --port 8000 --seed      # 启动并载入演示数据
python3 -m unittest -v                     # 运行全部测试
```

事实库默认写入 `workforce.db`(SQLite)，可用 `--db` 指定路径。

## 统计口径(契约 calibers)

- **归属**：人员按统计日有效的主要劳动关系归属机构；无主要劳动关系时按主执业注册机构归属。多点执业、借调、进修不产生新的归属，同一人跨机构在同一口径下只计一次。
- **可独立接诊**：持有效执业资质、当日未休假且处于可服务时段的**执业医师**。执业助理医师单独列计，仅在乡镇卫生院、村卫生室视为可独立执业。
- **休假/长期离岗**：保留归属与编制口径，但不计入当周可接诊能力。
- **医护比** = 注册护士 / (执业医师 + 执业助理医师)。
- **生效日原则**：证书到期、调动、休假自生效日起影响能力；迟到更正只影响其后的查询，已发布基线原样保留。

## 双时态模型

每条事实携带两个时间轴：业务时间(`effective_from`/`effective_to`)与知识时间(`recorded_at`/`expired_at`)。事实只新增或作废，绝不原地改写：

- `date` 参数选择统计日(业务时间)，回答"当周能独立接诊多少人"；
- `as_of` 参数选择知识时刻，回答"按当时已确认的数据，口径是什么样"——据此可按任意历史日期复现当时发布的能力基线。

## 变更流程

机构提交变更进入**待核验**，属地核验员通过后事实才入账；退回则永不生效。同一幂等键重复提交返回原变更；内容指纹相同的重复上报标记 `DUPLICATE`，只形成一次有效事实。

变更类型：`PERSON_REGISTER`(建档)、`QUALIFICATION_ADD`(资质更正)、`QUALIFICATION_RENEW`(续期)、`REGISTRATION_ADD`(注册/多点备案)、`EMPLOYMENT_START`/`EMPLOYMENT_END`(劳动关系起止)、`TRANSFER`(调动，同步迁移主执业注册)、`AVAILABILITY_SET`(可服务时段/休假/长期离岗)。

## 主要接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` `/contract` | 健康检查、领域契约 |
| POST | `/institutions` | 登记机构(机构、县区、层级) |
| POST | `/counties/{县}/verifiers` | 登记属地核验员 |
| POST | `/changes` | 机构提交变更(支持 `idempotency_key`) |
| POST | `/changes/{id}/verify` | 属地核验：`{"verifier","decision":"APPROVE\|REJECT"}` |
| GET | `/changes?county_code=&status=` | 变更列表(待核验积压) |
| GET | `/persons/{id}` | 人员分档档案(资质/注册/劳动关系/时段) |
| GET | `/counties/{县}/capacity?date=&as_of=` | 县级汇总：能力构成、紧缺专科、新鲜度、冲突项、机构分表 |
| GET | `/institutions/{id}/capacity?date=&as_of=` | 机构下钻：指标 + 人员名册 + 冲突项 |
| POST | `/baselines` | 发布县级能力基线(不可变快照) |
| GET | `/baselines/{id}` | 原样复现已发布基线 |
| POST | `/scenarios` | 建立规划情景(`ADD_POST`/`TRAINING_COMPLETE`/`ATTRITION`) |
| GET | `/scenarios/{id}/projection?date=` | 情景预测(独立于正式现状) |

规划情景在正式现状的内存副本上演算，输出 `kind=projection` 的独立对象，预测值绝不写回事实库；正式现状接口(`kind=official`)永远不读取情景数据。

## 县级汇总示例

```bash
curl -X POST localhost:8000/baselines -d '{"county_code":"330127","date":"2026-09-20"}'
curl "localhost:8000/counties/330127/capacity?date=2026-09-20&as_of=2026-09-01"
```

响应中 `capacity` 分列执业医师/助理医师/护士，`shortage_specialties` 给出紧缺专科构成，`freshness` 标出超期未核验的机构与待核验积压，`conflicts` 列出重叠劳动关系、一证多档、有多点备案无主注册、无有效资质仍在岗等冲突项。
