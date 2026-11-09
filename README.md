# Instagram Auto-Poster

This app posts to your Instagram Business or Creator account on a schedule. It runs free on GitHub, so your computer can be off.

**How it works**

1. You list your posts in a spreadsheet: the date and time, and the photo and caption if you have them.
2. For any row that's missing something, the app fills it in:
   - **No caption?** AI writes one in your brand voice.
   - **No image?** It makes a clean text graphic from your `graphic_text`. If you only give a `topic`, it writes the graphic text too.
   - **Phone photos (PNG, HEIC, odd shapes)** are converted to the JPEG format and shape that Instagram requires.
3. Everything is prepared **24 hours ahead** in the `generated/` folder. You can review or edit it there before it goes live.
4. At the scheduled time the post is published through Instagram's official API. It supports single photos, carousels, Reels and Stories.

It checks twice an hour, so posts go out within about 30 minutes of their scheduled time.

---

## One-time setup (about 30 minutes)

### 1. Put the files on GitHub
1. Create a free account at github.com.
2. Click **New repository**. Give it a name such as `instagram-autopost` and set it to **Public**.
   - The repository must be public because Instagram fetches your images from a public web address. This also means anyone with the link can see your upcoming images and captions. If that matters for a post, keep it vague until close to posting time.
3. Click **uploading an existing file** and drag in everything from this folder.
   - **Mac tip:** the `.github` folder is hidden. Press **Cmd + Shift + .** in Finder to show it, then drag it in as well. Without it, nothing runs on a schedule.

### 2. Get your Instagram access token
1. Go to developers.facebook.com, log in, and click **Create app**.
2. Pick the use case for managing content on Instagram. It's usually called something like *"Manage messaging & content on Instagram."*
3. In the app, open **Instagram → API setup with Instagram business login**.
4. Under **Generate access tokens**, click **Add account** and log into your Instagram Business or Creator account.
5. Click **Generate token** and copy it.

Because you're the app's owner posting to your own account, you shouldn't need Meta's App Review. Leave the app in development mode.

### 3. (Optional) Get an AI key
You only need this if you want AI captions or graphics. Create a key at console.anthropic.com under **API keys**. Each caption costs a fraction of a cent.

### 4. Add your keys to GitHub
In your repository, go to **Settings → Secrets and variables → Actions → New repository secret** and add:

| Name | Value |
|---|---|
| `IG_ACCESS_TOKEN` | the Instagram token from step 2 |
| `ANTHROPIC_API_KEY` | your AI key (optional) |
| `GH_PAT` | lets the app renew your Instagram token automatically (see below) |
| `SHEET_CSV_URL` | optional: a Google Sheet link (see "Using Google Sheets") |

**Why `GH_PAT`:** Instagram tokens expire after 60 days, and the app renews yours twice a month. To get one, go to GitHub **Settings → Developer settings → Fine-grained tokens → Generate new token**. Give it access to **only this repository**, with **Secrets: Read and write**, and paste it in. Without `GH_PAT`, you'll need to paste in a new Instagram token every two months.

### 5. Personalise and test
1. Edit `config.yaml`: describe your **brand voice**, set your graphic colours, and set the footer text (your @handle).
2. Go to the **Actions** tab. If asked, click **"I understand my workflows, go ahead and enable them."**
3. Click **Post to Instagram → Run workflow** to run a test. A green tick means it worked. A red X means something failed, and clicking into the run shows a plain-English error.

GitHub emails you when a run fails, so you'll know if a post didn't go out.

---

## Scheduling posts

Edit `posts.csv`. You can do this on GitHub (click the file, then the ✏️ pencil) or in Excel, saving as CSV. Put images and videos in the `media/` folder.

| Column | What to put |
|---|---|
| `id` | A unique short name, e.g. `nov-tip-1` (letters, numbers, `-` and `_` only) |
| `post_at` | When to post, in Toronto time: `2026-11-20 09:30` |
| `type` | `image`, `carousel`, `reel` or `story` |
| `media` | File name(s) in `media/`. For a carousel, separate 2–10 files with `\|`: `a.jpg\|b.jpg`. A reel needs one `.mp4`. Leave it blank to get a text graphic |
| `caption` | Your caption. Leave it blank for AI to write one (stories have no captions) |
| `topic` | What the AI should write about when the caption or graphic is blank |
| `graphic_text` | Exact words for the text graphic, if you want to choose them |
| `alt_text` | Optional image description for accessibility |

**Examples** (the sample `posts.csv` has one of each):
- **Fully yours:** media + caption
- **Your photo, AI caption:** media + topic
- **Your words on a graphic, AI caption:** graphic_text
- **Fully automatic:** topic only

To skip a row without deleting it, put `#` at the start of its id.

### Reviewing before it posts
Starting 24 hours before posting time, each post appears in `generated/<id>/`, with the final image(s) and a `post.json` containing the caption. You can:
- **Edit the caption:** change the `"caption"` text in `post.json`.
- **Start over:** delete that post's folder and it'll be made again on the next run.
- **Cancel it:** delete the row from `posts.csv`, or move its date.

`posted.json` keeps a record of everything posted, with links, plus any errors.

### Using Google Sheets instead of posts.csv
1. Make a Google Sheet with the same column headings.
2. Go to **File → Share → Publish to web**, choose that sheet, choose **CSV**, and click **Publish**.
3. Put the link it gives you in the `SHEET_CSV_URL` secret.

Media files still go in the `media/` folder. You can also paste a full image web address into the media column.

---

## Good to know
- **Limits:** Instagram allows about 100 posts per day through the API, and each post can have up to 5 hashtags. The app trims extra AI hashtags.
- **Shapes:** Feed photos are fitted between portrait (4:5) and wide (1.91:1), and carousel slides are all made 4:5. Instead of cropping, the app adds borders in your background colour. Stories are 9:16.
- **Videos** must already be `.mp4` or `.mov` that Instagram accepts. The app doesn't edit video.
- **Missed posts:** if GitHub has an outage and a post is more than 24 hours late, it's skipped rather than posted late. To reschedule it, change its `post_at`.
- **Checking from your own computer (optional):** install Python and run `pip install -r requirements.txt`. Then `python autopost.py check` tests your settings, and `python autopost.py preview` makes every graphic and caption so you can look at them.

## Troubleshooting
| Message | Fix |
|---|---|
| `Invalid OAuth access token` / code 190 | The token expired. Generate a new one (setup step 2) and update `IG_ACCESS_TOKEN`. Then add `GH_PAT` so it renews automatically. |
| `media/... not found` | The file name in `posts.csv` must match the file in `media/` exactly, including capital letters. |
| `Instagram couldn't process the media` | Usually an unsupported video. Re-export it as an H.264 `.mp4`. |
| Nothing happens at all | Check the Actions tab is enabled and that the `.github` folder was uploaded. |
| `...will go out on the next run` | Normal. A post was added at the last minute, and it goes out about 30 minutes later. |
