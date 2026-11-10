# Asterisk 20 (Ubuntu 24.04 package) for RingMind: PJSIP softphones -> AudioSocket -> bot.
# Multi-arch: works on arm64 (Apple Silicon) and amd64.
FROM ubuntu:24.04

ARG DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
 && apt-get install -y --no-install-recommends asterisk asterisk-modules gettext-base ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 # Drop the stock sample configs; we ship a minimal set (asterisk.conf keeps the distro paths).
 && find /etc/asterisk -mindepth 1 ! -name asterisk.conf -delete

COPY conf/ /etc/asterisk/templates/
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

# SIP signalling + RTP media (must match rtp.conf and the compose port mappings).
EXPOSE 5060/udp 10000-10099/udp

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
