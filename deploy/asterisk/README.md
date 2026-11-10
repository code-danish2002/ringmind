# Asterisk for free softphone calls

Phone a Zoiper or Linphone app on the same Wi-Fi as your Mac, and Asterisk (in Docker on the Mac)
passes the call to the bot over AudioSocket:

```
phone app --SIP/RTP over Wi-Fi--> Mac LAN_IP:5060 / :10000-10099 --Docker--> asterisk
asterisk --HTTP GET /asterisk/register (uuid, caller)--> app:8000
asterisk --AudioSocket TCP (8 kHz slin16)--> app:9092
```

The image is Ubuntu 24.04's `asterisk` package (Asterisk 20.6). It works on arm64 (Apple Silicon) and amd64.

## Extensions

| Dial | What happens |
|------|--------------|
| `100` | Talk to Maya (RingMind). Asterisk answers, makes a UUID, registers `uuid -> caller number` with the bot, then runs `AudioSocket()`. |
| `200` | Echo test: you hear yourself. This checks SIP and RTP between the phone and Asterisk without the bot. |

## Accounts

| User | Caller ID sent to the bot | Meaning |
|------|---------------------------|---------|
| `1001` | `"Priya Sharma" <+15551234567>` | Known demo customer |
| `1002` | `<+15550001111>` | Unknown caller |

Both accounts use the password in `SIP_PASSWORD`.

## 1. Find the Mac's LAN IP

```sh
ipconfig getifaddr en0     # Wi-Fi on most Macs
```

If that prints nothing, try `en1`, or open System Settings → Wi-Fi → Details → TCP/IP. The address
changes if you switch networks or the router hands out a new lease. When it changes, update
`LAN_IP` and recreate the container.

## 2. Environment variables

| Var | Required | Default | Notes |
|-----|----------|---------|-------|
| `LAN_IP` | yes | | The Mac's Wi-Fi IP. Asterisk puts it in SIP and SDP so the phone sends audio to the Mac, which Docker forwards to the container. |
| `SIP_PASSWORD` | yes | | At least 8 characters, with no `;`, `"`, `\` or whitespace. The container refuses to start otherwise, because port 5060 is open on your LAN. |
| `SIP_1001_CALLERID_NUM` | no | `+15551234567` | |
| `SIP_1002_CALLERID_NUM` | no | `+15550001111` | |
| `BOT_HOST` | no | `app` | Compose service name of the bot. |
| `BOT_HTTP_PORT` | no | `8000` | |
| `BOT_AUDIOSOCKET_PORT` | no | `9092` | |
| `LOCAL_NET` | no | `172.16.0.0/12` | Comma-separated CIDRs that talk to Asterisk *without* going through the Mac's IP. Don't add `192.168.0.0/16` (see "No audio from the phone" below). |

Put `LAN_IP` and `SIP_PASSWORD` in `.env`, then:

```sh
docker compose up -d --build asterisk
docker compose logs -f asterisk          # should end with "Asterisk Ready."
docker compose exec asterisk asterisk -rx "pjsip show endpoints"
```

Docker publishes UDP `5060` and `10000-10099`, which must match `rtp.conf`. Each call uses
two ports, so this range allows about 50 calls at once.

## 3. Zoiper (iOS / Android)

1. Settings → Accounts → **+** → "Yes, I have an account" → **Manual configuration** → **SIP account**.
2. Account name: `RingMind 1001`.
3. Domain / Host: `<LAN_IP>:5060` (for example `192.168.1.23:5060`).
4. Username: `1001`. Password: your `SIP_PASSWORD`.
5. Optional settings: leave Authentication username and Outbound proxy empty.
6. Network settings → Transport: **UDP**. STUN: **off** (it's a LAN, so you don't need STUN).
7. Audio codecs: put **G.711 μ-law (PCMU)** and **G.711 A-law (PCMA)** at the top.
8. Save. The account should show a green tick (Registered).
9. Dial **200** first. You should hear yourself. Then dial **100** to talk to the bot.

To test as an unknown caller, add a second account with user `1002`.

## 3b. Linphone

1. Open Linphone, go to Assistant → **Use a SIP account** (sometimes labelled "Account login" → "Third-party SIP account").
2. Username: `1001`. Password: your `SIP_PASSWORD`.
3. Domain: `<LAN_IP>:5060`. Transport: **UDP**. Leave Display name empty, because Asterisk sets caller ID.
4. In Settings → Audio, make sure PCMU and PCMA are enabled.
5. In Settings → Network, turn **ICE** and **STUN** off if you see them.
6. Call `200`, then `100`.

## Troubleshooting

- **The phone won't register (timeout).**
  - Check that the phone and the Mac are on the **same Wi-Fi network**. Guest networks often block traffic between devices ("client isolation").
  - Turn **mobile data off** on the phone so the app can't use the cellular network.
  - Check that `LAN_IP` is the Mac's address *right now*.
  - On the Mac, if a **firewall prompt** appears for Docker ("accept incoming network connections"), click **Allow**. If the firewall is on, check System Settings → Network → Firewall → Options and make sure Docker / `com.docker.backend` is allowed.
  - To see if packets arrive at all, run `docker compose exec asterisk asterisk -rx "pjsip set logger on"`, then `docker compose logs -f asterisk`.
- **401 / "wrong password".** The username is `1001` or `1002`. The password is `SIP_PASSWORD` from the running container. Recreate the container after you change it.
- **One-way audio or no audio.** This is almost always `LAN_IP` or the RTP ports:
  - `LAN_IP` must be the Mac's current Wi-Fi IP.
  - UDP `10000-10099` must be published 1:1 (`10000-10099:10000-10099/udp`), and nothing else on the Mac may use those ports.
  - Turn off STUN and ICE in the softphone.
  - Run `docker compose exec asterisk asterisk -rx "pjsip set logger on"` and look at the `c=IN IP4 ...` line in Asterisk's 200 OK. It must be `LAN_IP`. If it is a `172.x` container address, `LOCAL_NET` covers the source address Docker Desktop shows for the phone. That address is `192.168.65.x`, so never put `192.168.0.0/16` in `LOCAL_NET`.
- **Echo (200) works but 100 is silent or hangs up at once.** The bot isn't reachable. Check the following:
  - Run `docker compose logs asterisk` and look for the `Bot register result:` line and `AudioSocket` errors.
  - The `app` service must be up and listening on 8000 and 9092.
  - `ASTERISK_HOSTS` on the app must allow the Asterisk container's IP. Otherwise register returns 403 and the bot doesn't know the caller's number, though the call itself still connects.
- **To test without a phone,** run `docker compose exec asterisk asterisk -rx "channel originate Local/100@softphones application Milliwatt"`. The bot should get a call with an empty caller number. Hang it up with `asterisk -rx "channel request hangup all"`.

## Files

- `Dockerfile`: Ubuntu 24.04 plus the `asterisk` package. The stock sample configs are removed.
- `entrypoint.sh`: checks the env vars, renders `conf/*.conf` with `envsubst` (only our variables), then runs `asterisk -f`. Logs go to the console.
- `conf/modules.conf`: explicit, minimal module list (`autoload = no`). Startup takes about 2 s.
- `conf/pjsip.conf`, `conf/rtp.conf`, `conf/extensions.conf`: the SIP accounts, RTP ports and dialplan (context `softphones`).
- The other `conf/*.conf` files are near-empty stubs. They stop Asterisk from logging "missing config" errors.
