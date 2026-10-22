# Newsdesk: autopilot news posts for @riyaagadhwal

Newsdesk watches marketing, brand, creator and internet-culture news every minute. When a story is genuinely new, verified and on-niche, it writes a tweet-style post in your voice: the news, then a sarcastic punchline. It renders the post as a single image with a "Follow @riyaagadhwal for more" footer and publishes it to Instagram. Everything runs on free tools.

```
RSS feeds + Google News ──► seen within 5 min of publishing? ──► same story as something else? (cluster)
      every minute               no → ignored as old                  already posted this week? → skip
                                                                              │
     sensitive (tragedy, politics, crime)? → blocked ◄────────────────────────┤
     off-niche (no marketing/brand/creator keywords)? → skipped                ▼
                                                     verified? official source OR 2+ trusted outlets
                                                     (waits up to 60 min for a 2nd outlet, else drops it)
                                                                              │
         daily cap (3) / 90-min gap reached? → wait or skip (no AI calls spent) ◄
                                                                              ▼
   Gemini writes: news line + punchline + caption + hashtags + first comment + evidence for every fact
                                                                              ▼
   score ≥ 80/100? ─► code checks (numbers, quotes, evidence, copied headline) ─► AI fact-checker ─► render card
                                                                              ▼
              dry_run: preview to your Telegram        live: publish to Instagram + first comment + alert
```

## What is realistic (read this first)

| You asked for | What actually happens |
|---|---|
| News from the last 5 minutes | A story only counts as **new** if Newsdesk sees it within 5 minutes of its publish time. Feeds are checked every minute. Outlets sometimes publish late, so a few stories will be missed rather than posted late. |
| Verified before posting | Most stories first appear in a single outlet. Newsdesk waits up to 60 minutes for a second trusted outlet or an official newsroom. So a post usually goes out **10–45 minutes** after the news breaks. Accuracy wins over speed. |
| Fully automatic | Yes. After the dry-run week, one command makes it publish without you. |
| Trending audio | Not needed: these are image posts. |
| Clickable source links | Instagram captions don't make links clickable. The caption names the outlets. Full links go to your Telegram and are saved with every post. |
| Pinned comment | The first comment is posted automatically. Instagram's API can't pin it, so pin it yourself if you want. |
| ₹0 per month | Yes, but there are trade-offs. **Gemini free tier:** Google may use your prompts to improve its models, and humans may review them. That's fine for public news; never put private info in prompts. Free limits can change without notice. **Oracle Always Free:** it may refuse to create a server when a region is full; try again later or another region. Oracle can also reclaim free servers it considers idle; upgrading the account to Pay-As-You-Go avoids that and still costs ₹0 inside the free limits. |

## Monthly cost

| Piece | Tool | Cost |
|---|---|---|
| Server (runs every minute) | Oracle Cloud Always Free VM (Ubuntu) | ₹0 |
| Public image address (HTTPS) | DuckDNS subdomain + Caddy | ₹0 |
| Writing + fact-checking | Gemini API free tier, capped at 300 calls/day | ₹0 |
| Previews, alerts, pause button | Telegram bot | ₹0 |
| Publishing + insights | Instagram API with Instagram Login | ₹0 |

Upgrade path: set `llm.provider: anthropic` and add `ANTHROPIC_API_KEY` to use Claude. Each story costs two calls, and only verified, on-niche stories reach the AI. Expect roughly a few hundred rupees a month at this volume; check current pricing first.

---

## Setup, step by step (about 1–2 hours, once)

### 1. Instagram access token (20 min)
Your account is already a Creator account, which this API supports. No Facebook Page is needed.

1. Go to **developers.facebook.com**, log in, and choose **Create app**. Pick the use case for managing content on Instagram (Meta renames these screens often; look for "Instagram").
2. In the app, open **Instagram → API setup with Instagram login**.
3. Under **Generate access tokens**, click **Add account** and log in as **@riyaagadhwal**.
   - If it says the account needs a role, go to **App roles → Roles → Instagram testers** and add riyaagadhwal.
   - Then accept the invite in the Instagram app: **Settings → Website permissions → Apps and websites → Tester invites**.
4. Make sure these permissions are on:
   - `instagram_business_basic`
   - `instagram_business_content_publish`
   - `instagram_business_manage_comments` (for the first comment)
   - `instagram_business_manage_insights` (for analytics)
5. Click **Generate token** and copy it into `IG_ACCESS_TOKEN`.
6. Leave `IG_USER_ID` empty for now. `newsdesk check` prints it, and then you paste it in.

The app can stay in Development mode, because it only posts to your own account. Newsdesk refreshes the 60-day token every week. If the token ever dies, publishing pauses itself and Telegram tells you.

### 2. Gemini key (2 min)
Go to **aistudio.google.com → Get API key** and put the key in `GEMINI_API_KEY`.

