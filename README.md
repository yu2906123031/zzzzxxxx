# 跨平台永续价差工具

Python 3.12 项目，包含公开盘口价差雷达、订单级模拟执行，以及独立的
Hyperliquid INIT / Aster INITUSDT 小额实盘入口。

## 快速开始

雷达和模拟执行使用 Python 标准库：

```powershell
python arb.py
```

打开 http://127.0.0.1:8787 。策略参数见 `config.json`；雷达中的执行为模拟。
部分行情接口在 Windows 证书链异常时需要 Node.js 提供根证书，始终验证 TLS。

## INIT 独立执行入口

```powershell
python -m pip install -r requirements-live.txt
Copy-Item .env.init-live.example .env.init-live
# 在本机填写 API 钱包凭据，不要提交 .env.init-live
python init_live.py --public-check
python init_live.py --balances
python init_live.py
```

以上命令不下单。明确执行以下命令才会进行一次真实资金试跑：

```powershell
python init_live.py --live
```

实盘规则：1 倍杠杆，每腿名义上限 $100，相同 INIT 数量；先在 Aster 做空，
确认实际成交量后在 Hyperliquid 做多。使用带 0.1% 价格保护的 IOC 限价单，
深度不足会缩量。不是 Post-only 挂单后逐笔对冲。

平仓估值扣除成交手续费、预计退出费用以及执行和资金费储备；预估收益达到
$0.10、亏损达到 $1 或持仓 60 秒时尝试平仓。资金费尚未按实际结算接入。
价差归零的预测收益不等于已实现利润；止损与限价均不保证成交或最大亏损。
跨平台订单不是原子交易，存在单边成交和恢复失败的可能。

固定账本保存在 `data/init-live.sqlite`；有订单后重启 `--live` 只核对与减仓，
不会再开第二笔。不要在有仓位或未知订单时删除账本。

## 文档与验证

- [实盘配置、风控和恢复](INIT_LIVE.md)
- [实时盘口模拟试跑](INIT_TRIAL.md)
- [模拟执行与演练](EXECUTION.md)
- [DEX 模拟说明](DEX_SIM.md)

```powershell
python -m unittest test_init_live test_init_trial test_depth_risk test_execution test_execution_runtime
```

当前上述 61 项测试通过，真实账户只读预检通过；尚未完成真实成交验收。
模拟或单元测试结果不能证明策略盈利。

## 上传范围

只包含源码、测试、网页、公开配置、文档和空白凭据模板。
真实密钥、资金账户信息、数据库、成交账本、余额报告、运行日志、缓存及
第三方抓取网页不包含在仓库中。
