# DayZ Discord Bot

This bot monitors DayZ server status and posts updates to Discord. It now uses Steam Game Server Query (via the a2s library) instead of BattleMetrics for server information.

## Features
- Fetches server status (online/offline, player count, max players, server name, game port)
- Supports multiple DayZ servers
- Discord commands for adding/removing servers

## Configuration

### servers.json
Add your DayZ servers in the following format:

```
{
  "servers": [
    {
      "id": "24745238",
      "name": "DUSK Vanilla Chernarus - 1PP | WIPED NOV 27 | NO MODS",
      "ip": "123.123.123.123",
      "port": "2302"
    },
    // ... more servers
  ]
}
```

- `id`: Unique identifier for the server (can be any string)
- `name`: Display name for the server
- `ip`: Server IP address
- `port`: Server port (usually 2302 for DayZ)

## Requirements
- Python 3.14+
- Discord bot token
- a2s library (`pip install a2s`)

## Usage
1. Configure your servers in `servers.json`.
2. Set up your Discord bot token in environment variables or `.env` file.
3. Run the bot:
   ```
   python bot.py
   ```

## License
MIT