### 3. Telegram bot (5 min)
1. In Telegram, message **@BotFather**, send `/newbot`, and copy the token into `TELEGRAM_BOT_TOKEN`.
2. Open your new bot and send it "hi".
3. After the install in step 5, run `.venv/bin/python -m newsdesk telegram-id` and paste the number into `TELEGRAM_CHAT_ID`.

### 4. Free server + web address (30 min)
1. Create an **Oracle Cloud** account. Then go to **Compute → Instances → Create instance**:
   - Image: **Ubuntu 24.04**
   - Shape: **VM.Standard.A1.Flex (Ampere)**, 1 OCPU / 6 GB is plenty
   - Add your SSH key
   - Copy the public IP once it's running.
2. In the instance's **VCN → Security list**, add ingress rules for TCP ports **80** and **443** from `0.0.0.0/0`.
3. At **duckdns.org**, sign in and create a subdomain (e.g. `riyanews`). Point it at the server's public IP.

### 5. Install (10 min)
Copy this folder to the server (e.g. with `scp -r newsdesk ubuntu@IP:~`), then:
```bash
ssh ubuntu@YOUR_IP
cd newsdesk
bash deploy/install.sh riyanews.duckdns.org
nano .env          # paste your keys; PUBLIC_BASE_URL=https://riyanews.duckdns.org/p
.venv/bin/python -m newsdesk check
```
`check` tests everything: the AI, the Instagram token, the public image URL and Telegram. It prints ✓ or ✗ for each with the reason. Run `.venv/bin/python -m newsdesk feeds` to see which news feeds respond and how recent they are. Remove any feed that's dead in `config.yaml`.

### 6. Your look
- Put a square photo of yourself at `assets/avatar.jpg`. Until then, a pink circle with "R" is used.
- In `config.yaml → brand`, you can switch `theme: dark` and change the `accent` colour of the footer.
- Run `.venv/bin/python -m newsdesk demo` to render sample cards into `data/demo/` and check the look.

---

## Testing (one week of dry run — do not skip)

The timer starts in **dry_run** mode. Every post that would have gone out arrives in Telegram instead, with:
- the image and the caption
- the score and why it counts as verified
- the source links and the first comment

Nothing touches Instagram.

During the week:
- Read every preview. If the jokes feel off, edit the voice section in `newsdesk/prompts.py`.
- If good stories are being skipped, check why with:
  `sqlite3 data/newsdesk.db "select status, reason, title from clusters order by id desc limit 30"`
  - `expired_unverified` means only one trusted outlet covered it.
  - `rejected` means it was off-niche or scored low.
  - `rejected_factcheck` comes with the exact problem.
- Tune `keywords`, `trusted_domains` and `ranking.threshold` in `config.yaml`.

The automated tests cover every failure case you listed: fresh vs old, duplicates, unverified stories, invented numbers and quotes, AI outage, Instagram errors, expired token, and network failure mid-publish (no double post).
```bash
.venv/bin/python -m pytest -q        # 36 tests
```

## Going live

From Telegram, send **/live**. Or on the server, run `.venv/bin/python -m newsdesk live`.

## Emergency stop (any one works instantly)

| Where | How |
|---|---|
| Telegram | `/pause`, and later `/resume` |
| Server | `.venv/bin/python -m newsdesk pause`, or `touch data/PAUSE` (delete the file to undo) |
| Back to previews only | `/dryrun` |
| Kill it entirely | `sudo systemctl stop newsdesk.timer` |

Telegram also gives you `/status`. Every error and every published post sends you a message. Logs are in `data/logs/newsdesk.log`, and tokens are scrubbed from them automatically.

## Improving over time

Every 6 hours Newsdesk pulls reach, likes, comments, shares, saves and views for each post. Every Monday at 9:00 IST, Telegram gets a report ranking posts by **(shares + saves) ÷ reach**, and by category. You can also run `.venv/bin/python -m newsdesk report` any time.

- Shares mean the joke landed: people send it to friends. Saves mean the insight landed.
  - Categories with a strong share rate → lower their bar.
  - Categories that never travel → remove their keywords.
- Comments come from the caption question. If comments are low, ask sharper either/or questions.
- Reach without shares usually means the topic is right but the punchline is weak. Raise the bar by requiring a higher `wit` score.
- Test one change at a time for a week (theme, posting cap, threshold). Instagram changes its ranking often, so trust your own numbers over any "algorithm hack".

## Files

| File | Purpose |
|---|---|
| `config.yaml` | Every setting: brand, caps, keywords, feeds, trusted outlets |
| `.env` | Secrets only (chmod 600) |
| `newsdesk/pipeline.py` | The full flow |
| `newsdesk/prompts.py` | Your voice + fact-checker instructions |
| `newsdesk/render.py` | The post card design |
| `newsdesk/factcheck.py` | Hard rules that run before the AI check |
| `data/posts/*/` | Every package ever made: `card.jpg` + `package.json` (sources, claims, evidence, score) |

Fonts: Inter, SIL Open Font License.
