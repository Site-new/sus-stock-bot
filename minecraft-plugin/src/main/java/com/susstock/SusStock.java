package com.susstock;

import org.bukkit.Bukkit;
import org.bukkit.Material;
import org.bukkit.NamespacedKey;
import org.bukkit.command.Command;
import org.bukkit.command.CommandSender;
import org.bukkit.enchantments.Enchantment;
import org.bukkit.entity.Player;
import org.bukkit.event.EventHandler;
import org.bukkit.event.Listener;
import org.bukkit.event.enchantment.EnchantItemEvent;
import org.bukkit.event.inventory.CraftItemEvent;
import org.bukkit.event.inventory.InventoryClickEvent;
import org.bukkit.event.inventory.InventoryCloseEvent;
import org.bukkit.event.inventory.PrepareAnvilEvent;
import org.bukkit.event.inventory.PrepareSmithingEvent;
import org.bukkit.event.player.PlayerJoinEvent;
import org.bukkit.event.player.PlayerPortalEvent;
import org.bukkit.inventory.Inventory;
import org.bukkit.inventory.ItemStack;
import org.bukkit.inventory.meta.ItemMeta;
import org.bukkit.persistence.PersistentDataType;
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
    private volatile boolean netherUnlocked = false;
    private volatile boolean endUnlocked = false;
    private NamespacedKey storeKey;
    private static final String ENCHANT_MENU_TITLE = "§5Your Enchants";
    private final java.util.Map<java.util.UUID, java.util.List<String>> enchantMenus = new java.util.concurrent.ConcurrentHashMap<>();
    private final java.util.Set<String> insiderUuids = java.util.concurrent.ConcurrentHashMap.newKeySet();
    private final java.util.Set<Long> shownInsider = java.util.concurrent.ConcurrentHashMap.newKeySet();
    private final java.util.Set<Long> shownPublic = java.util.concurrent.ConcurrentHashMap.newKeySet();
    private boolean newsFirstRun = true;

    @Override
    public void onEnable() {
        saveDefaultConfig();
        apiBase = getConfig().getString("api_base", "https://sus-stock-bot-production.up.railway.app");
        apiKey = getConfig().getString("api_key", "");
        storeKey = new NamespacedKey(this, "store");
        getServer().getPluginManager().registerEvents(this, this);
        // Auto-deliver pending website purchases to all online players every 30s
        Bukkit.getScheduler().runTaskTimer(this, () -> {
            for (Player p : Bukkit.getOnlinePlayers()) claimRewards(p, false);
        }, 600L, 600L);
        // Poll server-wide dimension unlocks every 20s (and once now)
        Bukkit.getScheduler().runTaskTimer(this, this::pollUnlocks, 20L, 400L);
        // Strip store-only items obtained outside the store, every 3s
        Bukkit.getScheduler().runTaskTimer(this, this::stripIllegalItems, 60L, 60L);
        // Poll insider list (60s) and market news (20s)
        Bukkit.getScheduler().runTaskTimer(this, this::pollInsiders, 40L, 1200L);
        Bukkit.getScheduler().runTaskTimer(this, this::pollNews, 100L, 400L);
        getLogger().info("SusStock enabled. API: " + apiBase);
    }

    private void pollInsiders() {
        runAsync(() -> {
            String resp = get("/api/mc/insiders?key=" + enc(apiKey));
            if (resp == null) return;
            insiderUuids.clear();
            java.util.regex.Matcher m = java.util.regex.Pattern.compile("\"([0-9a-fA-F\\-]{36})\"").matcher(resp);
            while (m.find()) insiderUuids.add(m.group(1));
        });
    }

    private void pollNews() {
        runAsync(() -> {
            String resp = get("/api/mc/news?key=" + enc(apiKey));
            if (resp == null || !resp.trim().startsWith("[")) return;
            java.util.List<long[]> meta = new java.util.ArrayList<>();
            java.util.List<String> headlines = new java.util.ArrayList<>();
            java.util.List<Boolean> positives = new java.util.ArrayList<>();
            java.util.List<String> impacts = new java.util.ArrayList<>();
            // Split objects
            java.util.regex.Matcher obj = java.util.regex.Pattern.compile("\\{[^}]*\\}").matcher(resp);
            while (obj.find()) {
                String o = obj.group();
                long ts = lnum(o, "ts");
                long pub = lnum(o, "public_at");
                String headline = sval(o, "headline");
                boolean pos = o.contains("\"positive\": true") || o.contains("\"positive\":true");
                meta.add(new long[]{ts, pub});
                headlines.add(headline);
                positives.add(pos);
                impacts.add(num(o, "impact"));
            }
            // On first run, mark everything already-seen so we don't spam old news
            if (newsFirstRun) {
                for (long[] mm : meta) { shownInsider.add(mm[0]); shownPublic.add(mm[0]); }
                newsFirstRun = false;
                return;
            }
            final java.util.List<long[]> fMeta = meta;
            final java.util.List<String> fHead = headlines;
            final java.util.List<Boolean> fPos = positives;
            final java.util.List<String> fImp = impacts;
            Bukkit.getScheduler().runTask(this, () -> {
                long t = System.currentTimeMillis() / 1000L;
                java.text.SimpleDateFormat tf = new java.text.SimpleDateFormat("h:mm a");
                for (int i = 0; i < fMeta.size(); i++) {
                    long ts = fMeta.get(i)[0], pub = fMeta.get(i)[1];
                    String color = fPos.get(i) ? "§a" : "§c";
                    String imp = impactStr(fImp.get(i));
                    String time = "§8[" + tf.format(new java.util.Date(ts * 1000L)) + "] ";
                    String head = fHead.get(i) + imp;
                    if (!shownInsider.contains(ts)) {
                        shownInsider.add(ts);
                        // Insiders see it immediately (early if not yet public)
                        boolean early = pub > t;
                        for (Player p : Bukkit.getOnlinePlayers())
                            if (insiderUuids.contains(p.getUniqueId().toString()))
                                p.sendMessage(time + (early ? "§d[Insider] " : "§6[Market] ") + color + head);
                    }
                    if (pub <= t && !shownPublic.contains(ts)) {
                        shownPublic.add(ts);
                        for (Player p : Bukkit.getOnlinePlayers())
                            if (!insiderUuids.contains(p.getUniqueId().toString()))
                                p.sendMessage(time + "§6[Market] " + color + head);
                    }
                }
                if (shownInsider.size() > 200) shownInsider.clear();
                if (shownPublic.size() > 200) shownPublic.clear();
            });
        });
    }

    private long lnum(String json, String key) {
        java.util.regex.Matcher m = java.util.regex.Pattern.compile("\"" + key + "\"\\s*:\\s*(\\d+)").matcher(json);
        return m.find() ? Long.parseLong(m.group(1)) : 0;
    }
    private String num(String json, String key) {
        java.util.regex.Matcher m = java.util.regex.Pattern.compile("\"" + key + "\"\\s*:\\s*(-?\\d+(?:\\.\\d+)?)").matcher(json);
        return m.find() ? m.group(1) : "0";
    }
    private String impactStr(String raw) {
        try {
            double v = Double.parseDouble(raw);
            if (Math.abs(v) < 0.01) return "";
            return " §7(" + (v > 0 ? "§a+" : "§c") + String.format("%.1f", v) + "%§7)";
        } catch (Exception e) { return ""; }
    }
    private String sval(String json, String key) {
        java.util.regex.Matcher m = java.util.regex.Pattern.compile("\"" + key + "\"\\s*:\\s*\"((?:[^\"\\\\]|\\\\.)*)\"").matcher(json);
        return m.find() ? m.group(1).replace("\\\"", "\"").replace("\\\\", "\\") : "";
    }

    // Items that can ONLY be obtained from the website store
    private boolean isStoreOnly(Material m) {
        if (m == null) return false;
        return m == Material.TOTEM_OF_UNDYING || m == Material.ELYTRA
            || m == Material.EXPERIENCE_BOTTLE || m == Material.NETHERITE_INGOT;
    }

    private boolean isStoreTagged(ItemStack item) {
        if (item == null || !item.hasItemMeta()) return false;
        ItemMeta meta = item.getItemMeta();
        return meta != null && meta.getPersistentDataContainer().has(storeKey, PersistentDataType.INTEGER);
    }

    private ItemStack makeStoreItem(Material m, int amount) {
        ItemStack item = new ItemStack(m, amount);
        ItemMeta meta = item.getItemMeta();
        if (meta != null) {
            meta.getPersistentDataContainer().set(storeKey, PersistentDataType.INTEGER, 1);
            item.setItemMeta(meta);
        }
        return item;
    }

    private void stripIllegalItems() {
        for (Player p : Bukkit.getOnlinePlayers()) {
            ItemStack[] contents = p.getInventory().getContents();
            boolean removed = false;
            for (int i = 0; i < contents.length; i++) {
                ItemStack it = contents[i];
                if (it != null && isStoreOnly(it.getType()) && !isStoreTagged(it)) {
                    p.getInventory().setItem(i, null);
                    removed = true;
                }
            }
            if (removed) p.sendMessage("§cThat item can only be obtained from the Sus Stock store.");
        }
    }

    @EventHandler
    public void onCraft(CraftItemEvent e) {
        Material m = e.getRecipe().getResult().getType();
        if (m.name().contains("SHULKER_BOX")) {
            e.setCancelled(true);
            if (e.getWhoClicked() instanceof Player)
                e.getWhoClicked().sendMessage("§cShulker Boxes can only be bought from the Sus Stock store.");
        }
    }

    @EventHandler
    public void onEnchant(EnchantItemEvent e) {
        // Enchanting table disabled — enchants are bought on the website
        e.setCancelled(true);
        e.getEnchanter().sendMessage("§cEnchanting tables are disabled. Buy enchants on the Sus Stock website (/susenchant).");
    }

    @EventHandler
    public void onAnvil(PrepareAnvilEvent e) {
        ItemStack result = e.getResult();
        if (result == null) return;
        ItemStack first = e.getInventory().getItem(0);
        java.util.Map<Enchantment, Integer> before = (first != null) ? first.getEnchantments() : new java.util.HashMap<>();
        for (java.util.Map.Entry<Enchantment, Integer> en : result.getEnchantments().entrySet()) {
            if (en.getValue() > before.getOrDefault(en.getKey(), 0)) {
                e.setResult(null);
                return;
            }
        }
    }

    @EventHandler
    public void onSmithing(PrepareSmithingEvent e) {
        // Block netherite upgrades unless the ingot used was bought from the store
        for (ItemStack item : e.getInventory().getContents()) {
            if (item != null && item.getType() == Material.NETHERITE_INGOT && !isStoreTagged(item)) {
                e.setResult(null);
                return;
            }
        }
    }

    // ── Enchant menu ──────────────────────────────────────────────────────────
    private static final String[] ROMAN = {"", "I", "II", "III", "IV", "V"};

    private String prettyEnchant(String token) {
        String[] parts = token.split(":");
        String name = parts[0].replace("_", " ");
        StringBuilder sb = new StringBuilder();
        for (String w : name.split(" "))
            if (!w.isEmpty()) sb.append(Character.toUpperCase(w.charAt(0))).append(w.substring(1)).append(" ");
        String lvl = "";
        try { int l = Integer.parseInt(parts[1]); lvl = (l < ROMAN.length) ? ROMAN[l] : String.valueOf(l); } catch (Exception ignored) {}
        return sb.toString().trim() + (lvl.isEmpty() ? "" : " " + lvl);
    }

    private void openEnchantMenu(Player p, java.util.List<String> tokens) {
        if (tokens.isEmpty()) { p.sendMessage("§7You haven't bought any enchants. Get them on the website store."); return; }
        int size = ((tokens.size() - 1) / 9 + 1) * 9;
        if (size < 9) size = 9;
        Inventory inv = Bukkit.createInventory(null, size, ENCHANT_MENU_TITLE);
        for (int i = 0; i < tokens.size(); i++) {
            ItemStack book = new ItemStack(Material.ENCHANTED_BOOK);
            ItemMeta meta = book.getItemMeta();
            if (meta != null) {
                meta.setDisplayName("§b" + prettyEnchant(tokens.get(i)));
                meta.setLore(java.util.Arrays.asList("§7Click to apply to the item in your hand"));
                book.setItemMeta(meta);
            }
            inv.setItem(i, book);
        }
        java.util.List<String> copy = new java.util.ArrayList<>(tokens);
        p.openInventory(inv);
        // Put AFTER openInventory so the previous menu's close event doesn't wipe it
        enchantMenus.put(p.getUniqueId(), copy);
    }

    @EventHandler
    public void onMenuClick(InventoryClickEvent e) {
        if (!ENCHANT_MENU_TITLE.equals(e.getView().getTitle())) return;
        e.setCancelled(true);
        if (!(e.getWhoClicked() instanceof Player)) return;
        Player p = (Player) e.getWhoClicked();
        java.util.List<String> tokens = enchantMenus.get(p.getUniqueId());
        if (tokens == null) return;
        int slot = e.getRawSlot();
        if (slot < 0 || slot >= tokens.size()) return;
        String token = tokens.get(slot);
        ItemStack held = p.getInventory().getItemInMainHand();
        if (held == null || held.getType() == Material.AIR) {
            p.sendMessage("§cHold the item you want enchanted, then click again.");
            return;
        }
        String[] parts = token.split(":");
        Enchantment ench = parts.length >= 1 ? Enchantment.getByKey(NamespacedKey.minecraft(parts[0])) : null;
        if (ench == null) { p.sendMessage("§cUnknown enchant."); return; }
        // Only allow enchants that legitimately fit this item (no Fortune on a sword, etc.)
        if (!ench.canEnchantItem(held)) {
            p.sendMessage("§c" + prettyEnchant(token) + " can't go on a " + held.getType().name().toLowerCase().replace("_", " ") + ".");
            return;
        }
        int level;
        try { level = Integer.parseInt(parts[1]); } catch (Exception ex) { level = 1; }
        final int flevel = level;
        // Consume server-side, then apply
        runAsync(() -> {
            String resp = post("/api/mc/use_enchant", "{\"uuid\":\"" + p.getUniqueId() + "\",\"token\":\"" + esc(token) + "\"}");
            if (resp == null || !resp.contains("\"ok\"")) { p.sendMessage("§cCouldn't apply that enchant."); return; }
            Bukkit.getScheduler().runTask(this, () -> {
                ItemStack cur = p.getInventory().getItemInMainHand();
                if (cur == null || cur.getType() == Material.AIR) { p.sendMessage("§cYou're no longer holding an item."); return; }
                cur.addUnsafeEnchantment(ench, flevel);
                p.sendMessage("§a✨ Applied " + prettyEnchant(token) + " to your " + cur.getType().name().toLowerCase().replace("_", " ") + "!");
                tokens.remove(token);
                if (tokens.isEmpty()) {
                    p.closeInventory();
                    p.sendMessage("§7All purchased enchants applied.");
                } else {
                    openEnchantMenu(p, tokens);
                }
            });
        });
    }

    @EventHandler
    public void onMenuClose(InventoryCloseEvent e) {
        if (ENCHANT_MENU_TITLE.equals(e.getView().getTitle()))
            enchantMenus.remove(e.getPlayer().getUniqueId());
    }

    private void pollUnlocks() {
        runAsync(() -> {
            String resp = get("/api/mc/unlocks?key=" + enc(apiKey));
            if (resp == null) return;
            netherUnlocked = resp.contains("\"nether\":true") || resp.contains("\"nether\": true");
            endUnlocked = resp.contains("\"end\":true") || resp.contains("\"end\": true");
        });
    }

    @EventHandler
    public void onPortal(PlayerPortalEvent e) {
        String c = e.getCause().name();
        if (c.contains("NETHER") && !netherUnlocked) {
            e.setCancelled(true);
            e.getPlayer().sendMessage("§c🔒 The Nether is locked! Unlock it on the website (Server Unlocks).");
        } else if (c.contains("END") && !endUnlocked) {
            e.setCancelled(true);
            e.getPlayer().sendMessage("§c🔒 The End is locked! Unlock it on the website (Server Unlocks).");
        }
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
            // Run on the main thread
            Bukkit.getScheduler().runTask(this, () -> {
                for (String c : cmds) {
                    if (c.startsWith("@susitem:")) {
                        // Tagged store-only item: @susitem:MATERIAL:AMOUNT
                        String[] parts = c.substring(9).split(":");
                        try {
                            Material m = Material.valueOf(parts[0]);
                            int amt = parts.length > 1 ? Integer.parseInt(parts[1]) : 1;
                            p.getInventory().addItem(makeStoreItem(m, amt));
                        } catch (Exception ex) {
                            getLogger().warning("Bad susitem: " + c);
                        }
                    } else {
                        Bukkit.dispatchCommand(Bukkit.getConsoleSender(), c.replace("%player%", p.getName()));
                    }
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

        if (cmd.getName().equalsIgnoreCase("susenchant")) {
            if (!(sender instanceof Player)) { sender.sendMessage("Players only."); return true; }
            Player p = (Player) sender;
            runAsync(() -> {
                String resp = get("/api/mc/my_enchants?uuid=" + p.getUniqueId() + "&key=" + enc(apiKey));
                if (resp == null || !resp.contains("\"enchants\"")) { p.sendMessage("§7Couldn't load your enchants."); return; }
                java.util.List<String> list = parseCommands(resp);
                Bukkit.getScheduler().runTask(this, () -> openEnchantMenu(p, list));
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
