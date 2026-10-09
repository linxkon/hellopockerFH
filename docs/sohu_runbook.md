# 搜狐德扑客户端运行说明

客户端以自行运行为交付路径，使用 `sohu_v16_squeeze_C_metacap` 适配策略。参赛前必须在网页选择“我来运行”并完成 League 报名，确认训练及正式 Competition 的 ID 和配置。仓库内示例筹码参数来自协议样例，不能当作正式比赛参数。

## 安装和本地验证

使用 Linux、Python 3.10 或以上版本，保持系统时间同步。仓库根目录执行：

```bash
uv venv .venv
uv pip install --python .venv/bin/python -r requirements-sohu.txt
.venv/bin/python -m unittest discover -v
```

`eval7` 是必需依赖，正式入口预热失败会停止，不能以策略的依赖退化路径参加比赛。HTTP、SQLite、进程和锁使用标准库。Dashboard 的依赖另见 `requirements.txt`。

## 凭证和配置

将凭证保存到仓库外由当前用户拥有的普通文件，权限设为 `0600`。示例位置是 `/root/.config/fullhouse-bot/agent-key`；不要把密钥粘贴到命令行、配置 JSON、日志或聊天中。

复制 [客户端配置](../configs/sohu.example.json) 到自己的配置位置，将 `competition_id` 替换为已确认的训练 ID。当前已确认的训练配置见 [sohu.training.json](../configs/sohu.training.json)，Discovery 快照为 Play S15，盲注 500/1000，buy-in 50000，initialBankroll 1500000；配置变化时需重新核对。`strategy` 的相对路径从启动目录解析；在仓库根目录启动，或使用绝对路径。不同 Competition 使用各自的 `state_dir`，目录必须在仓库外，权限为 `0700`。`expected_summary` 可填写从官方 Discovery 确认过的字段，例如 `blinds`、`buyIn`、`automaticRebuy`，不匹配时停止。

```bash
.venv/bin/python -m competition_client --config /path/to/training.json --check
```

`--check` 只执行公开 Discovery、服务端时间核对、策略预热和本地启动检查，不会 Join。配置 ID 缺失不会改选其他比赛。`cf638cd1-a992-41fc-9e39-37ccb0449a17` 已由用户确认为训练场；正式 League 仍需另行确认 ID 和报名状态。

## 启动和停止

确认训练目标后启动：

```bash
.venv/bin/python -m competition_client --config /path/to/training.json
```

也可使用 `--competition-id`、`--key-file`、`--strategy`、`--state-dir` 覆盖配置。Scheduled 时等待；Active 后 Join；Idle 每约 30 秒刷新发现并 Join，Queued 每约 5 秒 Observe，Seated 每约 2 秒 Observe。Action 成功响应直接推进主循环。比赛结束但仍在桌上时继续处理当前手，直到 Idle 后保存最终服务端累计成绩并退出。

按 Ctrl+C 或发送 SIGTERM 进入 Leave。等待日志 `Leave accepted; driver stopped` 后再重新启动。Leave 的未知网络结果只重试同一 body；精确 Table Leave 明确 stale 后才改用 Competition ID。收到 accepted 后不会再发任何参赛命令。

运行目录中的 `decisions.sqlite3` 保存决策、请求状态、退出意图、最新 Table ID 和服务端累计成绩，不含 Authorization、密钥、完整 Observation 或手牌。牌局复盘数据单独保存在下述 `telemetry` 目录。每份决策先落盘再发送。重启先 Observe 确认当前请求；相同请求复用原决策，过期或已完成请求归档。未完成 Leave 在重启时优先恢复，完成后此次启动立即退出；下一次明确启动才可重新参赛。不要删除数据库来绕过幂等记录。

同一凭证只能运行一个 driver，锁在 `~/.local/state/fullhouse-sohu-locks` 中，修改 state-dir 不会绕过锁。凭证轮换时保留旧状态，使用独立运行目录并确认旧值守已停止。

线上训练场在成绩尚未建立时返回 `standing: null`，客户端原样保存未知成绩；缺少 standing 字段仍会停止。公开历史中的 fold/check 可以省略 amount，它们固定投入零筹码；盲注、call、bet、raise 和 allIn 的 amount 仍必须存在并为安全整数。这两种真实响应已纳入回归测试。

## 排障和值守

| 情况 | 处理 |
|---|---|
| `authentication_required` | 客户端停止；重新领取凭证并安全替换文件 |
| `registration_required` | 客户端停止；在网页完成所选 League 永久报名 |
| `insufficient_bankroll` | 停止 Join，核对服务端余额 |
| 时钟偏差 | 同步系统时间，重新执行只读检查 |
| 网络中断或 503 | 原样重试；Action 超过截止预算后改为 Observe，不重新决定 |
| `invalid_decision` | 明确拒绝后先 Observe，同一请求仍有效时仅允许记录原因并改成合法 check/fold；再次拒绝则停止 |
| 未知错误、500、非法响应 | 停止并保留数据库；核对日志与赛方协议后再恢复 |
| 策略超时或异常 | 在同一 Observation 的允许动作中选择 check，再选 fold；无安全动作则停止 |

