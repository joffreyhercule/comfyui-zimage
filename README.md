# comfyui-zimage

**English** · [Français](README.fr.md)

Generate images on your own computer — no account, no subscription, and nothing sent
over the internet. You describe what you want, the image appears.

Everything installs itself: you start the installer, you wait, and it is ready. Runs on
**Windows, Linux and macOS**, and speaks **thirteen languages** — installer and studio
alike.

---

## Before you start

| You need | Details |
|---|---|
| **Python 3.10 or newer** | [python.org/downloads](https://www.python.org/downloads/) — on Windows, tick "Add python.exe to PATH" |
| **~25 GB of free space** | ComfyUI and the image models |
| **A graphics card** | NVIDIA (8 GB of video memory or more) or an Apple Silicon Mac. Without one everything still works on the processor, but an image then takes tens of minutes instead of seconds, and 32 GB of RAM are needed |
| **A decent connection** | about 15 GB to download, once |

---

## 1. Install

Get the project, then start the installer.

**Windows** — double-click `install.bat`.

**Linux / macOS** — in a terminal:

```bash
./install.sh
```

Expect **20 minutes to an hour** depending on your connection. The installer shows its
progress and tells you what it is doing.

> **Interrupted?** Just start it again. It picks up where it left off and re-downloads
> nothing that is already there.

It will ask you three questions.

**The language**, first. The installer settles on your computer's own language and
lists the thirteen it speaks: press `Enter` to keep it, or type a number to switch. To
decide in advance: `install.bat --lang de` (or `./install.sh --lang de`).

**Ollama**, then: a program that automatically translates your descriptions into
English before they reach the image generator, which understands that language far
better. It is **optional** — you can say no and add it later by running the installer
again. Without it, write your descriptions in English for better results.

**Starting the studio**, finally, once everything is in place: answering yes launches
it straight away, and you have nothing else to open.

> **Unattended?** `install.bat --yes` accepts every default and asks nothing — the
> system language is then used without a prompt, and the studio starts on its own.
> `--no-run` installs without starting anything.

---

## 2. Run

**Windows** — double-click `run.bat`.
**Linux / macOS** — `./run.sh`

Your browser opens on the studio. Leave the black window (the console) open while you
use the studio: it is what keeps the engine running. To stop everything, close it or
press `Ctrl+C`.

> **The very first image takes 20 to 30 seconds**: the engine is loading its models.
> The next ones arrive in a few seconds.

The installer offers to do this for you at the end, so the first time you have nothing
to launch by hand.

---

## 3. Create images

Describe your image in the bar at the bottom, then **Generate**.

- **Width and height** — independent, from 256 to 2048 pixels. They are rounded to the
  nearest multiple of 16 (a constraint of the generator).
- **Variants** — 1 to 4 images in a row from the same description. Each has its own
  progress bar and its own cross to cancel it if you don't like where it is going.
- **Language** (top right) — translates the interface, and states which language you
  write your descriptions in. Thirteen languages available.

The more precise your description, the better the result. Think about the subject, the
setting, the light and the style: *"a black cat asleep on a red velvet sofa, soft
morning light, photograph"* beats *"a cat"*.

### The gallery

The **☰** button at the top left opens your images, filed by day. Click a day to
unfold it, an image to enlarge it.

Once an image is enlarged, you can **download** it, **delete** it (two clicks, to avoid
accidents), or **reuse its settings** — the description and the size come back into the
bar at the bottom, ready for a variation.

Search finds your images from the words of your description, in your own language, even
when the image was generated from an English translation.

Your images are saved in the project's `media/` folder, filed by date. Nothing is sent
anywhere.

---

## If something goes wrong

**"Python 3.10+ not found"** — install Python from
[python.org](https://www.python.org/downloads/), making sure to tick "Add python.exe to
PATH", then start the installer again.

**The browser doesn't open** — go to [http://127.0.0.1:8000](http://127.0.0.1:8000)
yourself.

**"ComfyUI did not answer"** — the engine did not start. The file `logs/comfyui.log`
says why. Most often: not enough video memory, or an incomplete installation — running
the installer again repairs it.

**Images are very slow, or the studio crashes mid-generation** — your card is short on
memory. Open `config.ini` and replace the `extra_args =` line with:

```ini
extra_args = --reserve-vram 2
```

**`logs/comfyui.log` ends with "access violation" or "CUDA not available"** — the engine
was started expecting a graphics card it cannot use. The installer writes what is needed
into `config.ini` when it sees no usable card; if the `extra_args =` line is empty, fill
it in yourself, then start again:

```ini
extra_args = --cpu --disable-cuda-malloc
```

Add `--bf16-unet` to that line if the machine has less than 24 GB of RAM — otherwise the
model is loaded at full precision and asks for about 24 GB on its own.

**My descriptions are not translated** — Ollama is not installed or not running. No
harm done: write in English, or run the installer again to add it.

**An error that isn't listed here** — the `logs/` folder holds the engine's logs
(`comfyui.log`); that is where the explanation is.

---

## Going further

**Plugging in an AI assistant.** The file `mcp_server.py` lets an MCP-compatible
assistant (Claude Code, Claude Desktop) generate images for you. The configuration to
give it:

```json
{
  "mcpServers": {
    "comfyui-zimage": {
      "command": "<project path>/venv/Scripts/python.exe",
      "args": ["<project path>/mcp_server.py"]
    }
  }
}
```

On Linux and macOS, replace `venv/Scripts/python.exe` with `venv/bin/python`. Images
created this way show up in your gallery.

**Cutting an image out on a transparent background.** The project ships what it takes
to generate a character or an object and then cut it out cleanly into a transparent
PNG: see [.claude/skills/asset-detoure/](.claude/skills/asset-detoure/).

**Tuning the studio.** The `config.ini` file, written by the installer, holds the paths
and the few adjustable settings; `config.ini.example` explains every line.

**Adding a language.** The thirteen languages are declared in a single place,
`studio/i18n.py`. Adding one takes three things: a line in that file, a
`locales/<code>.json` for the installer, a `studio/static/i18n/<code>.json` for the
studio. The command below compares both catalogues against English and reports any
missing key or misspelled field:

```bash
venv/Scripts/python.exe -m studio.i18n --check
```

---

## What this project does not do

It does one thing: **create images from text**. No retouching, no video, no extra
models to manage. That is deliberate — a tool that works within the first minute,
rather than a full workshop to configure.
