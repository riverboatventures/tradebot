// TradebotBridge: NinjaTrader 8 AddOn that feeds tradebot.
//
//  1. Account balances: every second writes outgoing\tradebot_account_<Account>.json
//     for each connected account. tradebot refuses to trade if it's missing or stale.
//  2. Price bars: reads tradebot\symbols.txt (written by tradebot, lines like "MES 12-26|5"),
//     subscribes to each instrument, and keeps outgoing\tradebot_bars_<Instrument>_<min>.csv
//     updated with the last 300 bars (bar CLOSE time in UTC).
//
// Install: copy this file to Documents\NinjaTrader 8\bin\Custom\AddOns\,
// open the NinjaScript Editor and press F5 to compile. Needs a connected data feed.
#region Using declarations
using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Text;
using System.Threading;
using System.Windows;
using NinjaTrader.Cbi;
using NinjaTrader.Core;
using NinjaTrader.Data;
#endregion

namespace NinjaTrader.NinjaScript.AddOns
{
    public class TradebotBridge : AddOnBase
    {
        private const int BarsToKeep = 300;
        private Timer timer;
        private bool loggedAccounts;
        private readonly object sync = new object();
        private readonly Dictionary<string, BarsRequest> requests = new Dictionary<string, BarsRequest>();
        private readonly Dictionary<string, DateTime> lastWrite = new Dictionary<string, DateTime>();
        private int tick;

        protected override void OnStateChange()
        {
            if (State == State.SetDefaults)
            {
                Name = "TradebotBridge";
                Description = "Publishes account balances and price bars for tradebot.";
                // Diagnostic: proves NinjaTrader compiled and loaded this add-on.
                try { AtomicWrite(Path.Combine(OutDir, "tradebot_bridge_loaded.txt"), DateTime.UtcNow.ToString("o")); }
                catch (Exception) { }
            }
            else if (State == State.Configure || State == State.Active)
            {
                StartOnce();
            }
            else if (State == State.Terminated)
            {
                if (timer != null) timer.Dispose();
                lock (sync)
                {
                    foreach (BarsRequest br in requests.Values) br.Dispose();
                    requests.Clear();
                }
            }
        }

        // NinjaTrader calls this for every window it opens, including the Control Center at startup.
        // This is the most reliable place for an add-on to start its work.
        protected override void OnWindowCreated(Window window)
        {
            StartOnce();
        }

        private void StartOnce()
        {
            lock (sync)
            {
                if (timer != null) return;
                timer = new Timer(_ => OnTimer(), null, 0, 1000);
            }
            Log("started. Writing files to " + OutDir);
        }

        private static string OutDir
        {
            get
            {
                string d = Path.Combine(Globals.UserDataDir, "outgoing");
                Directory.CreateDirectory(d);
                return d;
            }
        }

        private static void AtomicWrite(string path, string content)
        {
            string tmp = path + ".tmp";
            File.WriteAllText(tmp, content);
            if (File.Exists(path)) File.Delete(path);
            File.Move(tmp, path);
        }

        private static void Log(string msg)
        {
            NinjaTrader.Code.Output.Process("TradebotBridge: " + msg, PrintTo.OutputTab1);
        }

        private void OnTimer()
        {
            try { AtomicWrite(Path.Combine(OutDir, "tradebot_bridge_heartbeat.txt"), DateTime.UtcNow.ToString("o")); }
            catch (Exception e) { Log("heartbeat: " + e.Message); }
            try { PublishAccounts(); } catch (Exception e) { Log("accounts: " + e.Message); }
            if (tick++ % 10 == 0)
            {
                try { SyncSubscriptions(); } catch (Exception e) { Log("subscriptions: " + e.Message); }
            }
        }

