# Sus Stock — Minecraft Plugin

Links Minecraft players to the Sus Stock Market website so they share one wallet.

## Commands
- `/suslink <code>` — link your account (get the code from the website's "🟩 Link MC" button)
- `/susbalance` — check your SUS cash in-game
- `/susreward <player> <amount>` — (admin) give a player SUS cash

## Build
Requires Java 17+ and Maven.
```
cd minecraft-plugin
mvn package
```
The plugin jar will be at `target/SusStock.jar`.

## Install
1. Drop `SusStock.jar` into your server's `plugins/` folder.
2. Start the server once to generate `plugins/SusStock/config.yml`.
3. Edit that config:
   - `api_base`: your website URL (default is the Railway URL)
   - `api_key`: must match the `MC_API_KEY` environment variable on the Railway server
4. Run `/reload` or restart.

## How linking works
1. Player clicks **🟩 Link MC** on the website → **Generate Link Code** → gets a 6-char code.
2. In Minecraft they run `/suslink CODE`.
3. The plugin calls the website API and ties their Minecraft UUID to their Discord account.
4. From then on, their SUS cash is shared between the website and Minecraft.
