# Deployment

[Contents](index.md) · [简体中文](../zh/deployment.md)

## Choose a deployment

| Option | Use case | Files |
| --- | --- | --- |
| Application and worker | Cloud models or an existing Ollama service | `docker-compose.yml` |
| Application, worker, and Ollama | Manage the local model service together | Also use `docker-compose.ollama.yml` |
| The above with NVIDIA GPU access | Docker already supports the GPU | Also use `docker-compose.gpu.yml` |
| Source with Conda | Development and debugging | See [local development](development.md) |

Use Linux containers. The published application image targets `linux/amd64`. Compose 2.30 or newer is required. NVIDIA GPU use on Linux requires NVIDIA Container Toolkit; Docker Desktop must meet its platform-specific GPU requirements.

## Basic deployment

After downloading the repository, run from its root:

```bash
docker compose pull
docker compose up -d
docker compose ps
```

Open `http://localhost:8000`. This configuration uses a published image. Editing local source files and running this command does not rebuild those files into the image.

To change the website port in your own deployment, replace `8000:8000` with, for example, `8080:8000`. The first number is the host port used by browsers; the second remains the container API port. Apply the change with `docker compose up -d`.

## Include Ollama

Without the NVIDIA overlay:

```bash
docker compose -f docker-compose.yml -f docker-compose.ollama.yml pull
docker compose -f docker-compose.yml -f docker-compose.ollama.yml up -d
```

With NVIDIA GPU support:

```bash
docker compose -f docker-compose.yml -f docker-compose.ollama.yml -f docker-compose.gpu.yml pull
docker compose -f docker-compose.yml -f docker-compose.ollama.yml -f docker-compose.gpu.yml up -d
```

These files start the service without selecting or downloading models. Prepare the generation and embedding models separately, then refresh the model list and assign features in the administration console.

Use the same `-f` file arguments for subsequent logging, updates, and service management. See the [Ollama Docker documentation](https://docs.ollama.com/docker) for model and GPU setup.

## Connection addresses

| Model service location | Example console URL |
| --- | --- |
| Ollama in the same Compose project | `http://ollama:11434` |
| Ollama running directly on the Docker host | `http://host.docker.internal:11434` |
| Ollama on another LAN device | `http://device-address:11434` |
| Host-native Conda backend and host-native Ollama | `http://localhost:11434` |
| Compatible cloud service | The provider's full Base URL, such as its `/v1` compatibility endpoint |

Inside a container, `localhost` points to that container. A host-native Ollama service must accept connections from the application container; adjust its listening interface and firewall for your network.

The optional Ollama service binds to host address `127.0.0.1`, port `11434`, by default. If that port is occupied, change `OLLAMA_PORT` in your own `.env`. Connections within the Compose network still use `http://ollama:11434`.

## Persistent storage

| Host location | Container location | Purpose |
| --- | --- | --- |
| `data/` | `/data` | Databases, vector files, full-text and reading caches, skills, site assets |
| `.secrets/` | `/secrets` | Credential encryption key |
| Ollama named volume | `/root/.ollama` | Downloaded models |

Keep the `shualunwen` project name and these mounts when continuing an existing deployment. Use one worker for each data directory.

## Check service health

```bash
docker compose ps
docker compose logs --tail=100 app worker
```

Check `http://localhost:8000/api/health` for API health. A healthy API does not establish that models are configured, papers are available, or a particular task has succeeded.

## LAN and public access

For LAN access, use the host IP and website port and allow the corresponding firewall traffic. For public access, configure an HTTPS reverse proxy. The assistant uses SSE: allow long-lived requests and disable streaming-response buffering. PWA installation requires a secure context, with localhost usable for local development.

Continue with [quick start](quick-start.md) after the services are running. See [maintenance](maintenance.md) for upgrades and restoration.

[Previous: Quick start](quick-start.md) · [Next: Local development](development.md)
