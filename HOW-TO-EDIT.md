# How to change what is monitored

You can change everything from the GitHub website. You don't need to write code.
Open a file in the `config` folder, click the **pencil icon** (Edit), make your
change, then click **Commit changes**. Your change is used from the next run.

## Add or remove an outlet: `config/outlets.csv`

Each line is one outlet. To add one, add a new line at the bottom, for example:

```
Kathimerini,https://www.kathimerini.gr,,el,GR,newspaper,yes,
```

The columns are:

| Column | What to put | Required? |
|---|---|---|
| name | The outlet's name | Yes |
| website | Its homepage address | Yes |
| feed | Its RSS feed address, if you know it. Leave empty and the tool looks for one itself | No |
| language | Two-letter code: en, fr, de, ru, uk, es, ar… | Helpful |
| country | Country | Helpful |
| type | e.g. newspaper, broadcaster, state media | No |
| full_text | **yes** = read every new article in full. **no** = check headlines and summaries only | No (default yes) |
| notes | Anything you like | No |

Use **no** in `full_text` for outlets that rarely cover Bellingcat (for example local
news). It keeps each run fast. GDELT and Google News still search those outlets'
full text.

To remove an outlet, delete its line.

## Change the words searched for: `config/keywords.txt`

One word or name per line. Matching ignores capital letters, and a word also matches
longer words that start with it. Put `related:` in front of a line to show those
matches as "related" (for example names of people).

## Telegram channels: `config/telegram_channels.csv`

Add the channel's short name, i.e. what comes after `t.me/`. Only public channels work.

## Switch sources on or off: `config/settings.yml`

Change `true` to `false` next to any source.

## Run it now instead of waiting

Go to the **Actions** tab, click **Check for new mentions** on the left, then click **Run workflow**.
