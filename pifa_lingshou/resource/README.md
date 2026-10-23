# 资源目录

网页案例使用按决策节点组织的 `input/<节点>/` 和 `output/<节点>/`。求解测试使用独立且固定的 `input/solver_test/` 与 `output/solver_test/`：request 保留在 input，逐次 response 和对应可视化放在 output。

- `input/<节点>/request_编号_生成时间/request.json`：该节点可编辑输入；`generation.json` 记录来源。
- `output/<节点>/<request编号>/run_运行时间/`：每次运行的源请求、实际求解输入、上游快照、response、状态和可选报告。多次运行保留多份，不覆盖。时间使用 Asia/Shanghai。
- `workflows/<网页案例ID>/`：网页流程状态、表单版本、签约与结算材料；节点结果通过路径引用统一 output，不另存一套节点求解结果。
- `input/solver_test/request_NN/`：按测试批次递增编号，同一批包含年度、月度等各阶段；新测试另建批次。
- `output/solver_test/response_NN/`：对应测试批次的逐次求解 response 和 HTML 可视化；编号与阶段无关。

操作方法见 [阶段求解说明](../solver_test/README.md)。
