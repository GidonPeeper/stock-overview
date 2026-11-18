"""Statement importers — turn the files brokers actually hand out into the
data files the connectors read.

  degiro_pdf         DeGiro "Account statement" PDF  -> degiro_account.csv
  traderepublic_csv  Trade Republic "Transaction export" CSV
                                                     -> trades_/income_trade_republic.json
  traderepublic_pdf  Trade Republic account statement PDF -> the same two files
  trading212_pdf     Trading 212 "Activity statement" PDF
                                                     -> trading212_statement.json
  trading212_csv     Trading 212 History CSV export  -> the same file (merged
                                                        onto an earlier statement)

Each module exposes a pure `convert(bytes) -> ...` used by the in-app upload
endpoint, and a `python -m backend.importers.<name> <file>` CLI for local use.
"""
