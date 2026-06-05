package com.susstock;

import org.bukkit.Bukkit;
import org.bukkit.command.Command;
import org.bukkit.command.CommandSender;
import org.bukkit.entity.Player;
import org.bukkit.event.EventHandler;
import org.bukkit.event.Listener;
import org.bukkit.event.player.PlayerJoinEvent;
import org.bukkit.plugin.java.JavaPlugin;

import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.net.URLEncoder;
import java.nio.charset.StandardCharsets;
import java.util.Scanner;

public class SusStock extends JavaPlugin implements Listener {

    private String apiBase;
    private String apiKey;

    @Override
    public void onEnable() {
        saveDefaultConfig();
        apiBase = getConfig().getString("api_base", "https://sus-stock-bot-production.up.railway.app");
        apiKey = getConfig().getString("api_key", "");
        getServer().getPluginManager().registerEvents(this, this);
        // Auto-deliver pending website purchases to all online players every 30s
        Bukkit.getScheduler().runTaskTimer(this, () -> {
            for (Player p : Bukkit.getOnlinePlayers()) claimRewards(p, false);
        }, 600L, 600L);
        getLogger().info("SusStock enabled. API: " + apiBase);
    }

    @EventHandler
    public void onJoin(PlayerJoinEvent e) {
        // Deliver any pending in-game purchases shortly after join
        Bukkit.getScheduler().runTaskLater(this, () -> claimRewards(e.getPlayer(), false), 40L);
    }

    private void claimRewards(Player p, boolean announce) {
        runAsync(() -> {
            String resp = get("/api/mc/pending?uuid=" + p.getUniqueId() + "&key=" + enc(apiKey));
            if (resp == null || !resp.contains("\"commands\"")) {
                if (announce) p.sendMessage("§7No pending Sus Stock items.");
                return;
            }
            java.util.List<String> cmds = parseCommands(resp);
            if (cmds.isEmpty()) {
                if (announce) p.sendMessage("§7No pending Sus Stock items.");
                return;
            }
            // Run console commands on the main thread
            Bukkit.getScheduler().runTask(this, () -> {
                for (String c : cmds) {
                    String cmd = c.replace("%player%", p.getName());
                    Bukkit.dispatchCommand(Bukkit.getConsoleSender(), cmd);
                }
                p.sendMessage("§a🎁 Delivered " + cmds.size() + " Sus Stock item(s)!");
            });
        });
    }

    private java.util.List<String> parseCommands(String json) {
        java.util.List<String> out = new java.util.ArrayList<>();
        int i = json.indexOf("[");
        int j = json.indexOf("]", i);
        if (i < 0 || j < 0) return out;
        String inner = json.substring(i + 1, j);
        java.util.regex.Matcher m = java.util.regex.Pattern.compile("\"((?:[^\"\\\\]|\\\\.)*)\"").matcher(inner);
        while (m.find()) out.add(m.group(1).replace("\\\"", "\"").replace("\\\\", "\\"));
        return out;
    }

