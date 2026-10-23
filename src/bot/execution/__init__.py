"""Order execution (module 5). The only code that may place real (testnet) orders, through
`bot.exchange.trading`, and every entry goes through `bot.risk.engine.evaluate()` first."""
