# pinterest downloader — rajnish sahani

send a pinterest pin or pin.it link to your telegram bot. it downloads the best available image or video and sends the downloaded file as a document without re-encoding it.

created by rajnish sahani.

## deploy on render

1. create a **separate** github repository named `rajnish-pinterest` in your `dashaneka` account. keep your existing instagram repository as it is.
2. extract this archive and upload the contents of `pinterest_telegram_bot` to the new repository root. upload the files, not the zip or its outer folder.
3. the required root files are `bot.py`, `pinterest_downloader.py`, `requirements.txt`, `Dockerfile`, and `render.yaml`. also upload `README.md` and the `tests` folder.
4. after the repository contains these files, open:

   https://render.com/deploy?repo=https%3A%2F%2Fgithub.com%2Fdashaneka%2Frajnish-pinterest

5. if the repository is private, grant Render's GitHub app access to it. alternatively open https://dashboard.render.com/ and choose **new → blueprint**, then select your repository.
6. render asks for `BOT_TOKEN`. paste the token for your **pinterest bot** from botfather in that secret field. do not commit a token or `.env` file to github. do not reuse the instagram bot's token.
7. deploy the blueprint and wait for **live**. send `/start`, then a pin link in telegram.

render supplies the public service URL automatically. the bot uses a secret-protected webhook and listens on render's port. no manual webhook setup is required.

this project is configured for render's free web-service plan. free services sleep after inactivity, so the first request can be delayed. render's workspace free hours are shared with other free services, including your instagram bot; two services running constantly exceed the monthly free allowance. download traffic is also subject to render's limits.

## personal access

send `/id` to the pinterest bot. in render's environment settings, set `ALLOWED_USER_IDS` to the number it returns, then save and redeploy. comma-separated IDs allow multiple users. a blank value allows anyone who finds the bot to use it.

if editing this setting on a blueprint-managed service, update it in the blueprint file too before the next blueprint sync.

## what is included

- pinterest and pin.it link support; non-pinterest links rejected
- best video and audio streams merged by ffmpeg without re-encoding
- image extraction restricted to the requested pin and its page metadata
- matching original-image variants, including PNG versions of JPEG previews
- document uploads with a default 49 mb maximum
- temporary media deleted after sending, failure, timeout, or cancellation
- yt-dlp disk cache disabled; incidental cache/config lives in the temporary request folder
- 420-second overall deadline and 256 mb temporary-storage limit
- one download at a time
- secret-protected webhook on render; polling for local use
- `/start`, `/help`, and `/id`

telegram may display an inline video preview even when the bot uploads an mp4 as a document. this does not mean the bot re-encoded it. highest available quality means the media exposed by pinterest, not a guarantee of the creator's camera-original file. one media file is returned per pin; multi-page idea pins are not fully supported.

## local run

python 3.11+ and ffmpeg are required.

```sh
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

put your pinterest bot token in `BOT_TOKEN` in `.env`, then run:

```sh
python bot.py
```

leave `WEBHOOK_URL` unset for local polling. run only one instance with the same bot token.

## settings

| variable | default | purpose |
| --- | --- | --- |
| `BOT_TOKEN` | required | pinterest telegram bot token; `TELEGRAM_BOT_TOKEN` also accepted |
| `ALLOWED_USER_IDS` | blank | optional personal-use allowlist |
| `MAX_UPLOAD_MB` | 49 | telegram upload cap, maximum 49 |
| `MAX_TEMP_MB` | 256 | maximum temporary bytes across download and merging |
| `DOWNLOAD_TIMEOUT_SECONDS` | 420 | overall download deadline |
| `WEBHOOK_URL` | render external URL | HTTPS service origin; omit locally |
| `WEBHOOK_SECRET` | derived from token | secret checked on webhook requests |
| `PORT` | 10000 | web-service port |

## validation

21 automated tests cover parsing, URL validation, redirect handling, original-image matching, document uploads, cleanup, timeouts, cancellation, storage limits, webhook/polling setup, token redaction, and the personal allowlist.

```sh
pip install pytest
python -m pytest -q
```

live checks passed for the provided image and video pins without login cookies. the image was 878 × 1250 PNG; the video was 720 × 1280 MP4 with audio, about 56 seconds. render deployment and delivery through your own telegram bot must be verified after you deploy. docker was inspected but could not be built in the review environment.

pinterest can change its site or block individual servers. pinned dependencies make deployments repeatable; update yt-dlp when needed and re-test your links.