日志输出状态、请求 ID、计算耗时、提交耗时、重试和累计成绩，并脱敏疑似凭证。日志也应保存在仓库外。策略 stdout/stderr 被屏蔽，避免泄露私有牌局。HTTP 默认超时 10 秒；策略计算预算 1 秒、提交预留 2 秒，均为可调整的客户端参数。策略超时会杀掉计算进程，后续请求重新预热，因此超时后对手统计会重置。

## 线上日志和牌局复盘

正常启动客户端时默认启用运行日志和牌局记录，原命令无需增加参数：

```bash
.venv/bin/python -m competition_client --config configs/sohu.training.json
```

已有进程不会自动加载代码变更。需要在原终端 Ctrl+C，等待 `Leave accepted; driver stopped`，再用上述命令启动；此前没有记录的手牌和结算无法补录。`--check` 不创建牌局日志，也不会参赛。

训练配置的文件位于：

```text
~/.local/state/fullhouse-sohu/cf638cd1-a992-41fc-9e39-37ccb0449a17/
├── decisions.sqlite3                 # 提交幂等和恢复状态
└── telemetry/sohu_online/
    ├── replay.sqlite3                # 收到的牌局快照、决策和请求事件
    └── <UTC时间>-<run_id>.log         # 每次启动独立的文本运行日志
```

复盘库保留所有变化的 Observation，包含自己的底牌、可见的对手摊牌、公共牌、牌局阶段、物理座位及庄位、盲注、所有玩家筹码及状态、当前下注、总底池／边池、完整公开行动线、超时／离桌原因、合法动作与截止时间、赢家及逐池支付、未匹配下注退还、桌外 bankroll 和服务端 standing。我们没有行动请求、已经弃牌时收到的快照也会记录；相邻完全相同的快照去重。未公开的对手底牌不会推测或补写。聊天字段递归剔除，不记录也不统计。

官方线上还自动订阅网页使用的公开 `/api/tables/<tableId>/stream` 实时流：现场验证发现 Competition Observe 会直接返回下一手，轮询没有取得中间的 `hand.result`。实时流用独立线程和独立数据库连接保留公开牌局快照、摊牌及结算，不携带 Agent 密钥，不影响行动提交。只取 `table`，丢弃消息列表和聊天；断流自动重连并记录状态，换桌时切换订阅。local_arena 和其它服务不会自动订阅这个线上接口。

导出按 Hand ID 合并两种来源，公开快照中的 null 底牌不会覆盖已经收到的自己的底牌，结算后数据不会被旧的未结算快照覆盖。`snapshots[].visibility` 区分 `agent_observation` 和 `public_table_stream`；后者只带 `table`，不伪造私有 Observation、bankroll 或 standing。公开数据只用于记录与离线复盘，不传给在线策略。

决策事件保存策略实际输入、原始输出、转换后提交动作、决策复用、回退原因、计算耗时及提交结果；请求事件保存重试和错误代码。每次启动记录服务地址、Competition ID、配置参数、Python/eval7 版本和客户端／策略源码 SHA256，不记录密钥、凭证文件位置和 HTTP Authorization。文件权限 `0600`，日志目录 `0700`。复盘记录使用独立 SQLite WAL 库，提交恢复仍使用 `decisions.sqlite3` 的 FULL 持久化；运行中复盘库写入失败会明确报错并停止记录，不改变或阻塞动作提交。

只读查看统计，无需停止客户端，也不读取凭证或请求线上服务：

```bash
.venv/bin/python -m competition_client.replay --config configs/sohu.training.json
```

导出到仓库外的新目录，包含每手收到的快照：

```bash
.venv/bin/python -m competition_client.replay \
  --config configs/sohu.training.json \
  --include-snapshots \
  --output "$HOME/.local/share/fullhouse-replays/online-training-001"
```

导出生成 `sessions.json`（版本及来源）、`summary.json`（统计）、`hands.jsonl`（逐手牌谱）、`decisions.jsonl`（决策）和 `events.jsonl`（请求事件）；目标目录必须为空，避免覆盖已有结果。用 `--hand-id <id>` 查看指定手，或 `--database /absolute/path/replay.sqlite3` 读取独立归档。导出对正在写入的数据库使用一致的只读快照。

统计按 Competition／Table／Hand ID 合并重启记录，同一手行动不会重复计数。包括每位玩家的分街动作、VPIP/PFR、面对下注的弃牌／跟注／加注次数、公开摊牌、超时／离桌动作、已收到结算手的净筹码及 BB/100；短筹码 all-in 跟注不计 PFR，服务端强制弃牌不计主动弃牌率。VPIP/PFR 的分母只使用已结束翻前行动的可见样本。净收益按 `payouts + returnedBets - actions.amount` 计算，排除买入／补码转账。`starting_stacks_inferred` 是首次未结算快照的剩余筹码加已投入金额，不能当成比赛起始资金。

