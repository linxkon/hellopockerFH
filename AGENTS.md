# 项目接手与迭代指南

本文是后续 Agent 的快速入口，介绍架构、数据边界和工作流程。具体策略假设、参数建议、对手判断和实验收益放在独立实验记录中，不作为本文的长期指导。架构或入口变化时同步更新本文；历史结论应结合当前代码、规则和数据重新判断。

## 先了解什么

这是一个德州扑克策略研发项目，包含历史 Fullhouse 策略与评估工具，以及搜狐 Competition 协议客户端、比赛规则评估和内部多人竞技场。

- 线上接入、启动停止、日志和复盘：[docs/sohu_runbook.md](docs/sohu_runbook.md)。
- 本地多人 HTTP 对弈：[docs/local_arena.md](docs/local_arena.md)。
- 赛方协议与规则：[自行运行 Agent 要求](a_新比赛方资料/自行运行Agent要求.md)、[比赛规则](a_新比赛方资料/比赛规则.md)、[比赛要求](a_新比赛方资料/比赛要求.md)。
- 历史策略演进与实验背景：[README.md](README.md)。其中的策略结论有特定比赛和样本背景，不代表当前环境的默认最优方案。
- 历史验收：[docs/sohu_acceptance.md](docs/sohu_acceptance.md)；配置与版本记录位于 `configs/`。验收记录、示例参数和版本清单不能代替当前运行状态或官方 Discovery。
- 具体迭代记录：[docs/iterations/README.md](docs/iterations/README.md)。先查看索引，再按当前任务读取相关记录；新一轮使用 [迭代模板](docs/iterations/TEMPLATE.md) 建立独立文件。

开始修改前查看 `git status`，保留已有未提交工作；先确认任务涉及的入口、实际加载的策略文件和运行环境。

## 架构与职责

| 模块 | 职责与定位 |
|---|---|
| `competition_client/__main__.py`、`config.py` | CLI、配置、凭证、单凭证运行锁和组件生命周期 |
| `competition_client/http_client.py`、`protocol.py` | Competition HTTP 通信、服务端响应与动作校验 |
| `competition_client/driver.py` | Discovery → Join → Observe → Action 主循环，以及截止时间、重试、恢复和 Leave |
| `competition_client/adapter.py` | 官方 Observation 与既有策略状态之间的转换，包括座位、行动历史、下注金额和合法动作 |
| `competition_client/strategy_worker.py` | 持久子进程加载策略、预热和限时决策；策略异常或超时由客户端处理 |
| `competition_client/decision_store.py` | 提交决策的持久化、幂等与恢复，使用 `decisions.sqlite3` |
| `competition_client/replay_store.py`、`public_stream.py`、`replay.py` | 牌局与决策记录、官方公开桌面流补充、只读复盘统计及导出 |
| `hero_bots/`、`bots/` | 策略版本与对手实现；`opponent bots/`、`advanced testing bots/` 也包含评估对手 |
| `engine/game.py`、`engine/tournament.py` | 本地牌局执行与赛事模拟 |
| `engine/competition.py`、`engine/sohu_eval.py` | 搜狐规则配置、计分和不同赛制的本地评估 |
| `backtest.py`、`analysis.py`、`app.py` | 回测与 A/B 入口、结果分析、Streamlit Dashboard |
| `scripts/evaluate_iteration.py`、`replay_iteration.py`、`audit_iteration_traces.py`、`validate_iteration_arena.py` | 冻结六人桌批次、决策输入差异审计与六客户端 HTTP 验证；实验配置和结论放入迭代记录 |
| `local_arena/` | 供 2～6 个独立客户端接入的内部 Competition HTTP 服务，含裁判、生命周期和管理员报告 |
| `configs/`、`presets/`、`tests/` | 客户端及评估配置、对手场配置、单元与集成测试 |

