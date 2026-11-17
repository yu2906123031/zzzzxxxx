# INIT $100 trial

`init_trial.py` checks public INIT perpetual books on Hyperliquid and Aster.
It can run one simulated pair with `--paper`. It cannot send real orders.
It does not alter the main radar configuration or execution ledger.

```powershell
# Public preflight; fee values below are assumptions, replace with account rates.
python init_trial.py --hl-fee-bps 5 --aster-fee-bps 5

# One paper trial on live books, including a 30 second entry confirmation.
python init_trial.py --paper --seconds 120 --hl-fee-bps 5 --aster-fee-bps 5
```

Both legs use equal INIT quantities and each is capped at $100 notional.
Four-sided depth, a 10 BPS protection and 25% depth participation can reduce size.
Entry requires at least $0.10 estimated profit after the existing cost reserves.
This trial explicitly assumes an exit spread of zero; it does not establish
historical mean reversion or guarantee that the opening gap will close.
Immediate roundtrip gross PnL is reported separately.
USDT and USD are compared at 1:1; funding is a reserve, not actual settlements.
Simulation sizing precision is inherited from PaperExecutor and is not a live
exchange quantity/price precision adapter.

Paper exits use the existing $0.10 take profit, $1 stop loss and 60 second
maximum holding rules. A closing request is also made near the trial deadline.
Network failures can leave the simulation incomplete, explicitly reported as
`INCOMPLETE_PAPER_POSITION`; its SQLite ledger is retained. Each invocation
creates a separate trial and does not resume a previous trial.

Reports and the isolated paper ledger are saved under `output/init-trial/`.
An unavailable/invalid/stale market blocks the trial.

Public endpoint references:
- https://asterdex.github.io/aster-api-website/futures/market-data/
- https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint
