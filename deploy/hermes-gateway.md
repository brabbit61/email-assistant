# Hermes Telegram gateway setup (T2.1, issue #41)

Binds hermes-agent's messaging gateway to the existing Telegram bot (#4) and
locks it to Jenit's chat id only. Identical on every machine — the only thing
that differs across platforms is which service manager `hermes gateway
install` uses underneath (systemd user service on Linux; a Windows service
on the Phase 5 box).

## Config

Two files, both outside this repo (hermes owns its own config/secrets under
`~/.hermes/`, separate from this repo's `secrets/.env`).

**`~/.hermes/.env`** — add:
```
TELEGRAM_BOT_TOKEN=<same bot token as this repo's secrets/.env TELEGRAM_TOKEN>
TELEGRAM_ALLOWED_USERS=<Jenit's numeric chat id, e.g. secrets/.env TELEGRAM_CHAT_ID>
```
`TELEGRAM_ALLOWED_USERS` is enforced at message intake, fail-closed: if set,
only listed ids get through — everyone else is denied before the message
reaches the agent. Leave `GATEWAY_ALLOW_ALL_USERS` unset.

Note: the token is duplicated under two different env var names across two
separate `.env` files (this repo's worker uses `TELEGRAM_TOKEN`; hermes uses
`TELEGRAM_BOT_TOKEN`) because they're two independent processes with their
own config. Not unified on purpose — `hermes gateway setup` owns its own
`.env` and fighting that with a symlink isn't worth it.

**`~/.hermes/config.yaml`** — add:
```yaml
gateway:
  platforms:
    telegram:
      unauthorized_dm_behavior: ignore
```
Without this, hermes's default behavior for an unrecognized sender is to
send them a pairing-request prompt. `ignore` makes it silent instead — a
non-allowlisted sender gets no reply and no acknowledgment the bot exists.

Telegram has no `dm_policy` setting (that's a WhatsApp/WeCom/Weixin concept);
for Telegram the allowlist env var above **is** the access-control mechanism.

## Install as a service

Reuse hermes's own installer — don't hand-roll a systemd unit:
```sh
hermes gateway install   # writes + enables the user service, starts it
hermes gateway status    # confirm active
```
On Linux this creates a systemd **user** service (survives logout via
linger, which the installer enables). Manage it with `hermes gateway
start|stop|restart|status`, logs via `journalctl --user -u hermes-gateway -f`.

## One shared bot — coexistence rule

The worker (#42) sends P1 pings with the same bot token via `sendMessage`.
That's safe to share because Telegram only limits `getUpdates` (long-poll)
to one consumer per token; `sendMessage` is stateless and has no such limit.
**Constraint: the worker must never call `getUpdates` or register a
webhook** — it only sends. The gateway is the sole long-poll consumer.

## Verification

**1. Round-trip (transport proof).** From Jenit's Telegram, message the
bot ("ping"). Confirm a reply arrives. This is a vanilla-hermes reply, not
an email-assistant answer — the skill teaching it the domain is #44, not
this ticket.

**2. Lockdown — inversion test (closes the ticket's sign-off box).**
1. Stop the gateway: `hermes gateway stop`
2. Temporarily set `TELEGRAM_ALLOWED_USERS` in `~/.hermes/.env` to a wrong
   id (anything that isn't Jenit's real chat id)
3. `hermes gateway start`
4. Message the bot from Jenit's real Telegram account — confirm **no
   reply** (silence, per `unauthorized_dm_behavior: ignore`)
5. Restore the real chat id in `TELEGRAM_ALLOWED_USERS`, `hermes gateway
   restart`
6. Message the bot again — confirm the reply comes back

Passing both closes: "A message from Jenit's chat gets an agent reply" and
"A message from any other chat id is ignored" (AC), and the sign-off box
(lockdown verified — done via the one-account inversion test rather than a
second Telegram account).

## Redo on the Phase 5 Windows box

Same two config blocks, same `hermes gateway install`/`start`/`status`
commands — `gateway install` picks the right service manager for the OS
automatically. Nothing in this doc is Linux-specific except the systemd
log command.
