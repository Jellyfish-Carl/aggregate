# pifa_lingshou：独立批零采购与结算项目

已内置从 pifa 迁入的批发建模、预测、交易、场景与结算模块。运行不需要同级 pifa 目录，也不修改运行时 sys.path。网页流程：创建案例 → 编辑/保存输入 → 套餐与年度采购联合优化推荐 → 确认套餐 → 分阶段采购 → 日前及日内储能 → 标的日结算。

## 入口

### 求解测试入口

独立的 `solver_test/` 目录提供 `request_generation.py` 和 `run.py` 两个工作流入口，分别生成 request、调用模型并保存 response。request/response JSON 位于 `resource/input/solver_test/` 和 `resource/output/solver_test/`。日前申报 `L3-A` 和储能日前 `STORAGE-DA` 是两个阶段：先运行申报，再把它的 response 地址填入储能 request 的 `UPSTREAM_RESPONSE_FILE`。详见 [solver-test 使用说明](solver_test/README.md)。

网页“决策节点请求”可加载、编辑、保存、运行同一份文件，并查看运行历史。MILP 使用 SciPy 调用 HiGHS；终端逐行打印带时间戳的 HiGHS 原生日志，包括 presolve、分支节点、cuts、变量/约束变化、界和 Gap。

**PyCharm 解释器选择项目的 `pifa_lingshou/.venv/bin/python`。** 当前 aggregate 根目录的 `.venv` 缺少 SciPy。若能运行但 import 标红，检查 IDE 索引是否包含包的父目录 aggregate；当前本地配置已修正。

```sh
pifa_lingshou/.venv/bin/python pifa_lingshou/solver_test/request_generation.py
pifa_lingshou/.venv/bin/python pifa_lingshou/solver_test/run.py
```

常规回归：`pifa_lingshou/.venv/bin/python -m unittest discover -s pifa_lingshou -p 'test_*.py' -v`；测试按组件放在 `problem_solver/test/`、`service/test/`、`web/test/` 和 `utils/report/test/`，说明见 [TESTING.md](TESTING.md)。

### 完整 request 的命令行入口

入口 `__main__.py` 读取节点请求，并自动创建独立运行记录：

```sh
pifa_lingshou/.venv/bin/python -m pifa_lingshou \
  pifa_lingshou/resource/input/retail/request_02_20261014_141043_386056/request.json
```

带 `depends_on` 的请求自动查找有效上游；也可在没有依赖引用的请求上用 `--upstream` 指定前序 response。可选 `--output` 是额外导出，正式历史仍会保留。

### test、request 和 response

| 对象 | 职责 |
| --- | --- |
| `request_generation.py` | 调用 mock 数据生成逻辑，生成单节点 request，不求解。 |
| `run.py` | 从 `REQUEST_FILE` 读取 request，调用模型求解并保存 response 与节点可视化。 |
| `resource/input/solver_test/request_NN/*.json` | solver-test 输入请求，按一组决策链分组。 |
| `resource/output/solver_test/response_NN/<response名>/<run时间>/` | 每次独立运行的 `response.json`。 |

```text
request_generation.py → resource/input/solver_test/request_NN/*.json
  → run.py 读取 REQUEST_FILE 和可选上游 response
  → solve_request → run_layer → 对应模型 → SciPy/HiGHS
  → resource/output/solver_test/.../response.json
```

其他节点也由 `run_layer` 分派：L2 使用 MILP 边际估值配合规则撮合；L3-A 在日前现货价格公布前，按价格区间场景联合优化申报和储能；STORAGE-DA 在日前价格公布后锁定申报，并用确定价格重新优化储能；STORAGE-RT 在已执行历史上滚动调整储能。`problem_solver/solver.py` 中的 `DiagnosticPolicyAdapter` 是诊断适配器，不是这条路径的优化入口。

网页完整流程的路径是 `web → CaseService → layer_input → RequestStore → run_layer → 模型 → 保存 request/结果/报告`，还负责推荐、签约、版本和阶段顺序。阶段 test 使用 request 中的套餐选择和依赖，不走网页签约状态步骤。旧 `layer_runner --inputs` 只接收请求内层的参数对象，不能传整个 request；现在误传时会明确报错。

### 网页入口

网页入口为 `web/__main__.py`。在 aggregate 目录执行：

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

依赖 SciPy 内置的 HiGHS 执行 MILP 和固定整数定价 LP。可传 `--resource-dir /可写路径/resource` 分离代码与数据。服务仅监听本机，属于单用户演示应用。

