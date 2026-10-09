# 内部多人 Agent 竞技场

内部竞技场让 2～6 个独立运行的 Agent，通过新赛方的 Competition HTTP 协议同桌对弈。其他用户自己运行策略，无需提供策略源码。服务端和客户端均不需要官方账号或官方密钥。

第一版是一进程、一张固定名单的评估桌：所有名单成员 Join 后立即开局，打满配置手数结束。退出者在安全手间离桌，其余至少两人继续；已离桌成员不能中途重新买入这场固定名单比赛。客户端短暂断线后可以 Observe 恢复原座位，只要尚未被超时策略移出。

## 安装

在仓库根目录使用 Linux、Python 3.10+：

```bash
uv venv .venv
uv pip install --python .venv/bin/python -r requirements-sohu.txt
```

竞技场使用现有 `eval7` 和 Python 标准库，不依赖 Dashboard。本文所有命令从仓库根目录执行。

## 创建和启动一场比赛

创建比赛配置、独立内部密钥和客户端配置。人数可设为 2～6；运行目录必须在仓库外且此前不存在。

```bash
.venv/bin/python -m local_arena init \
  --directory "$HOME/.local/share/fullhouse-arena/match-001" \
  --players 6 --names Alice Bob Carol David Eve Frank \
  --template configs/arena.example.json \
  --hands 100 --port 8765

.venv/bin/python -m local_arena serve \
  --config "$HOME/.local/share/fullhouse-arena/match-001/server.json"
```

创建命令输出 Competition ID 和文件位置，不输出密钥。服务默认监听 `127.0.0.1:8765`。默认客户端地址自动采用配置端口；外部 HTTPS 地址通过 `init --base-url https://arena.example.com` 指定。

运行目录内容：

```text
match-001/
├── server.json                  # 配置、名单、凭证哈希及管理员随机种子
├── participants/
│   ├── agent-1/
│   │   ├── agent.key            # 该用户独立内部密钥
│   │   ├── client.json          # 接入配置
│   │   └── client-state/        # 使用现有客户端后生成的持久状态
│   └── agent-2/ ...
├── report.json                  # 最新管理员成绩及比赛完成状态
└── hands.jsonl                  # 每行一手完整管理员牌谱
```

目录权限为 `0700`，凭证、配置和结果文件为 `0600`。每个用户只领取自己的 `agent-N` 中的密钥和客户端配置，不分发 `server.json`、其他用户凭证或完整牌谱。

