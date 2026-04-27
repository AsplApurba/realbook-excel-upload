# Realbook Excel Upload

Python application for RealBooks job lookup and menu management.

It includes:
- A Flask web app in `app.py`
- A Tkinter desktop app in `gui.py`
- Shared Selenium and API automation in `NEWFILE.py`

## Local run

Web app:

```bash
python3 app.py
```

Desktop app:

```bash
./run.sh
```

## Render deploy

This repo is configured for Render using Docker because the app depends on Selenium plus Chromium.

Files used for deploy:
- `Dockerfile`
- `render.yaml`
- `requirements.txt`

### Environment variables

Set these in Render:
- `REALBOOKS_USERNAME`
- `REALBOOKS_PASSWORD`

Optional:
- `REALBOOKS_ROOT`
- `EXTRA_FILE_ROOTS`

### Create the service

1. Push this repository to GitHub.
2. In Render, create a new `Web Service` from the GitHub repo.
3. Let Render use the included `render.yaml`, or choose `Docker` runtime manually.
4. Deploy.

### Notes

- The app listens on the Render-provided `PORT`.
- Container filesystem data is ephemeral on Render, so uploaded temp files and logs are not durable across restarts.
- The app uses Chromium and Chromedriver installed in the container.
