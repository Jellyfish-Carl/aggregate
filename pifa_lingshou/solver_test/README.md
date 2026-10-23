# pifa_lingshou 求解测试

## 单阶段 request 与运行

打开 `solver_test/request_generation.py`，修改顶部的 `NODE`、`CUSTOMER_COUNT` 和 `SCENARIO_COUNT`，运行后会在终端显示 request 完整地址。当前 `NODE` 支持：

- `ANNUAL`：L1 年度采购
- `MONTHLY`、`TEN_DAY`：L1 月度和旬度采购
- `D-3`、`D-2`：L2 滚撮
- `L3-A`：日前现货价格公布前，按价格区间场景联合优化申报曲线和储能计划
- `STORAGE-DA`：日前现货价格公布后，读取 L3-A 的申报 response 并单独求解储能

本批生成配置默认为10个客户，每个客户仍有独立储能；10不是运行限制。模拟输入支持1至100个客户、1至500个场景，批量生成的默认场景档位为5、20、50、100、200、300、500。已有 request 的客户数由 `inputs.customers` 决定、场景数由 `inputs.scenario_count` 决定，`run.py` 不要求客户数为10，也不要求场景数属于上述七档。新的测试 request 会把所选数目用于实际求解，不会把储能优化场景静默压到8个。benchmark 中的数量若与实际 inputs 不一致仍会报错，避免统计失真。

将 request 路径作为命令行参数传给 `run.py`（也可在 IDE 使用顶部 `REQUEST_FILE`）；两处都不提供路径时，会按 `request_generation.py` 中的客户数和单请求场景数配置生成并只求解一个 ANNUAL request。普通模式每次只运行一个指定 request，不自动重跑上游，也不会因为 request 之前运行过而跳过；每次都会新建一条 `run_时间戳` 记录。求解结束或失败时都会打印本阶段的统计表，失败时变量数/GAP 等未知值显示 `N/A`，不会填成0。HiGHS 求解日志逐行输出到终端；MILP 时限为60秒。统计与 response 持久化在 `resource/output/solver_test/response_NN/response_.../run_时间戳/`。

需要明确运行完整 7 阶段 × 7 场景数矩阵时，仍使用 `run.py`，显式添加 `--matrix`（不添加时仍只运行单个 request）：

```sh
pifa_lingshou/.venv/bin/python pifa_lingshou/solver_test/run.py --matrix
```

它按每个场景数依次求解 ANNUAL → MONTHLY → TEN_DAY → D-3 → D-2 → L3-A → STORAGE-DA；后续阶段只读取前一阶段已保存的 response，不重复求上游。每次单阶段仍打印原生日志与上面的表格；另在矩阵目录保存汇总 `manifest.json` 和终端副本 `terminal.log`。由于 100–500 场景的日前 MILP 可能达到 60 秒限制或没有整数可行解，批量执行会如实保留状态并以失败退出，不会缩减所求场景数或伪报成功。

request 保存在 `resource/input/solver_test/request_NN/`。`NN` 是测试批次编号，与阶段无关：每批测试包含同一客户数、场景数和种子的年度、月度、旬度等阶段，输出对应 `response_NN/`。新年度测试自动递增编号；后续阶段沿用上游 response 所属批次；矩阵中的每档场景数各分配一个新批次。再次运行已有 request 只增加该批次的 `run_时间戳` 记录。脚本按阶段分别使用 `annual`、`monthly`、`ten_day`、`d3`、`d2`、`day_ahead_declaration`、`storage_day_ahead` 命名。非年度节点需要填写前一阶段 `response.json` 的地址；request 会记录依赖路径，`run.py` 会自动读取。测试入口将实际 MILP 时限设为 60 秒、目标相对 Gap 为 3%。这不是端到端耗时上限：建模、单独的定价 LP、写文件均需额外时间；旧版 HiGHS 内部操作也可能延迟检查时限。HiGHS 在独立子进程运行，外部 watchdog 有额外 30 秒返回/清理宽限。达到时限不代表已找到整数可行解，亦不保证 Gap 达到 3%。

表格和最终 `response.json` 使用同一份计时快照：总耗时从读取 request 前开始，包含建模、求解、前序结果写入和附属报告生成；不包含最终统计文件写入及终端汇总打印。其他处理时间 = 总耗时 − 求解时间；D-3/D-2 的求解时间包含 MILP 和固定整数后的定价 LP，两者分别记录在 response 中。无法获得整数解时 Gap 显示 `N/A`，未进入求解器的下游阶段不会伪造变量数和求解时间。

续跑同一矩阵时使用 `run.py --matrix --output-dir <已有矩阵目录> --resume`，只跳过该清单中已经验证的 request；失败项会生成新 request 并重试，旧 response 保留。回归测试放在 `problem_solver/test/test_solver_entry.py`，不是 request 运行依赖。

运行入口均可在项目根目录执行：

```sh
pifa_lingshou/.venv/bin/python pifa_lingshou/solver_test/request_generation.py
pifa_lingshou/.venv/bin/python pifa_lingshou/solver_test/run.py
```