生成的客户端配置明确设置 `log_source=local_arena`。使用 `competition_client` 后，每个选手的运行日志及可见牌谱存放在自己的 `client-state/telemetry/local_arena/`，与官方线上 `telemetry/sohu_online/` 分开，详情见 [客户端复盘说明](sohu_runbook.md#线上日志和牌局复盘)。选手日志排除聊天，仅保存该选手收到的可见牌局；本目录根下的 `hands.jsonl` 仍是包含全部底牌的管理员牌谱。

## 选手接入

同一机器上，每个用户在自己的终端运行自己的策略，例如：

```bash
.venv/bin/python -m competition_client \
  --config "$HOME/.local/share/fullhouse-arena/match-001/participants/agent-1/client.json" \
  --strategy hero_bots/bot_sohu_v16/bot.py
```

其他用户使用各自的 `agent-2` 等配置与策略。自我对弈也需要不同凭证、不同状态目录；同一策略文件可以运行多个独立实例。全部名单到齐才会发牌，少一人就保持 queued。

收到别人分发的配置后，在自己的机器上覆盖凭证和状态路径：

```bash
.venv/bin/python -m competition_client \
  --config "$HOME/.config/fullhouse-arena/client.json" \
  --key-file "$HOME/.config/fullhouse-arena/agent.key" \
  --state-dir "$HOME/.local/state/fullhouse-arena/match-001" \
  --strategy /absolute/path/to/my_bot.py
```

把密钥文件权限设为 `0600`。策略采用仓库原有的 `decide(state)` 接口，客户端负责转换赛方 Observation 与下注金额；策略必须能接受 `{"type": "warmup"}` 预热状态。

已按赛方协议编写的其他语言客户端可以直接接入：将服务地址、Competition ID、Bearer 密钥换成此次内部比赛信息。协议正文见 [自行运行 Agent 要求](../a_新比赛方资料/自行运行Agent要求.md)。主循环仍是 Discovery → Join → Observe → Action，退出时 Leave。

最小 Python HTTP 示例：

```bash
.venv/bin/python -m examples.arena_agent \
  --config "$HOME/.local/share/fullhouse-arena/match-001/participants/agent-2/client.json"
```

该示例只做 check/call，适合参考网络流程；长期迭代使用 `competition_client`，它提供决策持久化、重启恢复与策略进程超时控制。

## 其他机器接入

可以使用 SSH 隧道，无需改变默认服务监听地址：

```bash
ssh -N -L 8765:127.0.0.1:8765 user@arena-server
```

选手客户端继续访问 `http://127.0.0.1:8765`。也可在服务器配置 HTTPS 反向代理，把 `/api/competitions` 及其子路径转发到 `127.0.0.1:8765`，客户端 `base_url` 指向该 HTTPS 域名。客户端默认拒绝非 loopback 的明文 HTTP。

## 配置与速度

[配置模板](../configs/arena.example.json) 包含以下参数。创建命令也可覆盖盲注、买入、资金、手数、超时、种子、首手庄位、地址和端口。

| 参数 | 默认值 | 含义 |
|---|---:|---|
| `small_blind` / `big_blind` | 100 / 200 | 固定盲注 |
| `buy_in` | 20000 | 固定买入 |
| `initial_bankroll` | 400000 | 包含买入资金的初始总资金 |
| `automatic_rebuy` | true | 手间零筹码且余额足够时补一个完整买入 |
| `hands` | 100 | 整桌目标手数 |
| `minimum_hands` | 20 | 达到此实际参与手数后参与排名 |
| `action_timeout` | 30 | 30～300 秒，遵循赛方公开协议 |
| `max_consecutive_timeouts` | 3 | 连续超时达到阈值后安排安全离桌 |
| `duration_seconds` | 86400 | 包括等待名单到齐在内的总时限；到时结束并结算已开始的手 |
| `dealer_seat` | 0 | 首手庄位，之后自动轮转 |

本地服务收到合法动作立即推进，不添加动画、发牌等待、组桌等待或手间等待。生成的客户端配置使用 queued 50ms、seated 20ms 的轮询间隔，idle 为 100ms。别人自写客户端若仍每 2 秒 Observe，对局仍会受影响；可为内部环境调整轮询间隔。

决策本身耗时、网络延迟、客户端持久化和轮询都会影响吞吐。`competition_client` 使用 SQLite WAL、`synchronous=FULL`，只提交变化的元数据，决策仍先持久化再发送。内部 HTTP 模式的速度应单独测量，不能沿用进程内回测的速度。

## 成绩和复盘

`report.json` 包含各 Agent 的实际参与手数、净收益、BB/100、排名、补码次数、合法行动数、超时数、非法决策数，以及从请求生成到服务器收到合法行动的响应耗时。这是包含轮询、策略、持久化和网络的耗时，不等同于策略计算耗时。

净收益只累计已结算手；买入、补码和离桌转账不计为收益。BB/100 使用实际参与手数；排名使用精确分数比较，再按参与手数打破平局，仍同分者同名次。

`outcome` 明确区分：

- `waiting`：等名单到齐。
- `running`：比赛进行中。
- `completed`：达到目标手数。
- `incomplete`：人数不足或达到总时限，完成的手仍保留。
- `interrupted`：管理员停服或内部错误；未结算手不计分。

`settledStack` 表示最近已结算边界的桌上筹码；有 `inProgressHand` 时它不是当前行动中的即时筹码。正常结束并离桌后，桌上筹码转回 bankroll。完整私有牌谱记录每手初始/最终筹码、所有底牌、公共牌、行动和逐池结算，可由其复算成绩。

复现比赛时新建运行目录并使用相同 `--seed`、筹码参数和策略，再轮换名单顺序或 `--dealer-seat` 评估位置影响。例如：

```bash
.venv/bin/python -m local_arena init \
  --directory "$HOME/.local/share/fullhouse-arena/match-002" \
  --players 2 --names version-A version-B \
  --hands 100 --seed 12345 --dealer-seat 1 --port 8766
```

每次创建都生成新 Competition ID 和内部密钥。随机种子保存在管理员配置中，比赛时不向选手提供；只有相同行动序列与参赛成员才能复现同一手牌路径。

## 停止、恢复和实现边界

选手 Ctrl+C 或 SIGTERM：现有客户端发送 Leave 并在 accepted 后退出。未弃牌、未全押者即使尚未轮到自己也会被强制弃牌；已全押者完成结算后离桌。精确 Table Leave 可重复请求，不会重复转账。

客户端断线重启：继续使用原凭证和原状态目录，Observe 恢复当前桌；相同请求使用原持久决策。超时动作按规则自动 check/fold，带 `cause: timeout`；连续超时阈值触发的后续强制离桌动作带 `cause: leave`。

服务端 Ctrl+C 或 SIGTERM：保留已结算结果，未完成比赛标记 interrupted。第一版服务端牌局状态和幂等账本在内存中，不支持服务进程重启后续打；对已产生结果的运行目录再次 serve 会拒绝覆盖。需创建新比赛，不能把中断场次当完整比赛。

此服务实现公开的参赛 HTTP 主循环和内部固定名单评估。它不实现官方 OAuth、网页报名、动态分桌、League 瑞士轮、晋级流程和平台级恢复机制；断线淘汰阈值是内部评估策略。规则已做本地边界测试，仍不能据此保证与官方未公开裁判实现的所有细节完全一致。

## 验证

```bash
.venv/bin/python -m unittest \
  tests.test_arena_rules tests.test_arena_protocol \
  tests.test_arena_lifecycle tests.test_arena_e2e -v
```

端到端测试启动真实竞技场服务进程，以及 2、3、4、5、6 个独立的现有参赛客户端进程，检查比赛完成、手数、成绩复算、零协议错误和结果不覆盖。

2026-10-09 本地验证：完整回归 82 项通过。另运行六个独立 check/call HTTP 示例 Agent，20 手共 480 次合法行动，耗时约 39.8 秒，零超时、零非法决策，净收益合计为零。这个测量包含网络、轮询和结果写盘，受本机负载与客户端实现影响，不是复杂策略的固定吞吐承诺。