    @Override
    public boolean onCommand(CommandSender sender, Command cmd, String label, String[] args) {
        if (cmd.getName().equalsIgnoreCase("suslink")) {
            if (!(sender instanceof Player)) { sender.sendMessage("Players only."); return true; }
            Player p = (Player) sender;
            if (args.length < 1) { p.sendMessage("§eUsage: /suslink <code>  (get a code from the website)"); return true; }
            String code = args[0];
            runAsync(() -> {
                String body = "{\"code\":\"" + esc(code) + "\",\"uuid\":\"" + p.getUniqueId() + "\",\"username\":\"" + esc(p.getName()) + "\"}";
                String resp = post("/api/mc/link", body);
                if (resp != null && resp.contains("\"ok\"")) {
                    p.sendMessage("§a✔ Linked to Sus Stock! Your wallet is now shared with the website.");
                } else {
                    p.sendMessage("§c✖ Link failed — code may be invalid or expired. Generate a new one on the website.");
                }
            });
            return true;
        }

        if (cmd.getName().equalsIgnoreCase("susbalance")) {
            if (!(sender instanceof Player)) { sender.sendMessage("Players only."); return true; }
            Player p = (Player) sender;
            runAsync(() -> {
                String resp = get("/api/mc/balance?uuid=" + p.getUniqueId() + "&key=" + enc(apiKey));
                if (resp != null && resp.contains("balance")) {
                    String bal = extract(resp, "balance");
                    p.sendMessage("§e💰 Sus Stock balance: §a$" + bal);
                } else {
                    p.sendMessage("§cNot linked yet. Run /suslink <code> with a code from the website.");
                }
            });
            return true;
        }

        if (cmd.getName().equalsIgnoreCase("susreward")) {
            if (!sender.hasPermission("susstock.admin")) { sender.sendMessage("§cNo permission."); return true; }
            if (args.length < 2) { sender.sendMessage("§eUsage: /susreward <player> <amount>"); return true; }
            Player target = Bukkit.getPlayerExact(args[0]);
            if (target == null) { sender.sendMessage("§cPlayer not found."); return true; }
            double amount;
            try { amount = Double.parseDouble(args[1]); } catch (Exception e) { sender.sendMessage("§cInvalid amount."); return true; }
            runAsync(() -> {
                String body = "{\"uuid\":\"" + target.getUniqueId() + "\",\"amount\":" + amount + ",\"reason\":\"Reward in Minecraft\"}";
                String resp = post("/api/mc/add", body);
                if (resp != null && resp.contains("\"ok\"")) {
                    sender.sendMessage("§aGave $" + amount + " to " + target.getName());
                    target.sendMessage("§a💰 You received $" + amount + " in Sus Stock!");
                } else {
                    sender.sendMessage("§cFailed — is the player linked?");
                }
            });
            return true;
        }
        return false;
    }

    // ── HTTP helpers ──────────────────────────────────────────────────────────
    private void runAsync(Runnable r) { Bukkit.getScheduler().runTaskAsynchronously(this, r); }

    private String post(String path, String json) {
        try {
            HttpURLConnection c = (HttpURLConnection) new URL(apiBase + path).openConnection();
            c.setRequestMethod("POST");
            c.setRequestProperty("Content-Type", "application/json");
            c.setRequestProperty("X-API-Key", apiKey);
            c.setConnectTimeout(8000); c.setReadTimeout(8000);
            c.setDoOutput(true);
            try (OutputStream os = c.getOutputStream()) { os.write(json.getBytes(StandardCharsets.UTF_8)); }
            return readResp(c);
        } catch (Exception e) { getLogger().warning("POST " + path + " failed: " + e.getMessage()); return null; }
    }

    private String get(String path) {
        try {
            HttpURLConnection c = (HttpURLConnection) new URL(apiBase + path).openConnection();
            c.setRequestProperty("X-API-Key", apiKey);
            c.setConnectTimeout(8000); c.setReadTimeout(8000);
            return readResp(c);
        } catch (Exception e) { getLogger().warning("GET " + path + " failed: " + e.getMessage()); return null; }
    }

    private String readResp(HttpURLConnection c) {
        try {
            int code = c.getResponseCode();
            java.io.InputStream is = (code >= 200 && code < 300) ? c.getInputStream() : c.getErrorStream();
            if (is == null) return null;
            try (Scanner s = new Scanner(is, "UTF-8").useDelimiter("\\A")) {
                return s.hasNext() ? s.next() : "";
            }
        } catch (Exception e) { return null; }
    }

    private String extract(String json, String key) {
        int i = json.indexOf("\"" + key + "\"");
        if (i < 0) return "?";
        int colon = json.indexOf(":", i);
        int end = colon + 1;
        while (end < json.length() && ",}".indexOf(json.charAt(end)) < 0) end++;
        return json.substring(colon + 1, end).replace("\"", "").trim();
    }

    private String esc(String s) { return s.replace("\\", "\\\\").replace("\"", "\\\""); }
    private String enc(String s) { try { return URLEncoder.encode(s, "UTF-8"); } catch (Exception e) { return s; } }
}
