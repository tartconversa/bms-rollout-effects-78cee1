# BMS 固件试用的效果判定与扩围控制

`sourcepack` 是合成业务资料及通用资料检查器；`fwtrial` 是在其上的试用分析与
扩围决策工具。字段、单位和业务记录的含义见 docs/资料口径.md，完整性目录为
data/catalog.json；统计口径与操作说明见 docs/方法说明.md。
不得把文件自检成功解释成业务结论成立。

## 环境

Python 3.11.2；分析依赖已在 requirements.txt 固定版本。

    python3 -m pip install -r requirements.txt
    python3 -m sourcepack          # 资料完整性检查（不作业务解释）

## 分析工具 fwtrial

全部命令只读取本地资料，不需要接入真实设备；每次查看、决定、计划与复核都
追加写入台账（默认 out/ledger.jsonl），决定记录观察截止点、代码映射摘要与
核实事实摘要。

    python3 -m fwtrial analyze --cutoff 2025-01-30T08:00:00+08:00   # 效果与不确定性
    python3 -m fwtrial looks                                        # 登记查看轨迹（α 消耗）
    python3 -m fwtrial decide                                       # 扩围申请评估并留痕
    python3 -m fwtrial plan --date 2025-01-31                       # 次日核实安排
    python3 -m fwtrial review                                       # 后期事实触发的复核
    python3 -m fwtrial trace R2                                     # 从扩围决定回查
    python3 -m fwtrial demo                                         # 全流程演示

- 值班人员看 `plan`：当前该检查谁（定向疑点 + 登记抽查），结果回流时须带
  登记概率；未执行的计划不进任何分母，重复核查不增加样本量。
- 安全负责人看 `analyze`/`decide`/`trace`：单位有效运行暴露下的无效打扰、
  漏处置风险与处置延迟，含站内整群不确定性、可比范围与不可识别部分；
  决定分 支持扩围 / 限定规模扩围 / 继续试用 / 暂停 四档，高风险事件永不
  被排除以换取达标。
- 观察截止过滤：任何分析只使用 available_at 不晚于截止时刻的事实。

## 已有检查与语法构建

    python3 -m unittest discover -s tests -v
    python3 -m compileall -q sourcepack fwtrial tests

tests 覆盖资料容器、输入诊断与 fwtrial 的业务规则（口径、估计、决策、
计划、复核与留痕）。运行资料检查与分析不需要访问外部业务系统。
