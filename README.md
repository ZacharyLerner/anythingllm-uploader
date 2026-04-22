# AnythingLLM Uploader — Server Setup

Setup for a fresh Linux server (Amazon Linux 2023, aarch64).

## 1. System Packages

```bash
sudo dnf install -y python3.11 mesa-libGL libxcb libX11
```

`mesa-libGL`, `libxcb`, and `libX11` are required by OpenCV — without them OCR fails with errors like `libGL.so.1: cannot open shared object file` or `libxcb.so.1: cannot open shared object file`.

## 2. Clone and Create Venv

```bash
git clone <your-repo-url> anythingllm-uploader
cd anythingllm-uploader

python3.11 -m venv venv
source venv/bin/activate
pip install --upgrade pip
```

## 3. Install Dependencies

Point pip's temp dir at home (Amazon Linux's `/tmp` is too small for the wheels):

```bash
mkdir -p ~/tmp
export TMPDIR=$HOME/tmp
```

`TMPDIR` only persists for the current shell. If you'll be reinstalling later, add `export TMPDIR=$HOME/tmp` to `~/.bashrc`.

Then install:

```bash
pip install -r requirements.txt
```

## 4. Configure `.env`

Create a `.env` file in the project root:

```
ANYTHINGLLM_BASE_URL=http://your-anythingllm-host:3001
ANYTHINGLLM_API_KEY=your-api-key-here
```

## 5. Run

Development (auto-reload on file changes):

```bash
fastapi dev main.py --host 0.0.0.0 --port 3000
```

Production (no auto-reload):

```bash
fastapi run main.py --host 0.0.0.0 --port 3000
```

Server at `http://<host>:3000`, docs at `/docs`.

**First upload** downloads ~40 MB of OCR models — one-time, happens automatically.