线上决策路径：官方 Observation → 协议校验 → 适配为策略 state → worker 调用 `decide(state)` → 转换与校验动作 → 持久化 → 提交。

当前 `hero_bots/bot_sohu_v16/bot.py` 是适配入口，加载 `hero_bots/bot_v16_squeeze_C_metacap/bot.py`，并调整行动历史重建。分析行为或记录版本时须同时检查包装层和被加载实现；实际策略以所用配置及启动参数为准。策略还需支持 `{"type": "warmup"}` 预热。

复盘记录与动作恢复分开：前者用于研究，后者保证提交一致性。官方公开桌面流补充 Observe 可能漏过的摊牌和结算，目前只用于记录，不传给在线策略。

## 环境与数据边界

| 环境 | 入口及数据 |
|---|---|
| 官方线上 | `competition_client` 配合 `configs/sohu.training.json`；记录在配置的 `state_dir/telemetry/sohu_online/` |
| 内部竞技场选手 | `competition_client` 配合竞技场生成的选手配置；记录在各自状态目录的 `telemetry/local_arena/` |
| 内部竞技场管理端 | `local_arena`；独立比赛目录中的 `report.json`、`hands.jsonl`，报告标记 `local-sohu-arena-v1` |
| 线下自评估 | `backtest.py` 或 `engine.sohu_eval`；结果在 `results/` 或指定输出路径 |
| 其他 Competition 服务 | 客户端记录标记 `competition_custom`；使用独立状态目录 |

- 按来源、比赛、桌、手牌和运行版本关联数据，不能把线上、线下及管理员视角混为同一批样本。不同服务或比赛使用独立状态目录。
- 线上日志保留收到的可见牌局信息、决策输入输出、提交结果与运行版本；聊天不记录、不统计。完整字段和导出说明见运行文档。
- 未公开底牌和未收到的结算保持未知。记录可能因启动时间或断线不完整，缺失收益不能填零；报告应说明样本范围、结算覆盖率和统计分母。
- 桌外 bankroll、桌上筹码、逐手净收益和官方 standing 是不同概念；买入、补码和退出转账不能直接算作盈利。收益计算需处理逐池支付及未匹配下注退还。
- 复盘库中的对手统计，不等于在线策略实际使用的统计。当前基线按玩家标识维护进程内 `PLAYER_STATS`，在决策时更新；进程重启会丢失这些内存信息，复盘数据不会自动回灌。分析时核实身份键、采样时机和真正消费统计的代码。
- 竞技场管理员牌谱含全部底牌。回放决策或构建对手画像时，只能使用该决策时刻允许获得的信息，避免未来行动、后续公共牌、摊牌或完整牌谱泄漏。

## 常用入口

以下命令从仓库根目录执行。客户端和竞技场依赖见 `requirements-sohu.txt`，Dashboard 等依赖见 `requirements.txt`。优先使用已有 `.venv`。

```bash
# 只读分析已有线上日志，不连接比赛，也不读取凭证
.venv/bin/python -m competition_client.replay --config configs/sohu.training.json

# 导出到仓库外的新目录；使用尚不存在或为空的输出目录
.venv/bin/python -m competition_client.replay \
  --config configs/sohu.training.json --include-snapshots \
  --output "$HOME/.local/share/fullhouse-replays/review-001"

# 查看本地评估和竞技场参数
.venv/bin/python backtest.py --help
.venv/bin/python -m engine.sohu_eval --help
.venv/bin/python -m local_arena --help

# 完整测试，含启动本地服务和多客户端的集成测试
.venv/bin/python -m unittest discover -v
```

线上启动命令是 `.venv/bin/python -m competition_client --config configs/sohu.training.json`，执行后会实际参赛。追加 `--check` 只做 Discovery 和启动检查，不 Join，但仍涉及网络、策略预热及运行锁；不能作为完全离线的检查命令。

