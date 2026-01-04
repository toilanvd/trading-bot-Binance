import matplotlib.pyplot as plt
from datetime import datetime, timedelta
import math
import statistics

# Load data
datetimes = []
BTC_prices = []
estimated_balances = []
coins_holding = []
with open("estimated_balance.txt") as data_file:
    for line in data_file:
        timestamp_str, price_str, bal_str, coin_str = line.strip().split(';')
        # parse and shift to GMT+7
        dt_utc = datetime.strptime(timestamp_str.split('.')[0], '%Y-%m-%d %H:%M:%S')
        dt_local = dt_utc + timedelta(hours=7)
        datetimes.append(dt_local)
        BTC_prices.append(float(price_str))
        estimated_balances.append(float(bal_str))
        coins_holding.append(coin_str)

# Calculate returns for Sharpe ratio
returns = [0.0]
for prev, curr in zip(estimated_balances, estimated_balances[1:]):
    returns.append(curr / prev - 1)

# Risk-free rate per minute (annualized 4.7% roughly)
RISK_FREE_RATE = 1.047 ** (1.0 / 365 / 24 / 60) - 1
expected_return = sum(returns) / len(returns)
std_dev = statistics.stdev(returns)
Sharpe_ratio_minute = (expected_return - RISK_FREE_RATE) / std_dev
Sharpe_ratio_year = Sharpe_ratio_minute * math.sqrt(60 * 24 * 365)

# Compute max drawdown
max_dd = 0.0
peak = estimated_balances[0]
for bal in estimated_balances:
    peak = max(peak, bal)
    dd = bal / peak - 1
    max_dd = min(max_dd, dd)

# Plot setup
fig, ax1 = plt.subplots()
# Title with ROI details
cur_bal = estimated_balances[-1]
start_bal = estimated_balances[0]
all_roi_pct = math.floor((cur_bal / start_bal - 1) * 10000) / 100.0
max_dd_pct = math.floor(max_dd * 10000) / 100.0
sharpe_disp = math.floor(Sharpe_ratio_year * 100) / 100.0

fig.suptitle(
    f"Balance = {math.floor(cur_bal)} USDC ("
    f"maxDrawdown = {max_dd_pct}%, Sharpe = {sharpe_disp}, ROI = {'+' if all_roi_pct>=0 else ''}{all_roi_pct}%)",
    fontsize=20
)

# Define the split point for the two-colored line
target_utc = datetime(2024, 12, 31, 17, 0, 0)
target_local = target_utc + timedelta(hours=7)
log2_balances = [math.log2(bal / start_bal) for bal in estimated_balances]
split_idx = next((i for i, t in enumerate(datetimes) if t >= target_local), len(datetimes))

# Plot profit multiplier
ax1.plot(datetimes[:split_idx], log2_balances[:split_idx], color='olive', linewidth=0.75)
ax1.plot(datetimes[split_idx:], log2_balances[split_idx:], color='green', linewidth=1.5)

# Annotate coin holdings and balances
for x, y, coin, bal in zip(datetimes, log2_balances, coins_holding, estimated_balances):
    if coin and coin.endswith('*'):
        # marker
        ax1.plot(x, y, 'mo')

        # coin label just below the point
        ax1.annotate(
            coin[:-2],
            xy=(x, y),
            xytext=(0, -12),
            textcoords='offset points',
            ha='center',
            va='top'
        )

        # balance label above the point, rounded down, with arrow
        ax1.annotate(
            f"{math.floor(bal)}",
            xy=(x, y),
            xytext=(0, 12),
            textcoords='offset points',
            ha='center',
            va='bottom',
            arrowprops=dict(arrowstyle='->', color='gray', lw=0.7)
        )

# Axes labels and formatting
ax1.set_xlabel('Time')
ax1.set_ylabel('Profit multiplier (log2 scale)')
ax1.tick_params(axis='y')
ax1.yaxis.get_major_formatter().set_useOffset(False)

# Secondary axis for BTC price
ax2 = ax1.twinx()
ax2.set_ylabel('BTC price (USDC)', color='tab:orange')
ax2.plot(datetimes, BTC_prices, color='tab:orange', linewidth=0.75)
for x, price, coin in zip(datetimes, BTC_prices, coins_holding):
    if coin and coin.endswith('*'):
        ax2.plot(x, price, 'bo')
ax2.tick_params(axis='y', labelcolor='tab:orange')

plt.tight_layout()
plt.show()
