# Local development

[Contents](index.md) · [简体中文](../zh/development.md)

## Prepare the environment

Install Git, Conda, and Node.js 22. The repository's `environment.yml` selects Python 3.12 and installs backend and test dependencies.

From the repository root:

```bash
conda env create --prefix ./.conda-env -f environment.yml
conda activate ./.conda-env
```

On first setup, copy the environment example. PowerShell:

```powershell
Copy-Item .env.example .env
```

Bash:

```bash
cp .env.example .env
```

Keep an existing `.env` rather than replacing its credentials with the example. Configure model connections and assignments in the console. A host-native Ollama service normally uses `http://localhost:11434`.

## Start the backend

From the repository root in the activated Conda environment:

```bash
python -m uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8000 --reload
```

The default `PIPELINE_MODE=process` makes the API start a separate worker child process. Do not also launch a second worker manually.

## Start the frontend

In another terminal:

```bash
cd frontend
npm ci
npm run dev
```

Open the address printed by Vite, normally `http://127.0.0.1:5173`. The development proxy in `frontend/vite.config.ts` forwards `/api` to `127.0.0.1:8000`. If you change the backend port, update your local proxy configuration accordingly.

## Worker modes

| `PIPELINE_MODE` | Behavior |
| --- | --- |
| `process` | Default host-native mode; the API starts a separate worker |
| `external` | Deployment starts the worker separately; used by the Docker configuration |
| `inline` | Runs background logic in the API process for tests |

If you need manual worker management, first configure the API to use `external`. Start it from the same root and Conda environment:

```bash
python -m uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8000
```

Then use another PowerShell terminal:

```powershell
$env:PYTHONPATH = (Resolve-Path backend).Path
python -m app.worker
```

Both processes must share the data directory, encryption key, and runtime configuration.

## Checks and builds

Run backend tests from the repository root:

```bash
python -m pytest backend/tests
```

Run frontend checks from `frontend/`:

```bash
npm run check:math
npm run check:chat
npm run check:dwell
npm run check:auth
npm run build
```

To build an image containing your local source:

```bash
docker build -t papers-go:local .
```

In your deployment configuration, point both `app` and `worker` to that local image and recreate the services. The repository's default Compose configuration uses the Docker Hub release image.

[Previous: Deployment](deployment.md) · [Next: Configuration](configuration.md)
