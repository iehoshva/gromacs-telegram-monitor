# GROMACS Telegram Monitor

A small Linux daemon that watches one local GROMACS `mdrun` at a time and exposes status plus tightly-scoped control through a private Telegram bot. Runtime dependencies are Python 3 standard library, Linux `/proc`, `pgrep`, `kill`, `systemd --user`, GROMACS, and outbound HTTPS to Telegram. It does not require ChatGPT, Codex, a browser, a public IP, or an inbound port.

## What it monitors

Supported stages are fixed:

- NVT: `02_nvt/nvt.log`, checkpoint `02_nvt/nvt.cpt`, TPR `02_nvt/nvt.tpr`
- NPT: `03_npt/npt.log`, checkpoint `03_npt/npt.cpt`, TPR `03_npt/npt.tpr`
- PROD: `04_prod/prod.log`, checkpoint `04_prod/prod.cpt`, TPR `04_prod/prod.tpr`

A run appears in `/runs` only when at least one exact supported log file exists. Folder presence alone is not enough.

State rules are deterministic. With a live supported `gmx mdrun`, a log age of **<= 1800 seconds** is `RUNNING`; **> 1800 seconds** is `STALLED`. A stale log without a live process is never `STALLED`: it becomes `STOPPED` unless the log tail contains `Finished mdrun`, in which case it is `COMPLETED`. More than one controllable `mdrun` becomes `MULTIPLE_PROCESS` and destructive/control commands are blocked.

## Telegram setup

1. In Telegram, open **BotFather**, create a bot, and copy its bot token.
2. Obtain your own numeric Telegram user ID. Send the new bot at least one private message so Telegram permits the conversation.
3. Copy/install this project on the campus Linux PC.
4. Run `./install.sh` once. If credentials are still empty, the service is installed and enabled but deliberately not started.
5. Edit `~/.config/gromacs-telegram/config.env` and fill `TELEGRAM_BOT_TOKEN` and `TELEGRAM_USER_ID`.
6. Start it with `systemctl --user restart gromacs-telegram.service`.
7. Check service state with `systemctl --user status gromacs-telegram.service`.
8. Follow logs with `journalctl --user -u gromacs-telegram.service -f`.

The installed config is chmod `0600`. Keep the token private.

## Commands

- `/status` — current state, stage, PID/command when alive, log age, checkpoint availability, and MD progress.
- `/runs` — numbered valid run directories under the configured base directory.
- `/use <n>` — select a run from the most recent `/runs` mapping; no free-form paths are accepted.
- `/where` — show the selected run's resolved full path.
- `/continue` — continue the remembered/highest valid stage from its `.cpt` using `gmx mdrun -deffnm <prefix> -cpi <prefix>.cpt -append`.
- `/stop` — show PID/command/run/stage and open a five-minute confirmation window. It does **not** kill anything.
- `/confirmstop` — revalidate PID, argv, cwd, run root, and stage, then issue `kill -KILL` only on an exact match.
- `/help` — show the command list.

Only the configured Telegram user ID gets operational information or control.

## Progress semantics

Current progress is the latest `Step / Time` value found in the stage log, converted from ps to ns. It may trail the actual simulation slightly between log writes.

Target duration is read from the active **TPR** using `gmx dump -s`, not from `.mdp`. The monitor parses the effective `init-step`, `nsteps`, start time, and timestep. If the TPR is missing, malformed, unlimited (`nsteps < 0`), or cannot be dumped, the bot reports current ns with `target unavailable` instead of guessing.

## Stop and continue safety

`/continue` is refused whenever any controllable `gmx mdrun` is already active. It never runs `grompp`, never modifies topology/MDP files, and never adds `-ntmpi`, `-ntomp`, or `-pin`.

`/stop` is intentionally non-destructive. `/confirmstop` re-reads `/proc/<PID>` before the kill. PID reuse, changed argv, changed cwd, changed run root, changed stage, expiry, or `MULTIPLE_PROCESS` all cancel the operation. After a successful confirmed kill, the next monitor transition is labeled `STOPPED BY USER`.

## Internet outage behavior

Local GROMACS monitoring continues if Telegram or the internet is unavailable. Important undelivered state transitions are collapsed to the latest pending state. When Telegram connectivity returns, the daemon sends one recovery summary rather than replaying every 60-second check. Bot/network errors never signal or terminate GROMACS.

## Service persistence

The service uses `systemd --user` with `Restart=always`, `RestartSec=5`, and `KillMode=process`. The last setting is intentional: restarting/stopping the monitor service targets the bot process rather than an `mdrun` child launched by `/continue`, so the bot and simulation remain operationally independent. If the account does not have lingering enabled and you want the user service to survive logout/start at boot, the installer prints the optional command:

```bash
loginctl enable-linger "$USER"
```

Run it only if that behavior is desired and permitted on the campus machine.

## Uninstall

This removes only the monitor. It does not touch any GROMACS run directory, checkpoint, trajectory, topology, or simulation process.

```bash
systemctl --user disable --now gromacs-telegram.service || true
rm -f ~/.config/systemd/user/gromacs-telegram.service
systemctl --user daemon-reload
rm -rf ~/.local/share/gromacs-telegram
rm -rf ~/.local/state/gromacs-telegram
# Optional: remove the monitor config/token too
rm -rf ~/.config/gromacs-telegram
```
