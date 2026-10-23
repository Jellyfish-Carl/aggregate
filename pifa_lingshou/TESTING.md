# 测试组织

Python 回归测试按被测组件就近放置：`problem_solver/test/`、`service/test/`、`web/test/`。浏览器冒烟检查放在 `web/test/`，报告渲染检查放在 `utils/report/test/`。这些是验证代码，不属于运行时 `utils`。

可执行的 request 生成和模型求解入口位于独立的 `solver_test/` 目录：`request_generation.py` 生成 request，`run.py` 求解并保存 response 到 `resource/output/solver_test/`。使用方式见 [solver_test 使用说明](solver_test/README.md)。