这些统计来自收到的快照，不能保证启动前、实时流断线或轮询间没有漏掉整手或结算。`observation_count` 统计私有快照，`public_snapshot_count` 统计公开流快照。`hands_without_settlement` 明确列出结算缺失；缺失收益为 null，BB/100 仅描述已收到结算的样本，官方成绩始终以 `standing` 为准。离线统计覆盖我们弃牌后收到的行动；在线策略内存中的对手统计仍沿用原有决策时更新方式，记录功能不会改变策略。

不同环境的日志按来源分开：

| 环境 | 标记和位置 | 可见范围 |
|---|---|---|
| 官方线上客户端 | `source=sohu_online`，所选 `state_dir/telemetry/sohu_online/` | 本 Agent 收到的官方快照 |
| local_arena 客户端 | `source=local_arena`，该 Agent 的 `client-state/telemetry/local_arena/` | 该选手的快照 |
| local_arena 管理端 | 独立比赛目录的 `hands.jsonl`、`report.json`；报告 `environment=local-sohu-arena-v1` | 完整管理员牌谱，包括全部底牌 |
| 线下自评估／回测 | 原有 `results/` 或指定 `--output` 的结果 | 本地模拟结果 |
| 其它 Competition 服务 | `source=competition_custom`，`state_dir/telemetry/competition_custom/` | 该服务返回的快照 |

`log_source` 默认 `auto`：官方域名固定为 `sohu_online`，loopback 服务为 `local_arena`，其它域名为 `competition_custom`。`local_arena init` 生成的配置明确写入 `log_source=local_arena`，即使使用 HTTPS 自定义域名也不混成线上训练日志；旧的外部域名竞技场配置可补上这个字段。复盘库还校验来源、服务地址和比赛 ID，拒绝混用；为不同服务使用独立 `state_dir`。线上收益、选手视角数据和本地管理员完整牌谱应分别比较。

正式比赛每轮至少提前 5 分钟启动并持续值守。部署人员应监控网络、进程和服务端 standing，不逐手代替策略决策，也不修改已保存或已提交的 Action。

## 新规则回测

[回测示例配置](../configs/sohu.backtest.example.json) 仅用于本地演练。可替换为已确认的 Competition 参数，或直接使用官方 Discovery 中的 `summary` JSON。实际 BB/100 以每个 Agent 已参与并结算的手数为分母；零手为 null，买入、补买和退出转账不计入收益。资格分母固定为配置总手数的 95% 向上取整，同时满足 minimumHands。

```bash
.venv/bin/python backtest.py eval \
  hero_bots/bot_sohu_v16/bot.py bots/custom/competent_tag/bot.py \
  bots/custom/balanced_tag/bot.py bots/custom/calling_station/bot.py \
  bots/custom/nit_folder/bot.py bots/custom/multi_barrel/bot.py \
  --competition-config configs/sohu.backtest.example.json \
  --hands 20 --matches 24 --rotate-seats --no-ev --json
```

短局评估与完整预赛模拟可保存独立结果：

```bash
.venv/bin/python -m engine.sohu_eval \
  --config configs/sohu.backtest.example.json \
  --bots hero_bots/bot_sohu_v16/bot.py \
  bots/custom/competent_tag/bot.py bots/custom/balanced_tag/bot.py \
  bots/custom/calling_station/bot.py bots/custom/nit_folder/bot.py \
  bots/custom/multi_barrel/bot.py \
  --scenario 30 --matches 24 --seed-base 100 --output /tmp/sohu-30.json
```

`--scenario 20` 和 `30` 会设为一轮短局，返回盈利置信区间、排名分布和首名/前三频率。完全同分且缺少预赛排名时，频率按平均名次描述本地样本，不构成官方晋级判断。`--scenario preliminary` 使用配置总轮数；前三轮随机分组，第四轮起按累计实际 BB/100 分组，每桌不超过配置人数。Bankroll 和对手统计跨轮保留，每场结束 cash-out；前 12 名边界同分时标记需加赛，不按 bot_id 决定。加上 `--include-finals` 会继续模拟平衡分组的两场 20 手半决赛和成绩重置的 30 手总决赛，同分时使用预赛排名；资格人数不足或预赛排名未解决时报告 unresolved。

Dashboard 可选择 `Fullhouse legacy`、`Sohu 20 hands` 或 `Sohu 30 hands`，支持参数输入、串行及多进程评估。新旧结果用环境与参数区分，CSV 保留实际手数。新结果的绝对 BB/100 与比较中的 BB/100 差额均按各自实际手数计算；配对 t、置信区间和 bootstrap 仍以每场净筹码差为单位。样本较小时只能检验接入和计分，不能据此替换基线策略。

## 发布验收

本地测试通过后，在明确的官方训练 Competition 验证连续值守、断线重启、Leave 和服务端 standing。记录比赛 ID、Discovery summary、策略文件版本、依赖、客户端配置和运行机器，然后冻结发布。当前候选文件的哈希与依赖记录见 [候选版本清单](../configs/sohu.release-manifest.json)，验证结果见 [验收记录](sohu_acceptance.md)；正式 League 发布前仍需核对报名、正式参数和非空累计成绩。
