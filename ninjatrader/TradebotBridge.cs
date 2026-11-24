// TradebotBridge: NinjaTrader 8 AddOn that publishes account balances for tradebot.
//
// Install: copy this file to Documents\NinjaTrader 8\bin\Custom\AddOns\,
// then in NinjaTrader open the NinjaScript Editor and press F5 to compile.
// Every second it writes outgoing\tradebot_account_<AccountName>.json for each
// connected account. tradebot refuses to trade if this file is missing or stale.
#region Using declarations
using System;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Threading;
using NinjaTrader.Cbi;
using NinjaTrader.Core;
#endregion

namespace NinjaTrader.NinjaScript.AddOns
{
    public class TradebotBridge : AddOnBase
    {
        private Timer timer;

        protected override void OnStateChange()
        {
            if (State == State.SetDefaults)
            {
                Name = "TradebotBridge";
                Description = "Writes account balances to the outgoing folder for tradebot.";
            }
            else if (State == State.Active)
            {
                timer = new Timer(_ => Publish(), null, 0, 1000);
            }
            else if (State == State.Terminated)
            {
                if (timer != null) timer.Dispose();
            }
        }

        private void Publish()
        {
            try
            {
                string outDir = Path.Combine(Globals.UserDataDir, "outgoing");
                Directory.CreateDirectory(outDir);
                Account[] accounts;
                lock (Account.All) accounts = Account.All.ToArray();
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
                    string path = Path.Combine(outDir, "tradebot_account_" + a.Name + ".json");
                    string tmp = path + ".tmp";
                    File.WriteAllText(tmp, json);
                    if (File.Exists(path)) File.Delete(path);
                    File.Move(tmp, path);
                }
            }
            catch (Exception e)
            {
                NinjaTrader.Code.Output.Process("TradebotBridge error: " + e.Message, PrintTo.OutputTab1);
            }
        }
    }
}
