# pifa_lingshou：独立批零采购与结算项目

已内置从 pifa 迁入的批发建模、预测、交易、场景与结算模块。运行不需要同级 pifa 目录，也不修改运行时 sys.path。流程：创建案例 → 编辑/保存输入 → 按全预测期稳定性推荐套餐 → 人工确认套餐 → 分阶段采购 → 日前及日内储能 → 标的日结算。

## 入口

主入口归属网页应用文件夹 `web/__main__.py`；项目根目录不放 run_demo。在 aggregate 目录执行：

```sh
pifa_lingshou/.venv/bin/python -m pifa_lingshou.web --port 8765
```

打开打印的 `http://127.0.0.1:8765/`。网页顶端配置客户数量、标的日、预测期、λ、情景数、随机种子、采购基准价、零售参考价、权重和经济底线。第1页可改每客户96点负荷/分时基准/套餐价格、市场96点价格、整段逐日预测系数。高级JSON含储能、价格上下限与各阶段预测。套餐推荐不会自动签约，须逐客户人工选择并确认。

仅复制本目录到其他位置时，在本目录安装并运行：

```sh
python -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/python -m pifa_lingshou.web --port 8765
```

依赖 SciPy 1.11–1.13 内置的 HiGHS MILP。可传 `--resource-dir /可写路径/resource` 分离代码与数据。服务仅监听本机，属于单用户演示应用。

## 文件夹职责

| 文件夹 | 含义与放置原因 |
| --- | --- |
| `web` | 网页入口、HTTP接口、页面/CSS/交互脚本；管理操作与进度，不放优化方程。 |
| `service` | 业务编排。cases管理案例/签约/阶段，recommendation估计套餐，layer_runner运行节点，demo生成批量报告。 |
| `problem_solver` | 优化变量、约束、目标和求解器；L1合同、L2撮合、L3日前申报、储能分别实现。储能的唯一实现是 `storage_milp.py`，同时提供 `dispatch` 和兼容别名 `solve_storage_milp`。 |
| `inputs` | 输入校验、Mock及预测适配。case_data定义发布日期、交割范围与客户96点预测。 |
| `data_objects` | 共享客户、合同、场景和状态数据结构，统一层间数据格式。 |
| `accounting` | 批发成本、零售账单、客户分摊及实绩回放；核算逻辑不能放入数据输出目录。 |
| `config` | 可编辑默认值，包括推荐阈值、随机种子和储能参数。 |
| `constants` | 96点/48点等公共时间网格；与业务配置分开。 |
| `utils` | visualize.py负责报告入口，report负责图表模板，resource_store负责原子文件存储。 |
| `resource` | 每个案例的input/output与历史archive，只存资源数据。 |
| `tests` | 模型、核算、案例状态及前端交互回归，不参与业务计算。 |
| `docs` | 算法、市场口径、边界与验收记录。 |

可视化在 `utils/visualize.py`；`service/storage_validation.py` 只负责相同合同/申报/情景下储能启停成本对照。

## 每个案例独立存储

```text
resource/
  input/<case_id>/
    revision_0001.json         # 完整输入版本；修改只增加版本
    signed_packages.json      # 人工确认套餐与条款
    01_ANNUAL_request.json     # 本节点实际使用参数和版本
    ...
    settlement_actuals.json   # 确认后的负荷/价格/储能实绩
  output/<case_id>/
    state.json                # 状态与结果索引
    recommendation_0002.json   # 整段预测的F/L/S收益和节省
    01_ANNUAL.json / .html     # 每节点求解结果及报告
    ...
    risk_comparison.json / .html
    settlement.json           # 公司/客户结算，含96点明细
  archive/previous_outputs/   # 旧报告保留
```

页面可重开历史案例，新建不会覆盖旧案例。保存新输入使旧推荐失效；签约后条款锁定，未完成节点仍可更新预测。日内须提供已执行负荷/价格/充放电/SOC前缀，后续不可回退或改写。

## 模型与运行