        private void PublishAccounts()
        {
            Account[] accounts;
            lock (Account.All) accounts = Account.All.ToArray();
            if (!loggedAccounts)
            {
                loggedAccounts = true;
                Log("accounts found: " + (accounts.Length == 0 ? "none" :
                    string.Join(", ", accounts.Select(x => x.Name + " (" + x.ConnectionStatus + ")"))));
            }
            foreach (Account a in accounts)
            {
                if (a.ConnectionStatus != ConnectionStatus.Connected) continue;
                string json = string.Format(CultureInfo.InvariantCulture,
                    "{{\"Account\":\"{0}\",\"NetLiquidation\":{1},\"CashValue\":{2},\"RealizedProfitLoss\":{3},\"UnrealizedProfitLoss\":{4},\"Timestamp\":\"{5:o}\"}}",
                    a.Name,
                    a.Get(AccountItem.NetLiquidation, Currency.UsDollar),
                    a.Get(AccountItem.CashValue, Currency.UsDollar),
                    a.Get(AccountItem.RealizedProfitLoss, Currency.UsDollar),
                    a.Get(AccountItem.UnrealizedProfitLoss, Currency.UsDollar),
                    DateTime.UtcNow);
                AtomicWrite(Path.Combine(OutDir, "tradebot_account_" + a.Name + ".json"), json);
            }
        }

        private void SyncSubscriptions()
        {
            string cfg = Path.Combine(Globals.UserDataDir, "tradebot", "symbols.txt");
            if (!File.Exists(cfg)) return;
            foreach (string raw in File.ReadAllLines(cfg))
            {
                string key = raw.Trim();
                if (key.Length == 0) continue;
                lock (sync) { if (requests.ContainsKey(key)) continue; }

                string[] parts = key.Split('|');
                int minutes;
                if (parts.Length != 2 || !int.TryParse(parts[1], out minutes))
                {
                    Log("bad line in symbols.txt: " + key);
                    continue;
                }
                Instrument instrument = Instrument.GetInstrument(parts[0]);
                if (instrument == null)
                {
                    Log("unknown instrument: " + parts[0]);
                    continue;
                }

                BarsRequest br = new BarsRequest(instrument, BarsToKeep);
                br.BarsPeriod = new BarsPeriod { BarsPeriodType = BarsPeriodType.Minute, Value = minutes };
                br.TradingHours = instrument.MasterInstrument.TradingHours;
                string fileName = "tradebot_bars_" + parts[0].Replace(' ', '_') + "_" + minutes + ".csv";
                br.Update += (sender, e) => WriteBars(fileName, (BarsRequest)sender, false);
                br.Request((req, error, message) =>
                {
                    if (error != ErrorCode.NoError) Log(key + " request failed: " + message);
                    else WriteBars(fileName, req, true);
                });
                lock (sync) requests[key] = br;
                Log("subscribed " + key);
            }
        }

        private void WriteBars(string fileName, BarsRequest req, bool force)
        {
            try
            {
                lock (sync)
                {
                    DateTime last;
                    if (!force && lastWrite.TryGetValue(fileName, out last) && (DateTime.UtcNow - last).TotalSeconds < 1)
                        return;  // Update fires on every tick; write at most once per second
                    lastWrite[fileName] = DateTime.UtcNow;
                }
                Bars bars = req.Bars;
                int n = bars.Count;
                StringBuilder sb = new StringBuilder("time_utc,open,high,low,close,volume\n");
                for (int i = Math.Max(0, n - BarsToKeep); i < n; i++)
                {
                    DateTime utc = TimeZoneInfo.ConvertTimeToUtc(
                        DateTime.SpecifyKind(bars.GetTime(i), DateTimeKind.Unspecified),
                        Globals.GeneralOptions.TimeZoneInfo);
                    sb.AppendFormat(CultureInfo.InvariantCulture, "{0:yyyy-MM-ddTHH:mm:ss},{1},{2},{3},{4},{5}\n",
                        utc, bars.GetOpen(i), bars.GetHigh(i), bars.GetLow(i), bars.GetClose(i), bars.GetVolume(i));
                }
                AtomicWrite(Path.Combine(OutDir, fileName), sb.ToString());
            }
            catch (Exception e)
            {
                Log("write " + fileName + ": " + e.Message);
            }
        }
    }
}