线上配置的相对策略路径按启动目录解析。凭证、运行状态和私有牌谱留在仓库外，不输出或提交密钥。同一凭证只能运行一个 driver，不通过修改状态目录或删除锁绕过限制。

## 一般迭代流程

1. **确认现状。** 确定目标规则、数据来源、当前基线、代码版本与已有进程，区分协议问题、数据问题和策略问题。
2. **整理证据。** 固定一份可复查的数据快照，沿牌局 → 决策输入 → 策略分支 → 提交结果追踪。把已证实事实、推测与未知分别写清楚。
3. **提出可验证假设。** 说明预期改善的现象、适用场景和评价指标；优先采用能独立比较的改动，保留可复现基线。
4. **实现并验证正确性。** 按改动范围检查协议、金额语义、合法动作、信息可见性、时间预算及状态恢复；为实际行为变化补充必要回归测试。
5. **评估收益与代价。** 在一致规则下对比基线与候选，记录种子、座位、对手组合、手数、代码和配置。结合多组样本、波动及运行成本判断，避免由少量输赢或单一对手场决定结论。
6. **验证接入后再应用。** 进程内回测用于策略比较，local_arena 用于 HTTP 接入与多人生命周期验证，线上观察用于核实真实环境表现；三者不能互相替代。根据任务授权应用已验证版本，并保留回退依据。
7. **留下实验记录。** 记录问题、证据、改动、验证命令、结果、限制与后续待确认事项。具体优化方向和参数放在该记录中，只有稳定的架构或流程变化进入本文。

历史日志可以验证已发生决策的行为和信息使用；更换动作后对手的后续反应未知，不能直接用原牌谱结局当作候选策略的收益。

策略收益评估以 **6 人桌**为标准：1 个候选版本，对阵由 2～3 个经验证的历史强版本组成的 5 个独立实例；轮换人数配比和座位，并将候选与固定基线分别放入相同对手场做配对比较。版本选择、评估步骤与现有命令见 [六人桌评估方案](docs/six_player_evaluation.md)。具体入选版本和成绩放在迭代记录中，不在本文固定排名。

### 用迭代记录衔接每轮工作

- 每轮具体策略或支撑能力的优化，在 `docs/iterations/` 创建一份记录，贯穿方向、问题、计划、执行结果、测试评估和下一步；开始时写计划，执行中补证据，结束时写结论，不把未执行内容记为完成。
- 接手时先核对记录与实际代码、配置是否一致。根据相关历史记录的证据和未解决问题，自行提出本轮假设；历史建议可被新证据推翻，不自动继承为任务或策略要求。
- 在当前任务授权与资源范围内推进“分析 → 实现 → 验证 → 评估 → 保留、继续验证或放弃”的循环。结论同时说明是否已应用到线上；实现、测试通过和上线是不同状态。
- 收尾时更新目录索引，并留下下一步要验证的问题、所需证据和继续条件。下一轮通过前序记录链接承接；无效实验也保留，避免重复试错。待办建议本身不触发无限实验或自动上线。

## 修改与运行约定

- 改动前先读相关实现与测试；协议金额可能区分本次增量和本街累计，不凭动作名称猜测语义。本地模拟规则也需与目标官方规则核对。
- 优先运行与变更相关的测试；涉及跨模块协议、计分或竞技场生命周期时扩大回归范围。测试通过说明验证范围内行为正确，不直接证明策略更优。
- 已运行的客户端不会自动加载文件修改。文档、离线分析或回测任务无需重启线上进程；需要应用新代码时，按任务授权先正常 Leave，确认退出后使用原恢复状态启动。
- 不删除 `decisions.sqlite3` 绕过已保存的提交，也不改写已提交动作。复盘库与实验输出同样保留原始证据，不覆盖旧实验来隐藏差异。
- 不在本文固定进程 PID、当前收益、临时对手画像或未经验证的策略建议。接手时查询实际状态；服务是否仍在运行、日志是否继续写入应以现场检查为准。