## 文件夹职责

| 文件夹 | 含义与放置原因 |
| --- | --- |
| `web` | 网页入口、HTTP接口、页面/CSS/交互脚本；管理操作与进度，不放优化方程。 |
| `problem_solver` | 优化变量、约束、目标和求解器；L1合同、L2撮合、L3日前申报、储能分别实现。储能的唯一实现是 `storage_milp.py`，同时提供 `dispatch` 和兼容别名 `solve_storage_milp`。 |
| `service` | 输入校验、Mock、预测适配与业务编排；`case_data.py` 定义发布日期、交割范围与客户96点预测。 |
| `data_objects` | 共享客户、合同、场景和状态数据结构，统一层间数据格式。 |
| `accounting` | 批发成本、零售账单、客户分摊及实绩回放；核算逻辑不能放入数据输出目录。 |
| `config` | 可编辑默认值，包括推荐阈值、随机种子和储能参数。 |
| `constants` | 96点/48点等公共时间网格；与业务配置分开。 |
| `utils` | visualize.py负责报告入口，report负责图表模板，resource_store负责原子文件存储。 |
| `resource` | request/response 分别固定在 `input/solver_test` 和 `output/solver_test`；网页案例数据仍由网页流程管理。见 [资源目录说明](resource/README.md)。 |
| `solver_test` | 手动分阶段 request 生成与求解入口；request/response 文件只写入 `resource/input`、`resource/output`。 |
| `*/test` | 各被测组件的自动回归测试和界面检查。见 [测试说明](TESTING.md)。 |
| `docs` | 算法、市场口径、边界与验收记录。 |

可视化在 `utils/visualize.py`；`service/storage_validation.py` 只负责相同合同/申报/情景下储能启停成本对照。

## 按决策节点保存输入和运行历史

```text
resource/
  input/<节点>/request_编号_时间/request.json
  output/<节点>/<request编号>/run_时间/
    source_request.json / request.json / upstream.json
    response.json / run.json / visualize.html（可选）
  workflows/<网页案例ID>/      # 表单版本、签约、结算与流程索引
```

多个请求各自保存，每个请求多次运行各自保留。修改输入后旧结果保留，但不能当作新版本上游；下游默认跟随当前版本最新成功结果，也可用 `depends_on.run_id` 指定运行。完整网页流程签约后条款锁定；日内须提供已执行实绩，不能回退锁定前缀。

## 模型与运行

| 阶段 | 实现与输入 |
| --- | --- |
| 套餐推荐 | 逐客户F/L/S选择与年度L1联合MILP，在客户P10节省、组合期望利润及可配置损失CVaR约束下优化净损失；推荐不直接生成成交订单。 |
| L1 年/月/旬 | 48点五档负荷随机MILP，成本、覆盖考核和CVaR。每节点使用自身交割范围日均96点预测与价格，合同数量按日历天数折算，考核比较统一到当前节点范围。 |
| L2 D-3/D-2 | MILP提供边际估值，逐笔阈值撮合规则决定买卖与部分成交；撮合本身不冒充MILP。 |
| L3-A 日前申报 | 继承锁定合同与已签套餐，联合优化96点日前申报与储能；此时日前价格尚未公布。 |
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
- 推荐与年度L1共用采购、考核、补救成本及净损失CVaR模型，使用签约时可得预测；不能视为全部后续节点的全局最优。组合不亏损底线约束期望值，损失CVaR上限另行配置。
- 交割范围用日均96点代表曲线，不是逐日全年联合情景树。标的日等效结算不等于正式月结；月度累计实绩、合同日历分解和考核规则仍需实际业务口径。
- 储能默认8条代表场景优化，再按全部原场景评估；gap只属于代表场景问题。默认软偏差考核可付费越过10%带，硬约束可配置但不保证任何输入都可行。
- P10–P90为模型情景区间，尚未以真实历史数据校准。分层λ曲线不保证单调，不是整体联合模型的全局有效前沿。

## 本次验证

2026-09-28：33项Python测试通过（32项全量回归及新增接口路由测试；改动后6项案例/接口测试再次通过）。新案例页面和原独立工作台通过模拟DOM/API交互检查。独立构建包在/tmp真实运行储能MILP与报告渲染，45个模块全部来自独立构建目录，无同级pifa依赖。

历史验收记录中的旧网页案例目录已按 2026-10-14 的清理要求删除；这些数字仅为当时的 Mock 验证记录。当前路径、生成和运行方式以本页及 solver_test 说明为准。