| 阶段 | 实现与输入 |
| --- | --- |
| 套餐推荐 | 全部逐日预测电量CV与预测相对半区间：稳定F、中等S、波动L；再检查客户节省与公司期望利润。用可编辑年/月/现货权重估计签约前采购成本，不生成订单。 |
| L1 年/月/旬 | 48点五档负荷随机MILP，成本、覆盖考核和CVaR。每节点使用自身交割范围日均96点预测与价格，合同数量按日历天数折算，考核比较统一到当前节点范围。 |
| L2 D-3/D-2 | MILP提供边际估值，逐笔阈值撮合规则决定买卖与部分成交；撮合本身不冒充MILP。 |
| L3-A 日前申报 | 独立96点MILP，储能功率为0，继承锁定合同与已签套餐。 |
| 日前储能 | 固定申报的MILP，套利、考核、退化与CVaR共同优化。 |
| 日内储能 | 固定已执行前缀，按剩余负荷/价格更新预测重算未来动作。 |
| 结算 | 按实际电量、价格和固定动作核算并验证物理约束，不做事后最优调度。模拟数据明确标记MOCK_REPLAY。 |

第3页“各层独立运行工作台”允许默认前置、上游JSON和手动输入，独立层试验不要求先建完整案例。命令行也保留：

```sh
pifa_lingshou/.venv/bin/python -m pifa_lingshou.service.layer_runner L1
pifa_lingshou/.venv/bin/python -m pifa_lingshou.service.layer_runner L3-A --inputs inputs.json --upstream upstream.json
pifa_lingshou/.venv/bin/python -m pifa_lingshou.service.layer_runner STORAGE-RT --inputs actual-prefix.json --upstream day-ahead.json
pifa_lingshou/.venv/bin/python -m pifa_lingshou.service.demo --no-server
```

节点支持 ANNUAL、MONTHLY、TEN_DAY、D-3、D-2、L3-A、STORAGE-DA、STORAGE-RT，以及L1/L2/CHAIN。批量入口service.demo保留F/L/S×五λ报告并打印HTML地址；不传--no-server时尝试启动本地服务。案例页的λ比较从已保存的各节点预测复算已签组合，不新增成交。

## 核算及待明确边界

- 九项成本：年度、月度、旬内、D-3、D-2、日前现货、实时现货、罚款、储能退化。卖出收入按负成本计。
- 客户成本/组合收入按分时电量×平均买/卖价分摊；客户实际账单另按其套餐计算。页面同时展示分摊收入与真实应收，二者不混用。
- 推荐成本为签约前价格代理，不是未来采购最优结果；不亏损条件只约束期望值。稳定性阈值是可编辑演示策略，不是监管规则。
- 交割范围用日均96点代表曲线，不是逐日全年联合情景树。标的日等效结算不等于正式月结；月度累计实绩、合同日历分解和考核规则仍需实际业务口径。
- 储能默认8条代表场景优化，再按全部原场景评估；gap只属于代表场景问题。默认软偏差考核可付费越过10%带，硬约束可配置但不保证任何输入都可行。
- P10–P90为模型情景区间，尚未以真实历史数据校准。分层λ曲线不保证单调，不是整体联合模型的全局有效前沿。

## 本次验证

2026-09-28：33项Python测试通过（32项全量回归及新增接口路由测试；改动后6项案例/接口测试再次通过）。新案例页面和原独立工作台通过模拟DOM/API交互检查。独立构建包在/tmp真实运行储能MILP与报告渲染，45个模块全部来自独立构建目录，无同级pifa依赖。

验收案例 `resource/output/20260928-154230-5b5abcd7/`：三客户F/S/L，16条原始场景，完成事前全节点、五λ复算和模拟实绩结算。储能充8.9302MWh、放7.5585MWh；成本150,550.88元、收入163,032.99元、利润12,482.10元，客户节省40,775.45元。测试另外核验了日内滚动和锁定前缀。

真实HTTP浏览器验收尚未完成：本环境禁止监听端口，申请启动127.0.0.1服务又被自动审批组件以 `gpt-5.6-luna is not supported` 拒绝。函数、模型和模拟DOM检查不能替代真实浏览器验收。
