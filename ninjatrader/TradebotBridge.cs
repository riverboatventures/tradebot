// TradebotBridge: NinjaTrader 8 INDICATOR that feeds tradebot.
//
// Put it on ONE chart (e.g. MES 12-26, 5 Minute). While that chart is open it:
//  1. writes outgoing\tradebot_account_<Account>.json every second for each connected account
//     (tradebot refuses to trade if this file is missing or stale), and
//  2. writes outgoing\tradebot_bars_<Instrument>_<min>.csv with the last 300 bars for the chart's
//     instrument plus every instrument listed in "Other instruments" (bar CLOSE time, UTC).
//
// Install: NinjaScript Editor > right-click Indicators > New Indicator > name it TradebotBridge >
// Generate > replace all the code with this file > Ctrl+S. Then add it to a 5 Minute chart.
#region Using declarations
using System;
using System.Collections.Generic;
using System.ComponentModel.DataAnnotations;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Text;
using System.Threading;
using NinjaTrader.Cbi;
using NinjaTrader.Core;
using NinjaTrader.Data;
using NinjaTrader.NinjaScript;
#endregion

namespace NinjaTrader.NinjaScript.Indicators
{
    public class TradebotBridge : Indicator
    {
        private const int BarsToKeep = 300;
        private Timer accountTimer;
        private readonly Dictionary<int, DateTime> lastWrite = new Dictionary<int, DateTime>();
        private static bool loggedAccounts;

        [Display(Name = "Other instruments", Description = "Comma-separated, with contract month",
                 GroupName = "Parameters", Order = 1)]
        public string OtherInstruments { get; set; }

        protected override void OnStateChange()
        {
            if (State == State.SetDefaults)
            {
                Name = "TradebotBridge";
                Description = "Publishes account balances and price bars for tradebot.";
                Calculate = Calculate.OnPriceChange;
                IsOverlay = true;
                DisplayInDataBox = false;
                PaintPriceMarkers = false;
                IsSuspendedWhileInactive = false;  // keep working when the chart is hidden
                OtherInstruments = "MNQ 12-26, MYM 12-26, M2K 12-26, MCL 01-27, MGC 02-27";
            }
            else if (State == State.Configure)
            {
                string own = Instrument != null ? Instrument.FullName : "";
                foreach (string raw in (OtherInstruments ?? "").Split(','))
                {
                    string name = raw.Trim();
                    if (name.Length > 0 && name != own)
                        AddDataSeries(name, BarsPeriodType.Minute, BarsPeriod.Value);
                }
            }
            else if (State == State.DataLoaded)
            {
                accountTimer = new Timer(_ => PublishAccounts(), null, 0, 1000);
                Print("TradebotBridge: started. Writing files to " + OutDir);
            }
            else if (State == State.Terminated)
            {
                if (accountTimer != null)
                {
                    accountTimer.Dispose();
                    accountTimer = null;
                }
            }
        }

        protected override void OnBarUpdate()
        {
            int b = BarsInProgress;
            // While loading history, only write once we reach the most recent bars.
            if (State == State.Historical && CurrentBars[b] < BarsArray[b].Count - 2)
                return;
            DateTime last;
            if (lastWrite.TryGetValue(b, out last) && (DateTime.UtcNow - last).TotalSeconds < 1)
                return;  // updates arrive on every price change; write at most once per second
            lastWrite[b] = DateTime.UtcNow;
            WriteBars(BarsArray[b]);
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
            if (File.Exists(path)) File.Replace(tmp, path, null);
            else File.Move(tmp, path);
        }

        private static void Log(string msg)
        {
            NinjaTrader.Code.Output.Process("TradebotBridge: " + msg, PrintTo.OutputTab1);
        }

        private static void PublishAccounts()
        {
            try
            {
                Account[] accounts;
                lock (Account.All) accounts = Account.All.ToArray();
                if (!loggedAccounts)
                {
                    loggedAccounts = true;
                    Log("accounts: " + string.Join(", ", accounts.Select(x => x.Name + " (" + x.ConnectionStatus + ")")));
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
            catch (Exception e)
            {
                Log("accounts: " + e.Message);
            }
        }

        private static void WriteBars(Bars bars)
        {
            try
            {
                string fileName = "tradebot_bars_" + bars.Instrument.FullName.Replace(' ', '_') + "_"
                                  + bars.BarsPeriod.Value + ".csv";
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
                Log("bars: " + e.Message);
            }
        }
    }
}
