# Herdr bridge

This is a small program that runs on your Mac or Linux computer. It lets the Herdr iPhone app talk to the herdr on that computer, so you can check on your agents from your phone. It runs quietly in the background and starts again every time you log in (on Linux it uses a systemd user service).

## What you need first

1. A Mac or a Linux computer.
2. herdr installed on it.
3. Tailscale on it, signed in. Tailscale is a free app that links your devices privately, so your phone can reach your Mac from anywhere.
4. Tailscale on your iPhone, signed in with the same account.
5. HTTPS Certificates switched on in Tailscale. Open https://login.tailscale.com/admin/dns and turn it on. If you skip this, the installer will tell you.

## Install

1. Open a terminal (the Terminal app on a Mac).
2. Paste this line and press Enter:

```
curl -fsSL https://raw.githubusercontent.com/kuldeepakkatwal/herdr-bridge/main/install.sh | bash
```

3. When it finishes, a QR code is drawn right in the terminal. A QR code is the square black and white pattern a camera can read. Make the terminal window big enough to show all of it.
4. On your iPhone, open the Herdr app, tap Add machine, then tap Scan QR code. Or just point the normal Camera app at it.
5. Tap Connect.

On Linux, the installer may ask for your password once. That lets your user publish the bridge through Tailscale.

That is it. If something goes wrong, the installer prints one sentence saying what to fix. Fix it and paste the line again.

## Update

Paste the same install line again. It downloads the newest version and restarts the bridge. Nothing breaks if you run it many times.

## Uninstall

On a Mac, paste this whole line into Terminal:

```
launchctl bootout gui/$(id -u)/com.herdr-remote.bridge; tailscale serve --https=8795 off; rm -rf ~/.herdr-remote ~/Library/LaunchAgents/com.herdr-remote.bridge.plist
```

On Linux, paste this instead:

```
systemctl --user disable --now herdr-remote; tailscale serve --https=8795 off; rm -rf ~/.herdr-remote ~/.config/systemd/user/herdr-remote.service; systemctl --user daemon-reload
```

## Is it safe?

The bridge only listens on your own computer. It is shared to your phone through Tailscale, and it only answers the Tailscale login of the person who installed it. Other people on the same Tailscale network are turned away.
