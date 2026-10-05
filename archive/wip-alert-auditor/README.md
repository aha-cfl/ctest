# WIP: alert auditor (unfinished, parked)

Started to paper-trade a signal provider's Threads callouts (entry + one standard stop) on live 1-minute
candles and score them in R. Parked when the direction changed to real execution venues and rotating strategies.

State: `parse.py` works on sample phrasings; `prices.py` and `audit.py` are written but untested; no tests,
not wired into the desk or dashboard. Imports `kalshi.feeds` / `kalshi.desk` from the active tree.
