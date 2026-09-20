# 区域医护能力规划台账

汇集人员资质、注册地点、执业范围、劳动关系和可服务时段，按统一归属口径形成区域医护能力基线，回答「某个县当周真正能独立接诊多少人」，支撑 2030 年医师与护士扩容规划。

## 业务口径

- **分列建档**：人员资质、注册地点（含执业范围）、劳动关系、可服务时段分别建档；执业医师与执业助理医师分列统计，一人持多类资质时按最高类别计入。
- **归属口径**：同一人员跨机构出现时，按覆盖统计日的在职劳动关系唯一归属——借调优先于在编，同级取最早生效记录；多点执业注册不重复计人头。计入能力还须证书在统计日未到期、在归属机构有有效注册。
- **双时间语义**：证书到期、调动、休假自生效日起影响能力；核验与更正只追加事务时间版本，不倒改历史。任意历史日期发布的能力基线可按当时已确认的数据版本复现。
- **属地核验**：机构上报经属地核验员核验后方可计入；重复上报与迟到更正只形成一次有效事实；口径冲突（如跨机构双在编）待裁决期间不计入能力，并在下钻中列为冲突项。
- **情景隔离**：规划情景（新增岗位、培训完成、人员流失）仅基于正式现状演算并标注 `is_projection`，预测值永不写入正式台账。

## 运行

`python3 service.py --check` 核对服务配置；`python3 service.py --port 8000` 启动服务；`python3 -m unittest -v` 运行全部测试。

## 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` `/contract` | 健康检查与领域契约 |
| POST | `/orgs` `/verifiers` `/persons` | 机构、属地核验员、人员建档 |
| POST | `/reports` | 变更上报（幂等，进入待核验；`corrects` 声明更正对象） |
| GET | `/reports?county=&status=` | 核验队列 |
| POST | `/verifications` | 属地核验（`decision`: 通过/驳回；冲突裁决 `resolution`: 更正既有） |
| GET | `/capacity/county/{县}?date=&as_of=&drill=1` | 县级能力汇总与机构下钻（构成、新鲜度、冲突项） |
| GET | `/capacity/institution/{机构}?date=&as_of=` | 机构能力明细 |
| POST | `/baselines` `{county, date}` | 发布能力基线快照 |
| GET | `/baselines/{id}`；`/baselines?county=&as_of=` | 复现当时发布的能力基线 |
| POST | `/scenarios`；GET `/scenarios/{id}/projection?date=` | 规划情景与演算（标注 `is_projection`） |
| GET | `/conflicts?county=` | 冲突项清单 |

## 事实事类与负载

- `qualification`：`{category: 执业医师/执业助理医师/注册护士, cert_no, expires?}`
- `registration`：`{org, type: 主执业点/多点执业, specialty}`
- `employment`：`{org, relation: 在编/借调, status: 在职/进修/长期离岗/离职}`
- `availability`：`{type: slot, org, weekday: 0-6, period}` 或 `{type: leave, reason}`（生效区间为休假起止）
