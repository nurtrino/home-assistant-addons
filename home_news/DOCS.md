# Home News

Curated news for Chase's room. Every 30 minutes (6 AM to midnight; every 2 hours overnight) the add-on:

1. **Collects** about 60 sources across five sections: War & Defense, Politics, Gaming (including Steam news
   for your games), AI, and Economy. Sources live in `/addon_configs/<this add-on>/sources.yaml`
   (created on first start). Edit that file to add or remove feeds, then restart the add-on.
2. **Scores** every new story with **Claude Haiku** (`sorter_model`): it decides the section, rates how much it
   matters to you (0 to 100), writes a clean headline, groups duplicates, and drops junk (deals, sports, listicles).
3. **Picks the top three** with **Claude Sonnet** (`editor_model`) when your room mode switches to
   `editor_trigger_state` (Idle, meaning you're on your way home), at 7 AM, after a big data release, and every
   4 hours. The picks include a sentence written for the TV's welcome voice.
4. **Economy:** reads the Forex Factory calendar (forecast and previous) and, for CPI, payrolls and unemployment,
   fetches the actual number from the BLS public API at release time. It sends a notification to
   `notify.<notify_target>`.

It publishes:

| Entity | Contents |
|---|---|
| `sensor.home_news` | `top3` (headline, spoken line, section, source, link), `calendar`, `releases`, `updated` |
| `sensor.home_news_ai`, `_war`, `_politics`, `_gaming`, `_economy` | `items`: up to 15 stories each (headline, source, link, score, time, outlet count) |

## Options

| Option | Default | Notes |
|---|---|---|
| `anthropic_api_key` | | From console.anthropic.com. Without it, ranking falls back to source weight and recency. |
| `sorter_model` | `claude-haiku-4-5` | Scores and sorts every story (high volume, so use a cheap model). |
| `editor_model` | `claude-sonnet-5-5` | Picks the top three. |
| `refresh_minutes` | 30 | Collection interval during active hours. |
| `active_start` / `active_end` | 06:00 / 23:59 | Outside these hours: every 2 hours, and no scheduled top-three picks. |
| `steam_app_ids` | your games | Steam app IDs whose news counts as Gaming (big announcements only). |
| `notify_target` | `mobile_app_chases_iphone` | Where economic data releases are pushed. |
| `mode_entity` / `editor_trigger_state` | `input_select.chase_room_mode` / `Idle` | Refreshes the top three when you head home. |

Expected cost at defaults: about $8 a month for Haiku plus about $5 for Sonnet.
