# trader-data

Public data relay for TRADER-BOT.

## trades.json

Updated every 30 minutes by GitHub Actions.
Contains Capitol Trades BUY signals from the last 30 days.

Used by: `bharathiiraj/trader-bot`
Fetched via: `raw.githubusercontent.com`

## Schema

```json
{
  "scraped_at": "2026-06-16T21:00:00Z",
  "trade_count": 87,
  "trades": [
    {
      "politician": "Josh Gottheimer",
      "chamber": "House",
      "party": "Democrat",
      "ticker": "AMD",
      "traded_date": "May 1, 2026",
      "trade_age_days": 15,
      "owner_type": "direct",
      "type": "buy",
      "size": "1K-15K"
    }
  ]
}
```
