# INIT 小额实盘试跑

`init_live.py` 接入 Hyperliquid 主网及 Aster V3 主网，做多 INIT / 做空
INITUSDT。默认仅做账户和盘口检查，带 `--live` 执行一次真实交易。
这是独立执行入口，不会改动雷达或模拟账户配置。

## 本机配置

环境已有依赖；新环境安装：

```powershell
python -m pip install -r requirements-live.txt
Copy-Item .env.init-live.example .env.init-live
```

在本机编辑 `.env.init-live`：

- `HL_ACCOUNT_ADDRESS`：有 USDC 永续余额的账户地址。
- `HL_API_PRIVATE_KEY`：已由该账户授权的 Hyperliquid API 钱包私钥。
- `ASTER_USER_ADDRESS`：Aster 主账户钱包地址。
- `ASTER_API_PRIVATE_KEY`：已获授权的 Aster V3 API 钱包私钥。

使用 API 钱包，避免使用主钱包私钥；不要将密钥发到对话中。
进程环境变量优先于文件配置；密钥不写入账本或日志。
这四项是签名所需配置，不是传统的 HMAC API key/secret。
Entropy Profile 的资金账户公开地址同样可用于 Hyperliquid 查询，不能将 API
签名钱包地址当作资金账户。统一账户模式从 spotClearinghouseState 读取 USDC
余额及冻结金额，并以维护保证金后的可用额度进一步限制；默认模式仍读永续
账户余额。Portfolio margin 可显示 USDC，但本试跑不支持该模式下的实盘开仓。
两个账户分别需要至少 $102 可用保证金。Aster 需预先设为单向持仓模式；
程序不改变账户持仓模式。进入实盘等待时，将 INIT 设置为 1 倍 cross 杠杆。
拒绝接管已有 INIT 仓位或 INIT 挂单；运行期间不要用其他终端交易 INIT。

本次优先且仅使用这两个平台：
- Hyperliquid INIT：https://app.hyperliquid.xyz/trade/INIT
- Hyperliquid API 钱包授权：https://app.hyperliquid.xyz/API
- Aster API 钱包：https://www.asterdex.com/zh-CN/api-wallet ，选择 Pro API。
  V3 API 基础地址使用官方 Overview 中的 `https://fapi.asterdex.com`。

## 运行

```powershell
# 公共接口连通性，无签名、不下单
python init_live.py --public-check

# 使用本机凭据，只读账户检查，读取实际账户吃单费率和盘口
python init_live.py

# 仅查询两边余额；不足 $102 时仍显示余额，不下单
python init_live.py --balances

# 一次真实资金试跑，最多等待信号 120 秒
python init_live.py --live --wait-seconds 120
```

启动缺少凭据、接口返回 403、盘口过期或保证金不足均会阻止开仓。
此前示例域名 `https://fapi3.asterdex.com/fapi/v3/exchangeInfo` 返回 HTTP 403；
已修正为官方 Overview 的 `https://fapi.asterdex.com/fapi/v3/exchangeInfo`。
必须以当前公共检查和账户预检结果判断能否执行。
代码按官方 V3 接口编写，尚未获得真实账户成交验收。

## 本次交易规则

- 每腿名义上限 $100（不是 $100 保证金放大后的名义），相同 INIT 数量。
  按双方 LOT_SIZE/数量精度求共同步长，并检查最小名义金额。
- IOC 限价保护 0.1%，最多使用可见盘口 25%；同时考虑两边退出深度，
  允许实际规模小于 $100，不会强吃满额。
- 使用账户真实费率，估计开平仓四次手续费，另扣执行及资金费储备。
  预估净收益至少 $0.10 且持续 30 秒才尝试下单。
- 收益情景假设退出价差归零，不代表已锁定利润，也没有证明历史均值回归。
  USDT/USDC 按 1:1 比较。
- 先做空较薄的 Aster 盘口，确认成交量后才到 Hyperliquid 对冲。
  未成交不补买；部分成交过小、价差消失、对冲不等量时尝试减仓退出。
- 对冲后按实际订单成交金额、费用及可退出 VWAP 估值，保守平仓收益达到
  $0.10、亏损达到 $1 或持有 60 秒时尝试退出。价格跳空、断网和限价不成交
  都可能使实际亏损超过阈值，止损不是金额保证。
- 止盈估值要求在 IOC 限价保护及 25% 深度参与上限内可退出全部仓位。
  保护范围内深度不足会转入恢复减仓，不把更深盘口价格当作已可平仓。
- 所有退出订单 reduce-only。最多进行 10 轮恢复尝试；剩余敞口或未知订单
  明确标为 `RECOVERY_REQUIRED`，必须继续处理，不能视为已平仓。

## 账本和中断恢复

固定账本 `data/init-live.sqlite`，进程锁 `data/init-live.lock`。
订单意图在发送前落盘；每次创建唯一客户端订单号。请求超时只查询原单，
不重复开仓。无法确认的订单不会按“未成交”处理。
不同平台分别恢复，单个平台查询失败不阻止另一平台尝试 reduce-only。
账户仓位与本程序成交账本不一致时停止操作该仓位，避免减掉外部交易。

中断后重新运行相同 `--live` 命令：只核对原单并尝试退出，不再开新仓。
已结束的账本再次运行也不会开始第二笔交易。
进程被强制结束或机器断网时无法自动保护账户，需要恢复进程或手动处理。
不要在仍有仓位或未知订单时删除账本。

`CLOSED` 表示订单均已确认且两边仓位为零。
`cash_pnl_excluding_funding` 是成交现金流扣交易手续费，未计实际资金费结算。
程序没有把预测收益当作已实现净收益，也不执行转账、充值或提现。

验证：

```powershell
python -m unittest test_init_live test_init_trial test_depth_risk test_execution test_execution_runtime
```

接口依据：
- https://asterdex.github.io/aster-api-website/futures-v3/general-info/
- https://asterdex.github.io/aster-api-website/futures-v3/account&trades/
- https://asterdex.github.io/aster-api-website/futures-v3/market-data/
- https://github.com/hyperliquid-dex/hyperliquid-python-sdk

## 2026-11-17 测试复查

61 项回归测试通过，覆盖授权失效、统一账户余额、成交确认与去重、
请求超时不重复下单、部分成交、异平台独立恢复、重启减仓、平仓深度、
净利润/亏损/持仓超时退出。真实账户只读预检通过，没有发送真实订单。
修正了平仓保护深度、API 钱包授权和入场缩量后的收益重新计算，并增加
订单身份核对。当前依然是双边 IOC，不包含 Post-only 挂单逐笔对冲；
资金费依然使用储备估计，尚未接入实际资金费结算。这些测试不能替代
真实成交及故障恢复验收，也不能保证执行后的收益为正。
